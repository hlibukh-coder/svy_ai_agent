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

from src.tg_app import TG_API_ID, TG_API_HASH  # baked-in app creds, env overrides
MANAGER_TG_ID = int(os.getenv("MANAGER_TG_ID", "0") or "0")
ESCALATION_CHAT_ID = os.getenv("ESCALATION_CHAT_ID", "")

_TWO_GB = 2 * 1024 * 1024 * 1024

# Message ids sent PROGRAMMATICALLY (AI reply / operator / campaigns / escalation).
# The outgoing-event handler skips these so only messages typed by the owner from
# the phone/Telegram app get recorded as "human wrote directly".
from collections import deque
_sent_ids: deque = deque(maxlen=500)


def mark_sent(*messages) -> None:
    """Register message(s) returned by Telethon send_* as programmatic."""
    for m in messages:
        if isinstance(m, (list, tuple)):
            mark_sent(*m)
            continue
        mid = getattr(m, "id", None)
        if mid is not None:
            _sent_ids.append(mid)


def _display_name(ent) -> str:
    """Human name of a Telegram user entity: 'First Last', else @username."""
    full = " ".join(x for x in (getattr(ent, "first_name", "") or "",
                                getattr(ent, "last_name", "") or "") if x).strip()
    return full or (getattr(ent, "username", "") or "")


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
        self._me_id: int | None = None

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
        client.add_event_handler(self._on_outgoing, events.NewMessage(outgoing=True))
        try:
            me = await client.get_me()
            self._me_id = me.id
            await account_manager.update_account(
                self.account_id, meta={"name": me.first_name, "phone": f"+{me.phone}"})
        except Exception:
            pass
        await account_manager.update_status(self.account_id, "authorized")
        # Resolve real names for old conversations that still show as bare IDs.
        asyncio.create_task(self._backfill_names())

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
    async def _download_inbound_media(self, message) -> list:
        """Download the photo/document a client sent into FILES_DIR so the operator
        can open/save it from the dashboard. Returns [Attachment] or []."""
        from src import files as file_store
        from src.channels.base import Attachment
        if not (getattr(message, "photo", None) or getattr(message, "document", None)):
            return []  # webpage previews / geo etc. — nothing to download
        doc = getattr(message, "document", None)
        size = getattr(doc, "size", 0) if doc else 0
        if size and size > file_store.MAX_INBOUND_BYTES:
            logger.warning(f"[TG:{self.account_id}] inbound file too big ({size}b) — skipped")
            return []
        try:
            path = await message.download_media(file=file_store.files_dir())
            if not path:
                return []
            import mimetypes
            mime = ((getattr(doc, "mime_type", "") if doc else "")
                    or mimetypes.guess_type(path)[0]
                    or ("image/jpeg" if getattr(message, "photo", None) else "application/octet-stream"))
            return [Attachment(filename=os.path.basename(path), mimetype=mime, path=path)]
        except Exception as e:
            logger.error(f"[TG:{self.account_id}] media download failed: {e}")
            return []

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
            text = (event.raw_text or "").strip()
            attachments = await self._download_inbound_media(event.message)
            contact = getattr(event.message, "contact", None)
            if contact is not None:
                text = (text + f"\n[контакт: {getattr(contact, 'first_name', '') or ''} "
                               f"{getattr(contact, 'phone_number', '') or ''}]").strip()
            if not text and not attachments and getattr(event.message, "media", None):
                # media we couldn't download — must still show in the chat
                text = "[фото]" if getattr(event.message, "photo", None) else "[файл]"
            msg = InboundMessage(
                channel="telegram", account_id=self.account_id, peer=str(event.chat_id),
                text=text,
                sender_phone=phone, sender_name=_display_name(sender),
                external_id=str(event.id), attachments=attachments,
            )
            await self._on_inbound(msg)
        except Exception as e:
            logger.error(f"[TG:{self.account_id}] on_event error: {e}")

    async def _on_outgoing(self, event) -> None:
        """Messages the OWNER sends from the phone/Telegram app (not via the agent):
        record them so the dashboard shows the full conversation, and pause the AI
        in that chat — a human is clearly handling it. Programmatic sends are
        filtered out via the mark_sent registry."""
        try:
            if not event.is_private:
                return
            if event.id in _sent_ids:
                return  # sent by AI/operator/campaign through this app
            if self._me_id and event.chat_id == self._me_id:
                return  # saved messages (chat with self)
            if MANAGER_TG_ID and event.chat_id == MANAGER_TG_ID:
                return  # escalation chat with the manager
            text = (event.raw_text or "").strip()
            if not text and getattr(event.message, "media", None):
                text = "[фото]" if getattr(event.message, "photo", None) else "[файл]"
            if not text:
                return
            from src import context
            conv_id = f"telegram:{self.account_id}:{event.chat_id}"
            await context.save_message(conv_id=conv_id, role="assistant", content=text)
            await context.set_chat_ai_paused(conv_id=conv_id, paused=True)
            logger.info(f"[TG:{self.account_id}] phone-sent message recorded → {conv_id}")
        except Exception as e:
            logger.error(f"[TG:{self.account_id}] on_outgoing error: {e}")

    async def _backfill_names(self, only_missing: bool = True) -> int:
        """Resolve real Telegram names/phones for conversations that still show as
        'ID …'. Uses iter_dialogs() — which returns full entities (name + phone +
        access_hash) — instead of get_entity(int(id)), which FAILS on a bare numeric
        id when the session has no cached access_hash (e.g. a session copied to
        another machine — the exact reason names weren't filling in). Returns the
        number of contacts updated. Requires the account to be connected."""
        from src import context
        if not self.client:
            return 0
        try:
            if only_missing:
                need = {str(context.parse_conv_id(c)[2])
                        for c in await context.telegram_convs_without_name(self.account_id)}
                if not need:
                    return 0
            else:
                need = None  # refresh every dialog
            fixed = 0
            async for dialog in self.client.iter_dialogs(limit=None):
                ent = dialog.entity
                if not isinstance(ent, User):
                    continue
                did = str(getattr(ent, "id", ""))
                if need is not None and did not in need:
                    continue
                name = _display_name(ent)
                phone = getattr(ent, "phone", "") or ""
                if phone and not phone.startswith("+"):
                    phone = f"+{phone}"
                if name or phone:
                    await context.upsert_contact_profile(
                        f"telegram:{self.account_id}:{did}", name=name, phone=phone)
                    fixed += 1
            logger.info(f"[TG:{self.account_id}] contact names backfilled: {fixed}"
                        f"{('/' + str(len(need))) if need is not None else ''}")
            return fixed
        except Exception as e:
            logger.error(f"[TG:{self.account_id}] name backfill error: {e}")
            return 0

    # ── outbound ─────────────────────────────────────────────────────────────
    @staticmethod
    def _send_error(e: Exception) -> str:
        # AUTH_KEY_UNREGISTERED = сесію розлогінено (Telegram деавторизував пристрій)
        if "key is not registered" in str(e).lower():
            return ("Telegram не підключено (сесію розлогінено) — "
                    "Налаштування → «Підключити через QR», відскануйте телефоном")
        return str(e)

    async def send_text(self, peer: str, text: str) -> OutboundResult:
        if not self.client:
            return OutboundResult(ok=False, error="telegram not connected")
        try:
            m = await self.client.send_message(int(peer), text)
            mark_sent(m)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=self._send_error(e))

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        if not self.client:
            return OutboundResult(ok=False, error="telegram not connected")
        try:
            f = file
            if isinstance(file, (bytes, bytearray)):
                f = io.BytesIO(file)
                f.name = filename or "file"
            force_doc = not (mimetype or "").startswith("image/")
            m = await self.client.send_file(int(peer), f, caption=caption or None,
                                            force_document=force_doc)
            mark_sent(m)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=self._send_error(e))

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
                m = await self.client.send_message(pid, part)
                mark_sent(m)
            except Exception as e:
                logger.error(f"[TG:{self.account_id}] send part failed: {e}")

    async def send_reaction(self, peer: str, external_id: str, emoji: str) -> OutboundResult:
        if not self.client:
            return OutboundResult(ok=False, error="telegram not connected")
        try:
            from telethon.tl import functions, types
            reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
            await self.client(functions.messages.SendReactionRequest(
                peer=int(peer), msg_id=int(external_id), reaction=reaction))
            return OutboundResult(ok=True)
        except Exception as e:
            if "REACTION_INVALID" in str(e):
                return OutboundResult(
                    ok=False, error="Telegram не приймає цю реакцію — спробуйте 👍 ❤️ 🔥 🎉 🙏")
            return OutboundResult(ok=False, error=self._send_error(e))

    # ── capabilities ─────────────────────────────────────────────────────────
    def supports_typing(self) -> bool:
        return True

    def supports_reactions(self) -> bool:
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
