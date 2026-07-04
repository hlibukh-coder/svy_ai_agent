"""
WhatsApp via WAHA (https://waha.devlike.pro) — REST out, webhook in.

Credentials (accounts table): {
  "base_url": "http://localhost:3000",   # WAHA server
  "api_key": "...",                      # X-Api-Key (optional, if WAHA secured)
  "session_name": "default",             # WAHA session
  "webhook_secret": "...",               # checked on inbound webhook (auto-generated)
  "escalation_peer": "<num>@c.us"        # optional per-account hand-off target
}
Inbound arrives at POST /webhooks/waha/{account_id}?token=<webhook_secret>.
"""
import base64
import logging
import os
import re

import httpx

from src import accounts as account_manager
from src.channels.base import ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
_WHATSAPP_MAX = 64 * 1024 * 1024  # WAHA/WhatsApp document cap (~64–100MB)


def _webhook_base() -> str:
    """Base URL WAHA uses to call OUR inbound webhook.

    WAHA almost always runs in Docker, where 'localhost' means the container
    itself — not the host app. So a PUBLIC_URL of http://localhost:8000 is
    unreachable from inside WAHA and inbound messages silently never arrive.
    We swap localhost/127.0.0.1 → host.docker.internal (resolves to the host on
    Docker Desktop, and on Linux when the container is started with
    --add-host=host.docker.internal:host-gateway, which our launcher does).
    Override explicitly with WAHA_WEBHOOK_BASE (e.g. a public tunnel or, under
    docker-compose, the app's service name http://svy_agent:8000).
    """
    override = os.getenv("WAHA_WEBHOOK_BASE", "").rstrip("/")
    if override:
        return override
    base = PUBLIC_URL
    for host in ("localhost", "127.0.0.1"):
        if base.startswith(f"http://{host}") or base.startswith(f"https://{host}"):
            return base.replace(host, "host.docker.internal", 1)
    return base


