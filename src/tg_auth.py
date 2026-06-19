"""
QR-login for Telegram from the dashboard.

Flow (frontend):
  1. POST /api/telegram/qr/start  → returns {svg, status}
  2. user scans QR with Telegram mobile (Settings → Devices → Link Desktop Device)
  3. frontend polls GET /api/telegram/qr/poll every ~2s:
       - {status:"waiting", svg?}  (svg present if the token was refreshed)
       - {status:"password"}       (2FA — show password field)
       - {status:"authorized"}     (done; client activated)
  4. if password: POST /api/telegram/qr/password {password}

The QR token expires every ~30s; poll() recreates it and returns a fresh svg.
"""
import asyncio
import io
import logging
import os

import qrcode
import qrcode.image.svg
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

logger = logging.getLogger(__name__)

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")

_client: TelegramClient | None = None
_qr = None
_lock = asyncio.Lock()


def _qr_svg(url: str) -> str:
    """Render the login URL as an inline SVG string (no Pillow needed)."""
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(url, image_factory=factory, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


async def status() -> dict:
    """Is a Telegram client currently authorized and running?"""
    from src import index
    client = index._tg_client
    if client is None:
        return {"status": "disconnected"}
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            return {"status": "authorized", "name": me.first_name, "phone": f"+{me.phone}"}
    except Exception:
        pass
    return {"status": "disconnected"}


async def start() -> dict:
    """Begin a QR login; return the QR as SVG."""
    global _client, _qr
    async with _lock:
        # already logged in?
        from src import index
        if index._tg_client is not None:
            try:
                if await index._tg_client.is_user_authorized():
                    return {"status": "authorized"}
            except Exception:
                pass

        if _client is None:
            _client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
        if not _client.is_connected():
            await _client.connect()

        if await _client.is_user_authorized():
            await _activate()
            return {"status": "authorized"}

        _qr = await _client.qr_login()
        return {"status": "waiting", "svg": _qr_svg(_qr.url)}


async def poll() -> dict:
    """Check scan progress; refresh the QR token if it expired."""
    global _qr
    async with _lock:
        if _client is None or _qr is None:
            return {"status": "disconnected"}
        try:
            done = await _qr.wait(timeout=2)  # returns user on success, raises on timeout
            if done:
                await _activate()
                return {"status": "authorized"}
        except SessionPasswordNeededError:
            return {"status": "password"}
        except asyncio.TimeoutError:
            # token may have expired — recreate and hand back a fresh QR
            try:
                _qr = await _client.qr_login()
                return {"status": "waiting", "svg": _qr_svg(_qr.url)}
            except Exception as e:
                logger.error(f"[QR] recreate failed: {e}")
                return {"status": "waiting"}
        except Exception as e:
            logger.error(f"[QR] poll error: {e}")
            return {"status": "waiting"}
        return {"status": "waiting"}


async def submit_password(password: str) -> dict:
    """Complete a 2FA login."""
    global _client
    async with _lock:
        if _client is None:
            return {"status": "disconnected"}
        try:
            await _client.sign_in(password=password)
            await _activate()
            return {"status": "authorized"}
        except Exception as e:
            logger.error(f"[QR] password sign-in failed: {e}")
            return {"status": "password", "error": str(e)}


async def _activate():
    """Hand the now-authorized client to the app (handler + scheduler)."""
    global _client, _qr
    from src import index
    me = await _client.get_me()
    logger.info(f"[QR] authorized as {me.first_name} (+{me.phone})")
    await index.activate_client(_client, start_scheduler=True)
    _client = None  # ownership transferred to index._tg_client
    _qr = None
