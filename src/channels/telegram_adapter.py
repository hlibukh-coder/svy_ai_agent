"""
Telegram channel adapter (Telethon), multi-account.

- Legacy account id=1 keeps the existing file session (session/svy_agent) and, on
  auth, wires the module globals + proactive scheduler exactly like the old code,
  so nothing about the single-account behavior changes.
- New accounts use a StringSession persisted (encrypted) in the accounts table.
- QR login lives here; tg_auth.py is a thin per-account dispatcher onto these methods.
"""
import asyncio
import io
import logging
import os

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import User

from src import accounts as account_manager, tools
from src.channels.base import ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
MANAGER_TG_ID = int(os.getenv("MANAGER_TG_ID", "0") or "0")
ESCALATION_CHAT_ID = os.getenv("ESCALATION_CHAT_ID", "")

_TWO_GB = 2 * 1024 * 1024 * 1024


def _qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


class TelegramAdapter(ChannelAdapter):
    channel = "telegram"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        self.client: TelegramClient | None = None
        self._qr = None
        self._lock = asyncio.Lock()

    # ── identity / session ───────────────────────────────────────────────────
    def _is_legacy(self) -> bool:
        return (self.account_id == account_manager.LEGACY_TG_ACCOUNT_ID
                or bool((self.meta or {}).get("legacy_session")))

    def _make_client(self) -> TelegramClient:
        if self._is_legacy():
            path = (self.meta or {}).get("legacy_session") or account_manager.LEGACY_TG_SESSION
            return TelegramClient(path, TG_API_ID, TG_API_HASH)
        blob = self.session_blob if isinstance(self.session_blob, str) else None
        return TelegramClient(StringSession(blob), TG_API_ID, TG_API_HASH)

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self.client = self._make_client()
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self._activate(self.client)
        else:
            await account_manager.update_status(self.account_id, "disconnected")
            logger.info(f"[TG:{self.account_id}] not authorized — awaiting QR login")

    async def stop(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _activate(self, client: TelegramClient) -> None:
        """Wire an AUTHORIZED client: inbound handler, status, and (legacy only) the
        module globals + proactive scheduler."""
        self.client = client
        client.add_event_handler(self._on_event, events.NewMessage(incoming=True))
        try:
            me = await client.get_me()
            await account_manager.update_account(
                self.account_id, meta={"name": me.first_name, "phone": f"+{me.phone}"})
        except Exception:
            pass
        await account_manager.update_status(self.account_id, "authorized")

        # Persist the StringSession for non-legacy accounts (legacy uses its file).
        if not self._is_legacy() and isinstance(client.session, StringSession):
            try:
                await account_manager.save_session(self.account_id, client.session.save())
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] save_session failed: {e}")

        if self._is_legacy():
            from src import index, scheduler, config
            index._tg_client = client
            esc = int(ESCALATION_CHAT_ID) if ESCALATION_CHAT_ID else None
            tools.set_tg_client(client, esc)
            try:
                send_hour = int(await config.get_value("send_hour", 10) or 10)
                scheduler.start(client, send_hour)
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] scheduler start failed: {e}")
        logger.info(f"[TG:{self.account_id}] activated (handler + status)")

    # ── inbound ──────────────────────────────────────────────────────────────
    async def _on_event(self, event) -> None:
        try:
            sender = await event.get_sender()
            if not sender or not isinstance(sender, User):
                return
            if MANAGER_TG_ID and sender.id == MANAGER_TG_ID:
                return
            if not event.is_private:
                return
            phone = ""
            if sender.phone:
                phone = sender.phone if str(sender.phone).startswith("+") else f"+{sender.phone}"
            msg = InboundMessage(
                channel="telegram", account_id=self.account_id, peer=str(event.chat_id),
                text=(event.raw_text or "").strip(),
                sender_phone=phone, sender_name=getattr(sender, "first_name", "") or "",
                external_id=str(event.id),
            )
            await self._on_inbound(msg)
        except Exception as e:
            logger.error(f"[TG:{self.account_id}] on_event error: {e}")

    # ── outbound ─────────────────────────────────────────────────────────────
    async def send_text(self, peer: str, text: str) -> OutboundResult:
        if not self.client:
            return OutboundResult(ok=False, error="telegram not connected")
        try:
            await self.client.send_message(int(peer), text)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        if not self.client:
            return OutboundResult(ok=False, error="telegram not connected")
        try:
            f = file
            if isinstance(file, (bytes, bytearray)):
                f = io.BytesIO(file)
                f.name = filename or "file"
            force_doc = not (mimetype or "").startswith("image/")
            await self.client.send_file(int(peer), f, caption=caption or None,
                                        force_document=force_doc)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    async def send_reply(self, peer: str, reply: str) -> None:
        """Several short messages with typing indicators, like a real manager."""
        from src.index import _split_reply
        parts = _split_reply(reply)
        if not parts or not self.client:
            return
        pid = int(peer)
        for i, part in enumerate(parts):
            if i:
                try:
                    async with self.client.action(pid, "typing"):
                        await asyncio.sleep(min(2.0, 0.5 + len(part) / 140))
                except Exception:
                    pass
            try:
                await self.client.send_message(pid, part)
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] send part failed: {e}")

    # ── capabilities ─────────────────────────────────────────────────────────
    def supports_typing(self) -> bool:
        return True

    def max_file_bytes(self) -> int:
        return _TWO_GB

    async def healthcheck(self) -> dict:
        if not self.client:
            return {"status": "disconnected"}
        try:
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                return {"status": "authorized", "name": me.first_name, "phone": f"+{me.phone}"}
        except Exception:
            pass
        return {"status": "disconnected"}

    # ── QR login (driven by tg_auth dispatcher) ──────────────────────────────
    async def begin_qr(self) -> dict:
        async with self._lock:
            if self.client is None or not self.client.is_connected():
                self.client = self._make_client()
                await self.client.connect()
            if await self.client.is_user_authorized():
                await self._activate(self.client)
                return {"status": "authorized"}
            await account_manager.update_status(self.account_id, "connecting")
            self._qr = await self.client.qr_login()
            return {"status": "waiting", "svg": _qr_svg(self._qr.url)}

    async def qr_poll(self) -> dict:
        async with self._lock:
            if self.client is None or self._qr is None:
                return {"status": "disconnected"}
            # A scan may have completed during a previous poll (Telethon's wait()
            # can time out at the exact moment the login token is imported). If the
            # connection is already authorized, finalize instead of recreating.
            try:
                if await self.client.is_user_authorized():
                    await self._activate(self.client)
                    self._qr = None
                    return {"status": "authorized"}
            except Exception:
                pass
            try:
                done = await self._qr.wait(timeout=2)
                if done:
                    await self._activate(self.client)
                    self._qr = None
                    return {"status": "authorized"}
            except SessionPasswordNeededError:
                return {"status": "password"}
            except asyncio.TimeoutError:
                try:
                    self._qr = await self.client.qr_login()
                    return {"status": "waiting", "svg": _qr_svg(self._qr.url)}
                except AttributeError:
                    # ExportLoginToken returned LoginTokenSuccess (no .token) → the
                    # scan actually succeeded; finalize the login.
                    if await self.client.is_user_authorized():
                        await self._activate(self.client)
                        self._qr = None
                        return {"status": "authorized"}
                    return {"status": "waiting"}
                except Exception as e:
                    logger.error(f"[TG:{self.account_id}] QR recreate failed: {e}")
                    return {"status": "waiting"}
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] QR poll error: {e}")
                return {"status": "waiting"}
            return {"status": "waiting"}

    async def qr_submit_password(self, password: str) -> dict:
        async with self._lock:
            if self.client is None:
                return {"status": "disconnected"}
            try:
                await self.client.sign_in(password=password)
                await self._activate(self.client)
                self._qr = None
                return {"status": "authorized"}
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] password sign-in failed: {e}")
                return {"status": "password", "error": str(e)}
