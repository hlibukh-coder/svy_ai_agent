"""
Viber channel — official Viber Bot API (public account). Outbound REST, inbound
webhook. There is no WAHA-equivalent for personal Viber accounts; this adapter
uses the Bot API and stays inert ("sleeping") until a bot_token is configured.

Credentials (accounts table): {
  "bot_token": "...", "sender_name": "СВЮ.КЛУБ", "sender_avatar": "<url>",
  "welcome_text": "...",              # greeting shown when a user opens the chat
  "webhook_secret": "...", "escalation_peer": "<viber user id>"
}
Inbound at POST /webhooks/viber/{account_id}?token=<webhook_secret>.
NOTE: Viber requires a public HTTPS webhook (PUBLIC_URL must be reachable over HTTPS),
and customers must have messaged the bot first (Viber can't cold-initiate).
Media is URL-only in both directions: inbound files come as CDN URLs (downloaded
and stored locally by the router), outbound bytes are published via our public
outbox (/files/outbox/{uuid}) so Viber can fetch them.
"""
import logging
import mimetypes
import os

import httpx

from src import accounts as account_manager
from src.channels.base import Attachment, ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
VIBER_API = "https://chatapi.viber.com"
WELCOME_DEFAULT = "Вітаємо у СВЮ.КЛУБ! 👋 Напишіть ваше питання — відповімо тут."
# Viber "picture" messages accept only these formats; anything else goes as a file.
_PICTURE_EXTS = (".jpg", ".jpeg", ".png", ".gif")


