"""
Viber channel — official Viber Bot API (public account). Outbound REST, inbound
webhook. There is no WAHA-equivalent for personal Viber accounts; this adapter
uses the Bot API and stays inert ("sleeping") until a bot_token is configured.

Credentials (accounts table): {
  "bot_token": "...", "sender_name": "СВЮ.КЛУБ", "sender_avatar": "<url>",
  "webhook_secret": "...", "escalation_peer": "<viber user id>"
}
Inbound at POST /webhooks/viber/{account_id}?token=<webhook_secret>.
NOTE: Viber requires a public HTTPS webhook (PUBLIC_URL must be reachable over HTTPS),
and customers must have messaged the bot first (Viber can't cold-initiate).
"""
import logging
import os

import httpx

from src import accounts as account_manager
from src.channels.base import ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
VIBER_API = "https://chatapi.viber.com"


class ViberAdapter(ChannelAdapter):
    channel = "viber"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        self.token = self.credentials.get("bot_token", "") or ""
        self.sender_name = self.credentials.get("sender_name", "") or "СВЮ.КЛУБ"
        self.sender_avatar = self.credentials.get("sender_avatar", "") or None
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
    async def handle_webhook(self, payload: dict) -> None:
        event = (payload or {}).get("event")
        if event not in ("message", "conversation_started"):
            return  # webhook verification / delivered / seen / subscribed — ignore
        sender = payload.get("sender", {}) or {}
        peer = sender.get("id", "")
        if not peer:
            return
        message = payload.get("message", {}) or {}
        external_id = str(payload.get("message_token", ""))
        if self.is_duplicate(external_id):
            return
        msg = InboundMessage(
            channel="viber", account_id=self.account_id, peer=peer,
            text=message.get("text", "") or "",
            sender_name=sender.get("name", "") or "", external_id=external_id, raw=payload,
        )
        await self._on_inbound(msg)

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
        # Viber sends media by PUBLIC URL only (no raw upload).
        if not (isinstance(file, str) and file.startswith(("http://", "https://"))):
            return OutboundResult(
                ok=False, error="Viber вимагає публічний URL файлу (не байти/локальний шлях)")
        try:
            body = {"receiver": peer, "type": "file", "sender": self._sender(),
                    "media": file, "file_name": filename or "file"}
            r = await self._client().post("/pa/send_message", json=body)
            data = r.json()
            if data.get("status") == 0:
                if caption:
                    await self.send_text(peer, caption)
                return OutboundResult(ok=True)
            return OutboundResult(ok=False, error=data.get("status_message", "send failed"))
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))
