"""
Convert Telegram Desktop tdata folder to Telethon session — no SMS needed.

Steps:
1. Copy your tdata folder to this project root (svy_ai_agent/tdata)
   Location of tdata:
   - Windows: C:\Users\<YOU>\AppData\Roaming\Telegram Desktop\tdata
   - Linux:   ~/.local/share/TelegramDesktop/tdata
   - macOS:   ~/Library/Application Support/Telegram Desktop/tdata

2. Run: python auth_tdata.py
3. Session will be saved to session/svy_agent.session
4. Run the bot: python -m src.index
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from src.tg_app import TG_API_ID, TG_API_HASH  # baked-in app creds, env overrides
TDATA_PATH = os.path.join(os.path.dirname(__file__), "tdata")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "session", "svy_agent")


async def main():
    if not os.path.isdir(TDATA_PATH):
        print(f"ERROR: tdata folder not found at: {TDATA_PATH}")
        print()
        print("Copy your tdata folder here:")
        print(f"  {TDATA_PATH}")
        print()
        print("tdata is located at:")
        print("  Windows: C:\\Users\\<YOU>\\AppData\\Roaming\\Telegram Desktop\\tdata")
        sys.exit(1)

    print(f"Found tdata at: {TDATA_PATH}")
    print("Converting to Telethon session...")

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        print("ERROR: opentele not installed. Run: pip install opentele --no-deps")
        sys.exit(1)

    os.makedirs("session", exist_ok=True)

    try:
        tdesk = TDesktop(TDATA_PATH)
        assert tdesk.isLoaded(), "tdata failed to load — check the folder"

        client = await tdesk.ToTelethon(
            session=SESSION_PATH,
            flag=UseCurrentSession,
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
        )

        await client.connect()
        me = await client.get_me()
        print(f"Success! Authorized as: {me.first_name} (+{me.phone})")
        print(f"Session saved to: {SESSION_PATH}.session")
        await client.disconnect()

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
