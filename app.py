
import asyncio
import json
import os
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from random import randint

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient
from telethon.errors import FloodWaitError

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

CONFIG_PATH = Path("config.json")
DB_PATH = Path("forwarded.sqlite3")
MAX_TARGETS = 10
MAX_POSTS = 3
MAX_INTERVAL_MINUTES = 1440

DEFAULT_CONFIG = {
    "source": "",
    "target": "",
    "posts_per_round": 2,
    "interval_min": 25,
    "interval_max": 40,
    "batch_pause": 5,
}

# ---------- CONFIG ----------

if CONFIG_PATH.exists():
    try:
        config = {
            **DEFAULT_CONFIG,
            **json.loads(CONFIG_PATH.read_text(encoding="utf-8")),
        }
    except (ValueError, OSError):
        config = DEFAULT_CONFIG.copy()
else:
    config = DEFAULT_CONFIG.copy()


def save_config():
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def parse_targets(value):
    if not isinstance(value, str):
        return []

    return list(dict.fromkeys(
        value.replace(",", " ").replace(";", " ").split()
    ))


# ---------- DATABASE / LOGS ----------

db = sqlite3.connect(DB_PATH)
db.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    )
""")
db.commit()


def log_event(level, message):
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    clock = now.strftime("%H:%M")

    db.execute(
        "INSERT INTO logs (created_at, level, message) VALUES (?, ?, ?)",
        (timestamp, level.upper(), str(message)[:1500]),
    )
    db.commit()

    print(f"[{clock}] [{level.upper()}] {message}")


def get_recent_logs(limit=10):
    return db.execute(
        """
        SELECT created_at, level, message
        FROM logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_last_log():
    return db.execute(
        "SELECT created_at, level, message FROM logs ORDER BY id DESC LIMIT 1"
    ).fetchone()


# ---------- BOT & UBOT ----------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = TelegramClient("ubot", API_ID, API_HASH)

running = False
loop_task = None
forward_lock = asyncio.Lock()

source_entity = None
target_entities = []

current_round = 0
current_activity = "Belum ada aktivitas"
next_run_at = None
last_round_result = "Belum ada putaran"


def is_admin(user_id):
    return user_id == ADMIN_ID


def target_label(entity):
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"

    return (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or "Target tidak diketahui"
    )


async def notify_admin(text):
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as exc:
        log_event("ERROR", f"Notifikasi admin gagal: {exc}")


# ---------- PANEL ----------

def build_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Start", callback_data="forward_start")
    kb.button(text="⏸️ Pause", callback_data="forward_pause")
    kb.button(text="🧾 Logs", callback_data="forward_logs")
    kb.adjust(2)
    return kb.as_markup()


def panel_text():
    targets = parse_targets(config.get("target", ""))
    interval = (
        f"{config['interval_min']}–{config['interval_max']} menit"
    )

    status = "🟢 RUNNING" if running else "⏸️ PAUSED"

    if next_run_at and running:
        remaining = max(
            0, int((next_run_at - datetime.now()).total_seconds())
        )
        next_run = f"{remaining // 60} menit {remaining % 60} detik lagi"
    else:
        next_run = "Tidak dijadwalkan"

    return (
        "📡 TELEGRAM AUTO FORWARD\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Status: {status}\n"
        f"Source: {config.get('source') or 'Belum diatur'}\n"
        f"Jumlah target: {len(targets)}\n"
        f"Target: {', '.join(targets) or 'Belum diatur'}\n\n"
        "⚙️ PENGATURAN\n"
        f"Postingan per putaran: {config['posts_per_round']}\n"
        f"Jeda antarputaran: {interval}\n"
        f"Jeda antarbatch: {config['batch_pause']} menit\n\n"
        "📈 AKTIVITAS\n"
        f"Putaran saat ini: {current_round}\n"
        f"Aktivitas: {current_activity}\n"
        f"Hasil terakhir: {last_round_result}\n"
        f"Putaran berikutnya: {next_run}\n\n"
        "🛠️ PERINTAH\n"
        "/source @channel\n"
        "/target @grup1 @grup2\n"
        "/posts 1-3\n"
        "/interval 25 40\n"
        "/batchpause 5\n"
    )


async def show_panel(message):
    await message.answer(panel_text(), reply_markup=build_menu())


# ---------- POSTINGAN SOURCE ----------

