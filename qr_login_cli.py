"""Controlled QR login for the agent's Telegram session (session/svy_agent).

Saves the QR as /tmp/tg_qr.png, waits for a scan, finalizes the login and
persists the session. Prints a single RESULT line the caller can parse.

Run the server STOPPED during this (so the session file is free). Env:
  QR_TIMEOUT   per-token wait seconds (default: derived from token expiry)
  TG_2FA_PASSWORD  optional cloud password if 2FA is on
"""
import asyncio, os, sys, datetime
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import qrcode

AID = int(os.getenv("TG_API_ID")); AH = os.getenv("TG_API_HASH")
PW = os.getenv("TG_2FA_PASSWORD", "")
PNG = "/tmp/tg_qr.png"


def render(url: str):
    img = qrcode.make(url, box_size=10, border=3)
    img.save(PNG)


async def main():
    c = TelegramClient("session/svy_agent", AID, AH)
    await c.connect()
    if await c.is_user_authorized():
        me = await c.get_me()
        print(f"RESULT ALREADY_OK name={me.first_name} phone=+{me.phone}", flush=True)
        await c.disconnect(); return

    qr = await c.qr_login()
    render(qr.url)
    try:
        exp = (qr.expires - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    except Exception:
        exp = 30
    wait_s = int(os.getenv("QR_TIMEOUT", str(max(20, min(55, int(exp) - 2)))))
    print(f"QR_READY png={PNG} window_s={wait_s}", flush=True)

    try:
        await qr.wait(timeout=wait_s)
        me = await c.get_me()
        print(f"RESULT OK name={me.first_name} last={me.last_name or ''} phone=+{me.phone} user=@{me.username or '-'}", flush=True)
    except SessionPasswordNeededError:
        if PW:
            try:
                await c.sign_in(password=PW)
                me = await c.get_me()
                print(f"RESULT OK_2FA name={me.first_name} phone=+{me.phone}", flush=True)
            except Exception as e:
                print(f"RESULT PW_FAIL {type(e).__name__}: {e}", flush=True)
        else:
            print("RESULT NEED_2FA", flush=True)
    except asyncio.TimeoutError:
        print("RESULT TIMEOUT", flush=True)
    except Exception as e:
        print(f"RESULT ERR {type(e).__name__}: {e}", flush=True)
    finally:
        await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