class ViberAdapter(ChannelAdapter):
    channel = "viber"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        self.token = self.credentials.get("bot_token", "") or ""
        self.sender_name = self.credentials.get("sender_name", "") or "СВЮ.КЛУБ"
        self.sender_avatar = self.credentials.get("sender_avatar", "") or None
        self.welcome_text = self.credentials.get("welcome_text", "") or WELCOME_DEFAULT
        self.webhook_secret = self.credentials.get("webhook_secret", "") or ""
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=VIBER_API, headers={"X-Viber-Auth-Token": self.token}, timeout=30)
        return self._http

    def _sender(self) -> dict:
        s = {"name": self.sender_name}
        if self.sender_avatar:
            s["avatar"] = self.sender_avatar
        return s

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.token:
            await account_manager.update_status(self.account_id, "disconnected", "no bot_token")
            logger.info(f"[VIBER:{self.account_id}] sleeping — no bot_token")
            return
        if not PUBLIC_URL.startswith("https://"):
            await account_manager.update_status(
                self.account_id, "error", "PUBLIC_URL must be public HTTPS for Viber webhook")
            logger.warning(f"[VIBER:{self.account_id}] PUBLIC_URL is not HTTPS — webhook not set")
            return
        try:
            url = f"{PUBLIC_URL}/webhooks/viber/{self.account_id}"
            if self.webhook_secret:
                url += f"?token={self.webhook_secret}"
            r = await self._client().post("/pa/set_webhook", json={
                "url": url, "event_types": ["message", "subscribed", "conversation_started"],
                "send_name": True, "send_photo": False})
            data = r.json()
            if data.get("status") == 0:
                await account_manager.update_status(self.account_id, "authorized")
                logger.info(f"[VIBER:{self.account_id}] webhook set")
            else:
                await account_manager.update_status(
                    self.account_id, "error", data.get("status_message", "set_webhook failed"))
        except Exception as e:
            await account_manager.update_status(self.account_id, "error", str(e))
            logger.error(f"[VIBER:{self.account_id}] start failed: {e}")

    async def stop(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def healthcheck(self) -> dict:
        if not self.token:
            return {"status": "disconnected", "note": "no bot_token"}
        try:
            r = await self._client().post("/pa/get_account_info", json={})
            data = r.json()
            if data.get("status") == 0:
                return {"status": "authorized", "name": data.get("name", "")}
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return {"status": "disconnected"}

    # ── inbound webhook ──────────────────────────────────────────────────────
    def _parse_message(self, message: dict) -> tuple[str, list]:
        """Normalize one Viber message object → (text, [Attachment]). Media arrives
        as CDN URLs (valid briefly) — the router downloads and stores them."""
        mtype = message.get("type", "text")
        text = (message.get("text", "") or "").strip()
        atts: list = []
        if mtype in ("picture", "video", "file") and message.get("media"):
            fname = message.get("file_name", "") or ""
            if not fname:
                ext = {"picture": ".jpg", "video": ".mp4"}.get(mtype, "")
                fname = f"{mtype}{ext}"
            mime = (mimetypes.guess_type(fname)[0]
                    or {"picture": "image/jpeg", "video": "video/mp4"}.get(mtype)
                    or "application/octet-stream")
            atts.append(Attachment(filename=fname, mimetype=mime,
                                   url=message["media"], size=message.get("size")))
        elif mtype == "sticker":
            text = text or "[стікер]"
        elif mtype == "contact":
            c = message.get("contact") or {}
            text = (text + f"\n[контакт: {c.get('name', '')} {c.get('phone_number', '')}]").strip()
        elif mtype == "location":
            loc = message.get("location") or {}
            text = (text + f"\n[локація: {loc.get('lat')}, {loc.get('lon')}]").strip()
        elif mtype == "url":
            text = text or (message.get("media", "") or "")
        return text, atts

    async def handle_webhook(self, payload: dict) -> dict | None:
        event = (payload or {}).get("event")
        if event == "conversation_started":
            # Viber shows the message object returned in the webhook HTTP response as
            # a greeting — the only way to "write first" (bots can't cold-message).
            return {"sender": self._sender(), "type": "text", "text": self.welcome_text}
        if event != "message":
            return None  # webhook verification / delivered / seen / subscribed — ignore
        sender = payload.get("sender", {}) or {}
        peer = sender.get("id", "")
        if not peer:
            return None
        message = payload.get("message", {}) or {}
        external_id = str(payload.get("message_token", ""))
        if self.is_duplicate(external_id):
            return None
        text, atts = self._parse_message(message)
        if not text and not atts:
            return None
        msg = InboundMessage(
            channel="viber", account_id=self.account_id, peer=peer,
            text=text, attachments=atts,
            sender_name=sender.get("name", "") or "", external_id=external_id, raw=payload,
        )
        await self._on_inbound(msg)
        return None

    # ── outbound ─────────────────────────────────────────────────────────────
    async def send_text(self, peer: str, text: str) -> OutboundResult:
        if not self.token:
            return OutboundResult(ok=False, error="viber_no_token")
        try:
            r = await self._client().post("/pa/send_message", json={
                "receiver": peer, "type": "text", "sender": self._sender(), "text": text})
            data = r.json()
            if data.get("status") == 0:
                return OutboundResult(ok=True, external_id=str(data.get("message_token", "")))
            return OutboundResult(ok=False, error=data.get("status_message", "send failed"))
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        if not self.token:
            return OutboundResult(ok=False, error="viber_no_token")
        # Viber sends media by PUBLIC URL only — bytes/local files are published
        # through our outbox (/files/outbox/{uuid}) and Viber fetches them from us.
        size = None
        if isinstance(file, str) and file.startswith(("http://", "https://")):
            url = file
        else:
            if not PUBLIC_URL.startswith("https://"):
                return OutboundResult(
                    ok=False,
                    error="Viber надсилає файли лише за публічним URL — "
                          "вкажіть PUBLIC_URL (https://…) у налаштуваннях сервера")
            try:
                if isinstance(file, (bytes, bytearray)):
                    data = bytes(file)
                else:
                    with open(file, "rb") as fh:
                        data = fh.read()
                    filename = filename or os.path.basename(str(file))
                from src import files as file_store
                name = file_store.outbox_put(data, filename or "file")
                url = f"{PUBLIC_URL}/files/outbox/{name}"
                size = len(data)
            except Exception as e:
                return OutboundResult(ok=False, error=str(e))
        try:
            fname = filename or "file"
            ext = os.path.splitext(fname)[1].lower()
            is_picture = ((mimetype or "").startswith("image/") and ext in _PICTURE_EXTS)
            if is_picture:
                body = {"receiver": peer, "type": "picture", "sender": self._sender(),
                        "media": url, "text": caption or ""}
            else:
                if size is None:
                    size = await self._remote_size(url)
                body = {"receiver": peer, "type": "file", "sender": self._sender(),
                        "media": url, "file_name": fname, "size": int(size or 0)}
            r = await self._client().post("/pa/send_message", json=body)
            data = r.json()
            if data.get("status") == 0:
                if caption and not is_picture:
                    await self.send_text(peer, caption)
                return OutboundResult(ok=True, external_id=str(data.get("message_token", "")))
            return OutboundResult(ok=False, error=data.get("status_message", "send failed"))
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    @staticmethod
    async def _remote_size(url: str) -> int:
        """Viber requires the byte size for type=file; probe external URLs with HEAD."""
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.head(url)
                return int(r.headers.get("content-length", 0) or 0)
        except Exception:
            return 0
