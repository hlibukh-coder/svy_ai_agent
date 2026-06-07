"""
QR-code Telegram authorization — no SMS needed.
1. Run: python auth_qr.py
2. Open Telegram on your phone → Settings → Devices → Link Desktop Device
3. Scan the QR code shown in terminal
4. Session will be saved to session/svy_agent.session
"""
import asyncio
import os
import qrcode
from dotenv import load_dotenv

load_dotenv()

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def main():
    os.makedirs("session", exist_ok=True)

    client = TelegramClient(
        "session/svy_agent",
        int(os.getenv("TG_API_ID")),
        os.getenv("TG_API_HASH"),
    )

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as: {me.first_name} (+{me.phone})")
        await client.disconnect()
        return

    print("Starting QR login...")
    print("Open Telegram -> Settings -> Devices -> Link Desktop Device -> scan QR\n")

    qr_login = await client.qr_login()

    # Print QR in terminal as ASCII
    qr = qrcode.QRCode()
    qr.add_data(qr_login.url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(f"\nOr open this URL: {qr_login.url}\n")

    # Wait for scan (up to 60 seconds, auto-refreshes)
    try:
        await qr_login.wait(timeout=60)
    except SessionPasswordNeededError:
        # 2FA enabled
        password = input("2FA password: ")
        await client.sign_in(password=password)
    except Exception as e:
        print(f"QR login failed: {e}")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"Authorized as: {me.first_name} (+{me.phone})")
    print("Session saved to session/svy_agent.session")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
