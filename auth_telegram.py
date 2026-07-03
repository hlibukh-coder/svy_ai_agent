"""Run this script manually to authorize Telegram session."""
import asyncio, os, sys, json
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

HASH_FILE = "session/.phone_code_hash"


async def main():
    phone = os.getenv("TG_PHONE")
    from src.tg_app import TG_API_ID, TG_API_HASH
    client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as: {me.first_name} {me.phone}")
        await client.disconnect()
        return

    # Step 1: send code
    if len(sys.argv) < 2:
        print(f"Sending code to {phone}...")
        result = await client.send_code_request(phone, force_sms=True)
        os.makedirs("session", exist_ok=True)
        with open(HASH_FILE, "w") as f:
            json.dump({"phone_code_hash": result.phone_code_hash}, f)
        print(f"Code type: {result.type}")
        print(f"Next type: {result.next_type}")
        print("Code sent! Now run:")
        print(f"  python auth_telegram.py <CODE>")
        print("Replace <CODE> with the code you received in Telegram/SMS.")
        await client.disconnect()
        return

    # Step 2: sign in with code
    code = sys.argv[1]
    phone_code_hash = None
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE) as f:
            phone_code_hash = json.load(f).get("phone_code_hash")

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if len(sys.argv) >= 3:
            password = sys.argv[2]
        else:
            print("2FA password required. Run:")
            print(f"  python auth_telegram.py {code} <YOUR_2FA_PASSWORD>")
            await client.disconnect()
            return
        await client.sign_in(password=password)

    if os.path.exists(HASH_FILE):
        os.remove(HASH_FILE)

    me = await client.get_me()
    print(f"Authorized as: {me.first_name} {me.phone}")
    await client.disconnect()


asyncio.run(main())
