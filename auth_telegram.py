"""Run this script manually to authorize Telegram session."""
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient

async def main():
    client = TelegramClient(
        "session/svy_agent",
        int(os.getenv("TG_API_ID")),
        os.getenv("TG_API_HASH"),
    )
    await client.start(phone=os.getenv("TG_PHONE"))
    me = await client.get_me()
    print(f"Authorized as: {me.first_name} {me.phone}")
    await client.disconnect()

asyncio.run(main())