def phone_to_chatid(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"{digits}@c.us" if digits else (phone or "")


def chatid_to_phone(chat_id: str) -> str:
    num = (chat_id or "").split("@")[0]
    return f"+{num}" if num.isdigit() else ""


class WahaAdapter(ChannelAdapter):
    channel = "whatsapp"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        self.base_url = (self.credentials.get("base_url")
                         or os.getenv("WAHA_URL", "http://localhost:3000")).rstrip("/")
        # The new WAHA image requires an X-Api-Key; fall back to the shared env key
        # (run.sh pins WAHA_API_KEY) so a seeded account with no api_key still auths.
        self.api_key = self.credentials.get("api_key") or os.getenv("WAHA_API_KEY", "")
        self.session = self.credentials.get("session_name") or "default"
        self.webhook_secret = self.credentials.get("webhook_secret") or ""
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"X-Api-Key": self.api_key} if self.api_key else {}
            self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30)
        return self._http

    def _webhook_url(self) -> str:
        url = f"{_webhook_base()}/webhooks/waha/{self.account_id}"
        if self.webhook_secret:
            url += f"?token={self.webhook_secret}"
        return url

    def peer_for_phone(self, phone: str) -> str:
        return phone_to_chatid(phone)

    def max_file_bytes(self) -> int:
        return _WHATSAPP_MAX

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.base_url:
            await account_manager.update_status(self.account_id, "error", "no base_url")
            return
        # Auto-provision the WhatsApp session in the BACKGROUND so a QR is ready the
        # moment the operator opens Налаштування (just like Telegram) — and so a slow
        # WAHA (esp. under amd64 emulation on Apple Silicon, which can take a minute to
        # be API-ready) never blocks server startup. Retries until WAHA answers.
        await account_manager.update_status(self.account_id, "connecting")
        import asyncio
        asyncio.create_task(self._auto_provision())

    async def _auto_provision(self, attempts: int = 40, delay: float = 3.0) -> None:
        import asyncio
        for _ in range(attempts):
            try:
                status = await self._session_status()
            except Exception:
                await asyncio.sleep(delay)  # WAHA still booting — keep waiting
                continue
            if status == "WORKING":
                await self._mark_authorized()
                logger.info(f"[WAHA:{self.account_id}] session '{self.session}' already WORKING")
                return
            try:
                res = await self.begin_qr()
                logger.info(f"[WAHA:{self.account_id}] provisioned '{self.session}' "
                            f"→ {res.get('status')} (QR ready)")
                return
            except Exception as e:
                logger.warning(f"[WAHA:{self.account_id}] provision attempt failed: {e}")
                await asyncio.sleep(delay)
        await account_manager.update_status(self.account_id, "disconnected")
        logger.warning(f"[WAHA:{self.account_id}] gave up auto-provisioning (WAHA unreachable)")

    async def stop(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def _session_status(self) -> str:
        r = await self._client().get(f"/api/sessions/{self.session}")
        if r.status_code == 404:
            return "STOPPED"
        r.raise_for_status()
        data = r.json()
        return (data.get("status") or data.get("state") or "UNKNOWN").upper()

    async def _restart_session(self) -> None:
        """Kick a stuck session (FAILED) back to SCAN_QR_CODE so the QR is fresh.
        A NOWEB session drops to FAILED when a QR expires unscanned or a scan
        aborts — leaving a dead QR the operator can never link. Restarting mints
        a new pairing QR."""
        try:
            r = await self._client().post(f"/api/sessions/{self.session}/restart")
            if r.status_code == 404:
                await self._client().post(f"/api/sessions/{self.session}/start")
        except Exception as e:
            logger.warning(f"[WAHA:{self.account_id}] restart failed: {e}")

    async def _ensure_scannable(self, status: str) -> str:
        """If the session is in a dead/stopped state, recreate/restart it and wait
        briefly for it to return to SCAN_QR_CODE (or WORKING). Returns the status."""
        import asyncio
        if status in ("FAILED", "STOPPED", "UNKNOWN"):
            await self._restart_session()
            for _ in range(10):  # ~15s for STARTING → SCAN_QR_CODE
                await asyncio.sleep(1.5)
                try:
                    status = await self._session_status()
                except Exception:
                    continue
                if status in ("SCAN_QR_CODE", "WORKING"):
                    break
        return status

    # ── pairing (dashboard connect/QR) ───────────────────────────────────────
    async def begin_qr(self) -> dict:
        """Ensure the session exists (with our webhook) and return a fresh QR."""
        # Create-or-update the session with our webhook config (idempotent-ish).
        cfg = {"webhooks": [{"url": self._webhook_url(), "events": ["message"]}]}
        try:
            await self._client().post("/api/sessions",
                                      json={"name": self.session, "start": True, "config": cfg})
        except Exception:
            pass
        # Make sure it is started.
        try:
            await self._client().post(f"/api/sessions/{self.session}/start")
        except Exception:
            pass
        status = await self._session_status()
        status = await self._ensure_scannable(status)
        if status == "WORKING":
            await self._mark_authorized()
            return {"status": "authorized"}
        img = await self._fetch_qr()
        await account_manager.update_status(self.account_id, "connecting")
        return {"status": "waiting", "image": img} if img else {"status": "waiting"}

    async def qr_poll(self) -> dict:
        try:
            status = await self._session_status()
        except Exception as e:
            return {"status": "error", "error": str(e)}
        if status == "WORKING":
            await self._mark_authorized()
            return {"status": "authorized"}
        # Session died (expired/aborted QR) → auto-restart so the operator always
        # sees a LIVE, scannable QR instead of a dead one.
        if status in ("FAILED", "STOPPED", "UNKNOWN"):
            status = await self._ensure_scannable(status)
            if status == "WORKING":
                await self._mark_authorized()
                return {"status": "authorized"}
        img = await self._fetch_qr()
        return {"status": "waiting", "image": img} if img else {"status": "waiting"}

    async def _mark_authorized(self) -> None:
        await account_manager.update_status(self.account_id, "authorized")
        await account_manager.save_session(self.account_id, self.session)

    async def _fetch_qr(self) -> str | None:
        """Return a data: URL of the pairing QR, or None."""
        for path in (f"/api/{self.session}/auth/qr", f"/api/sessions/{self.session}/auth/qr"):
            try:
                r = await self._client().get(path, params={"format": "image"})
                if r.status_code != 200:
                    continue
                ctype = r.headers.get("content-type", "")
                if ctype.startswith("image/"):
                    b64 = base64.b64encode(r.content).decode()
                    return f"data:{ctype};base64,{b64}"
                data = r.json()
                raw = data.get("data") or data.get("qr")
                mt = data.get("mimetype", "image/png")
                if raw:
                    return raw if str(raw).startswith("data:") else f"data:{mt};base64,{raw}"
            except Exception:
                continue
        return None

    async def healthcheck(self) -> dict:
        try:
            status = await self._session_status()
            return {"status": "authorized" if status == "WORKING" else "disconnected",
                    "engine_status": status}
        except Exception as e:
            return {"status": "disconnected", "error": str(e)}

    # ── inbound webhook ──────────────────────────────────────────────────────
    async def handle_webhook(self, payload: dict) -> None:
        if (payload or {}).get("event") != "message":
            return
        p = payload.get("payload", {}) or {}
        if p.get("fromMe"):
            return  # echo of our own send
        external_id = str(p.get("id", ""))
        if self.is_duplicate(external_id):
            return
        peer = p.get("from", "")
        # Mark the incoming chat as read (blue ticks) — like a real manager who saw it.
        await self._send_seen(peer)
        msg = InboundMessage(
            channel="whatsapp", account_id=self.account_id, peer=peer,
            text=p.get("body", "") or "",
            sender_phone=chatid_to_phone(peer),
            sender_name=p.get("notifyName", "") or p.get("pushName", "") or "",
            external_id=external_id, raw=payload,
        )
        await self._on_inbound(msg)

    # ── presence: seen / typing (like a real manager) ────────────────────────
    async def _send_seen(self, peer: str) -> None:
        try:
            await self._client().post("/api/sendSeen",
                                      json={"session": self.session, "chatId": peer})
        except Exception:
            pass  # best-effort; never block on read receipts

    async def _typing(self, peer: str, on: bool) -> None:
        try:
            ep = "/api/startTyping" if on else "/api/stopTyping"
            await self._client().post(ep, json={"session": self.session, "chatId": peer})
        except Exception:
            pass

    def supports_typing(self) -> bool:
        return True

    async def send_reply(self, peer: str, reply: str) -> None:
        """Manager-like delivery: mark read, show 'typing…', then send each part —
        so on the client's WhatsApp it looks like a human replying, with blue ticks."""
        import asyncio
        from src.index import _split_reply
        parts = _split_reply(reply)
        await self._send_seen(peer)
        for i, part in enumerate(parts):
            await self._typing(peer, True)
            await asyncio.sleep(min(2.0, 0.5 + len(part) / 140))
            await self._typing(peer, False)
            res = await self.send_text(peer, part)
            if not res.ok:
                logger.error(f"[WAHA:{self.account_id}] send part failed: {res.error}")

    # ── outbound ─────────────────────────────────────────────────────────────
    async def send_text(self, peer: str, text: str) -> OutboundResult:
        try:
            r = await self._client().post(
                "/api/sendText", json={"session": self.session, "chatId": peer, "text": text})
            r.raise_for_status()
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        try:
            if isinstance(file, (bytes, bytearray)):
                data_b64 = base64.b64encode(bytes(file)).decode()
                file_obj = {"mimetype": mimetype or "application/octet-stream",
                            "filename": filename or "file", "data": data_b64}
            elif isinstance(file, str) and (file.startswith("http://") or file.startswith("https://")):
                file_obj = {"mimetype": mimetype or "application/octet-stream",
                            "filename": filename or "file", "url": file}
            else:  # local path
                with open(file, "rb") as fh:
                    data_b64 = base64.b64encode(fh.read()).decode()
                file_obj = {"mimetype": mimetype or "application/octet-stream",
                            "filename": filename or os.path.basename(str(file)), "data": data_b64}
            endpoint = "/api/sendImage" if (mimetype or "").startswith("image/") else "/api/sendFile"
            body = {"session": self.session, "chatId": peer, "file": file_obj}
            if caption:
                body["caption"] = caption
            r = await self._client().post(endpoint, json=body)
            r.raise_for_status()
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))