async def get_latest_posts():
    """
    Ambil 1-3 postingan terbaru.
    Album dengan grouped_id yang sama dihitung sebagai satu postingan.
    Return urutan lama ke baru untuk pengiriman.
    """
    if source_entity is None:
        return []

    messages = []

    async for message in client.iter_messages(
        source_entity,
        limit=100,
    ):
        messages.append(message)

    posts = {}
    for message in messages:
        if message.grouped_id:
            key = ("album", message.grouped_id)
        else:
            key = ("message", message.id)

        if key not in posts:
            posts[key] = {
                "ids": [],
                "latest_id": message.id,
            }

        posts[key]["ids"].append(message.id)
        posts[key]["latest_id"] = max(
            posts[key]["latest_id"], message.id
        )

    ordered = sorted(
        posts.values(),
        key=lambda post: post["latest_id"],
        reverse=True,
    )

    selected = ordered[:int(config["posts_per_round"])]

    # Urutan kronologis; ID album juga diurutkan.
    selected.reverse()
    for post in selected:
        post["ids"].sort()

    return selected


# ---------- FORWARDING ----------

async def send_post_to_target(post, target_entity):
    """
    Kirim satu postingan atau satu album.
    Jika terkena FloodWait, ikuti waktu tunggu yang diminta Telegram.
    """
    while running:
        try:
            await client.forward_messages(
                entity=target_entity,
                messages=post["ids"],
                from_peer=source_entity,
            )
            return True

        except FloodWaitError as exc:
            log_event(
                "WARNING",
                f"FloodWait {exc.seconds} detik. Pengiriman ditunda.",
            )
            await asyncio.sleep(exc.seconds)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log_event(
                "ERROR",
                f"❌ Gagal kirim ke {target_label(target_entity)}: {exc}",
            )
            return False

    return False


async def send_batch(batch, posts, batch_number):
    global current_activity

    if not batch:
        return 0, 0

    success = 0
    failed = 0

    current_activity = (
        f"Batch {batch_number}: mengirim ke {len(batch)} target"
    )
    log_event(
        "INFO",
        f"Batch {batch_number} dimulai; target={len(batch)}, "
        f"postingan={len(posts)}.",
    )

    for target_entity in batch:
        if not running:
            break

        target_name = target_label(target_entity)
        current_activity = f"Batch {batch_number}: target {target_name}"

        target_ok = True

        for post in posts:
            if not running:
                break

            result = await send_post_to_target(post, target_entity)
            if not result:
                target_ok = False

        if not running:
            break

        if target_ok:
            success += 1
            log_event(
                "INFO",
                f"✅ {target_name}: semua postingan berhasil dikirim.",
            )
        else:
            failed += 1
            log_event(
                "WARNING",
                f"❌ {target_name}: ada postingan yang gagal.",
            )

    log_event(
        "INFO",
        f"Batch {batch_number} selesai. Berhasil={success}, gagal={failed}.",
    )
    return success, failed


async def run_round():
    global current_round, current_activity, last_round_result

    current_round += 1
    round_number = current_round
    current_activity = "Mengambil postingan terbaru"

    log_event("INFO", f"Putaran #{round_number} dimulai.")

    posts = await get_latest_posts()

    if not posts:
        last_round_result = "Tidak ada postingan yang bisa diproses"
        log_event("WARNING", last_round_result)
        return

    targets = target_entities[:]

    # Bagi menjadi dua batch; jika ganjil, batch pertama mendapat satu
    # target tambahan.
    midpoint = (len(targets) + 1) // 2
    batch1 = targets[:midpoint]
    batch2 = targets[midpoint:]

    success1, failed1 = await send_batch(batch1, posts, 1)

    if not running:
        last_round_result = "Dijeda sebelum batch berikutnya"
        return

    if batch2:
        pause_seconds = int(config["batch_pause"]) * 60
        current_activity = (
            f"Jeda {config['batch_pause']} menit sebelum batch 2"
        )
        log_event(
            "INFO",
            f"Menunggu {config['batch_pause']} menit sebelum batch 2.",
        )
        await asyncio.sleep(pause_seconds)

        if not running:
            last_round_result = "Dijeda sebelum batch 2"
            return

    success2, failed2 = await send_batch(batch2, posts, 2)

    total_success = success1 + success2
    total_failed = failed1 + failed2

    last_round_result = (
        f"{total_success} target berhasil, {total_failed} target gagal"
    )
    current_activity = "Putaran selesai"

    log_event(
        "INFO",
        f"Putaran #{round_number} selesai. {last_round_result}.",
    )


