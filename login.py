import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]


async def main():
    client = TelegramClient("ubot", API_ID, API_HASH)

    await client.start()
    account = await client.get_me()

    print(f"Login berhasil: {account.first_name}")
    print(f"User ID: {account.id}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())