async def loop_worker():
    global running, loop_task, next_run_at, current_activity

    try:
        while running:
            await run_round()

            if not running:
                break

            low = int(config["interval_min"])
            high = int(config["interval_max"])
            delay_minutes = randint(low, high)

            next_run_at = datetime.now() + timedelta(
                minutes=delay_minutes
            )
            current_activity = (
                f"Menunggu putaran berikutnya ({delay_minutes} menit)"
            )

            log_event(
                "INFO",
                f"Putaran berikutnya dijadwalkan dalam "
                f"{delay_minutes} menit.",
            )

            await asyncio.sleep(delay_minutes * 60)

    except asyncio.CancelledError:
        log_event("INFO", "Tugas looping dibatalkan.")
        raise

    except Exception as exc:
        log_event("ERROR", f"Loop error: {exc}")
        await notify_admin(f"⚠️ Loop error: {exc}")

    finally:
        if loop_task is asyncio.current_task():
            running = False
            loop_task = None
            next_run_at = None
            current_activity = "Tidak aktif"


# ---------- START / PAUSE ----------

async def start_forwarding():
    global running, loop_task, source_entity, target_entities

    if running:
        return "Forwarding sudah berjalan."

    if not client.is_connected():
        return "UBOT belum tersambung."

    source = str(config.get("source", "")).strip()
    targets = parse_targets(config.get("target", ""))

    if not source:
        return "Atur source terlebih dahulu: /source @channel"

    if not targets:
        return "Atur target terlebih dahulu: /target @grup1 @grup2"

    if len(targets) > MAX_TARGETS:
        return f"Maksimal {MAX_TARGETS} target."

    try:
        posts_count = int(config["posts_per_round"])
        low = int(config["interval_min"])
        high = int(config["interval_max"])
        batch_pause = int(config["batch_pause"])

        if not 1 <= posts_count <= MAX_POSTS:
            return "Jumlah postingan harus 1–3."
        if not (1 <= low <= high <= MAX_INTERVAL_MINUTES):
            return "Rentang interval tidak valid."
        if not 0 <= batch_pause <= 1440:
            return "Jeda batch harus 0–1440 menit."

        source_entity = await client.get_entity(source)

        resolved_targets = []
        for target in targets:
            resolved_targets.append(await client.get_entity(target))

        target_entities = resolved_targets

        # Tes akses source sebelum menjalankan looping.
        await client.get_messages(source_entity, limit=1)

        running = True
        current_activity = "Memulai"
        loop_task = asyncio.create_task(loop_worker())

        log_event(
            "INFO",
            f"Looping dimulai. Source={source}; target={len(targets)}; "
            f"postingan={posts_count}.",
        )

        return "Looping aktif. Putaran pertama segera dimulai."

    except Exception as exc:
        running = False
        log_event("ERROR", f"Start gagal: {exc}")
        return f"Gagal memulai: {exc}"


async def pause_forwarding():
    global running, loop_task, next_run_at, current_activity

    if not running and loop_task is None:
        return "Forwarding sudah dijeda."

    running = False
    next_run_at = None
    current_activity = "Menghentikan tugas"

    task = loop_task
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    loop_task = None
    current_activity = "Dijeda"
    log_event("INFO", "Forwarding dijeda oleh admin.")

    return "Forwarding dijeda."


# ---------- PERINTAH ADMIN ----------

@dp.message(Command("start"))
async def command_start(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    await show_panel(message)


@dp.message(Command("source"))
async def command_source(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Contoh: /source @postmeostore")
        return

    if running:
        await message.answer("Pause forwarding sebelum mengganti source.")
        return

    config["source"] = parts[1].strip()
    save_config()
    log_event("INFO", f"Source diubah menjadi {config['source']}")
    await message.answer(f"Source disimpan: {config['source']}")


@dp.message(Command("target"))
async def command_target(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Contoh: /target @grup1 @grup2 @grup3"
        )
        return

    if running:
        await message.answer("Pause forwarding sebelum mengganti target.")
        return

    targets = parse_targets(parts[1])

    if not targets:
        await message.answer("Minimal satu target wajib diatur.")
        return

    if len(targets) > MAX_TARGETS:
        await message.answer(f"Maksimal {MAX_TARGETS} target.")
        return

    config["target"] = " ".join(targets)
    save_config()
    log_event("INFO", f"Target diperbarui: {', '.join(targets)}")
    await message.answer(f"Target disimpan:\n{', '.join(targets)}")


@dp.message(Command("posts"))
async def command_posts(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Gunakan: /posts 1, /posts 2, atau /posts 3")
        return

    if running:
        await message.answer("Pause forwarding sebelum mengubah jumlah postingan.")
        return

    amount = int(parts[1])
    if not 1 <= amount <= MAX_POSTS:
        await message.answer("Jumlah postingan hanya boleh 1–3.")
        return

    config["posts_per_round"] = amount
    save_config()
    log_event("INFO", f"Jumlah postingan per putaran: {amount}")
    await message.answer(f"Postingan per putaran diatur ke {amount}.")


@dp.message(Command("interval"))
async def command_interval(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()

    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Contoh: /interval 25 40")
        return

    if running:
        await message.answer("Pause forwarding sebelum mengubah interval.")
        return

    low, high = int(parts[1]), int(parts[2])

    if not (1 <= low <= high <= MAX_INTERVAL_MINUTES):
        await message.answer(
            "Interval harus 1–1440 menit, dan nilai minimum "
            "tidak boleh lebih besar dari maksimum."
        )
        return

    config["interval_min"] = low
    config["interval_max"] = high
    save_config()

    log_event("INFO", f"Interval diubah: {low}–{high} menit")
    await message.answer(
        f"Jeda antarputaran: acak antara {low} dan {high} menit."
    )


@dp.message(Command("batchpause"))
async def command_batchpause(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Contoh: /batchpause 5")
        return

    if running:
        await message.answer("Pause forwarding sebelum mengubah jeda batch.")
        return

    minutes = int(parts[1])

    if not 0 <= minutes <= 1440:
        await message.answer("Jeda batch harus 0–1440 menit.")
        return

    config["batch_pause"] = minutes
    save_config()
    log_event("INFO", f"Jeda antarbatch diubah: {minutes} menit")
    await message.answer(f"Jeda antarbatch diatur ke {minutes} menit.")


@dp.message(Command("status"))
async def command_status(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    await show_panel(message)


@dp.message(Command("logs"))
async def command_logs(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    rows = get_recent_logs(10)

    if not rows:
        await message.answer("Belum ada log.")
        return

    lines = ["🧾 LOG TERBARU", "━━━━━━━━━━━━━━━━━━"]

    for created_at, level, text in rows:
        clock = created_at[-8:-3]
        lines.append(f"[{clock}] [{level}] {text}")

    output = "\n".join(lines)

    # Batas aman panjang pesan Telegram.
    if len(output) > 3900:
        output = output[-3900:]

    await message.answer(output)


# ---------- TOMBOL PANEL ----------

@dp.callback_query(F.data == "forward_logs")
async def callback_logs(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Tidak diizinkan.", show_alert=True)
        return

    await callback.answer()

    rows = get_recent_logs(10)
    if not rows:
        text = "Belum ada log."
    else:
        lines = ["🧾 LOG TERBARU", "━━━━━━━━━━━━━━━━━━"]
        for created_at, level, message in rows:
            clock = created_at[-8:-3]
            lines.append(f"[{clock}] [{level}] {message}")
        text = "\n".join(lines)

    if callback.message:
        await callback.message.answer(text[-3900:])


@dp.callback_query(F.data == "forward_start")
async def callback_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Tidak diizinkan.", show_alert=True)
        return

    result = await start_forwarding()
    await callback.answer(result[:180], show_alert=True)

    if callback.message:
        await callback.message.answer(
            panel_text(),
            reply_markup=build_menu(),
        )


@dp.callback_query(F.data == "forward_pause")
async def callback_pause(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Tidak diizinkan.", show_alert=True)
        return

    result = await pause_forwarding()
    await callback.answer(result[:180], show_alert=True)

    if callback.message:
        await callback.message.answer(
            panel_text(),
            reply_markup=build_menu(),
        )


# ---------- MAIN ----------

async def main():
    try:
        await client.connect()

        if not await client.is_user_authorized():
            log_event("ERROR", "UBOT belum login. Jalankan login.py.")
            print("UBOT belum login. Jalankan login.py terlebih dahulu.")
            return

        account = await client.get_me()
        log_event("INFO", f"UBOT aktif: {account.first_name}")
        print(f"UBOT aktif: {account.first_name}")
        print("Panel bot dimulai.")

        await dp.start_polling(bot)

    finally:
        await pause_forwarding()

        if client.is_connected():
            await client.disconnect()

        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())