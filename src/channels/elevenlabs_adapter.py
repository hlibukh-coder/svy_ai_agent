"""
ElevenLabs voice-calls channel ("Дзвінки").

Unlike the chat channels, this is a PASSIVE / ingest channel: the call itself is
handled by the ElevenLabs Conversational AI agent (configured in ElevenLabs). After
each call ElevenLabs fires a "post-call" webhook with the full transcript, summary
and call metadata — we receive it, mirror the transcript into the conversation so the
call shows up in the dashboard "Діалоги" next to chats, link it to the BAS client by
the caller's phone, and store a structured record in the `calls` ledger. The AI brain
(router) is intentionally NOT invoked — the call already happened, there is nothing to
reply to.

Credentials (accounts table): {
  "api_key": "<xi-api-key>",        # optional — only needed for healthcheck/outbound
  "agent_id": "<elevenlabs agent>", # optional — informational / future outbound
  "webhook_secret": "...",          # auto-generated; appended to the webhook URL as ?token=
}

Wire-up: in ElevenLabs → Conversational AI → (agent or workspace) → Post-call webhook,
set the URL to:  {PUBLIC_URL}/webhooks/elevenlabs/{account_id}?token={webhook_secret}
(get it from the dashboard "Скопіювати webhook URL" button).
"""
import logging
import os

import httpx

from src import accounts as account_manager, context
from src.channels.base import ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
ELEVEN_API = "https://api.elevenlabs.io"


def _dig(d, *path, default=None):
    """Safe nested .get() — payload shapes vary by ElevenLabs telephony provider."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _norm_phone(p: str) -> str:
    p = (str(p or "")).strip()
    if not p:
        return ""
    digits = p.lstrip("+")
    return ("+" + digits) if digits and not p.startswith("+") else p


def _fmt_duration(secs: int) -> str:
    secs = int(secs or 0)
    if secs < 60:
        return f"{secs} с"
    return f"{secs // 60} хв {secs % 60} с"


class ElevenLabsAdapter(ChannelAdapter):
    channel = "elevenlabs"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        self.api_key = self.credentials.get("api_key", "") or ""
        self.agent_id = self.credentials.get("agent_id", "") or ""
        self.webhook_secret = self.credentials.get("webhook_secret", "") or ""
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"xi-api-key": self.api_key} if self.api_key else {}
            self._http = httpx.AsyncClient(base_url=ELEVEN_API, headers=headers, timeout=30)
        return self._http

    def webhook_url(self) -> str:
        base = PUBLIC_URL or "https://<ваш-домен>"
        url = f"{base}/webhooks/elevenlabs/{self.account_id}"
        if self.webhook_secret:
            url += f"?token={self.webhook_secret}"
        return url

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        # A webhook-only receiver is "connected" as soon as it exists; if an API key
        # is present we verify it so the dashboard can show the workspace is reachable.
        if self.api_key:
            try:
                r = await self._client().get("/v1/user")
                if r.status_code == 200:
                    await account_manager.update_status(self.account_id, "authorized")
                    await account_manager.update_account(
                        self.account_id, meta={"webhook_url": self.webhook_url()})
                    logger.info(f"[ELEVEN:{self.account_id}] API key OK — ready")
                    return
                await account_manager.update_status(
                    self.account_id, "error", f"ElevenLabs API {r.status_code}")
                return
            except Exception as e:
                await account_manager.update_status(self.account_id, "error", str(e))
                logger.error(f"[ELEVEN:{self.account_id}] start failed: {e}")
                return
        # No API key: still works as a passive post-call webhook receiver.
        await account_manager.update_status(self.account_id, "authorized")
        await account_manager.update_account(
            self.account_id, meta={"webhook_url": self.webhook_url(), "mode": "webhook-only"})
        logger.info(f"[ELEVEN:{self.account_id}] webhook-only mode — awaiting post-call webhooks")

    async def stop(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def healthcheck(self) -> dict:
        out = {"status": "authorized", "webhook_url": self.webhook_url()}
        if not self.api_key:
            out["note"] = "webhook-only (без API key)"
            return out
        try:
            r = await self._client().get("/v1/user")
            if r.status_code == 200:
                return out
            return {"status": "error", "error": f"ElevenLabs API {r.status_code}",
                    "webhook_url": self.webhook_url()}
        except Exception as e:
            return {"status": "error", "error": str(e), "webhook_url": self.webhook_url()}

    # ── inbound: post-call webhook ───────────────────────────────────────────
    async def handle_webhook(self, payload: dict) -> None:
        payload = payload or {}
        wtype = payload.get("type") or payload.get("event_type") or ""
        if wtype and wtype != "post_call_transcription":
            logger.info(f"[ELEVEN:{self.account_id}] ignoring webhook type={wtype}")
            return
        data = payload.get("data") or payload  # tolerate flattened payloads
        conversation_id = str(data.get("conversation_id") or payload.get("conversation_id") or "")
        if not conversation_id:
            logger.warning(f"[ELEVEN:{self.account_id}] post-call webhook without conversation_id")
            return
        if self.is_duplicate(conversation_id):
            return

        md = data.get("metadata") or {}
        pc = md.get("phone_call") or md.get("phone") or {}
        phone = _norm_phone(
            pc.get("external_number") or pc.get("caller_id") or pc.get("from_number")
            or _dig(data, "conversation_initiation_client_data", "dynamic_variables", "system__caller_id")
            or "")
        direction = (pc.get("direction") or "").lower() or "inbound"
        # Conversation key: prefer the caller's phone so repeat calls thread together
        # (and link to the same BAS client), else fall back to the provider id.
        peer = phone or conversation_id
        conv_id = f"{self.channel}:{self.account_id}:{peer}"

        analysis = data.get("analysis") or {}
        summary = (analysis.get("transcript_summary") or "").strip()
        outcome = analysis.get("call_successful") or ""
        duration = int(md.get("call_duration_secs") or 0)
        started_unix = md.get("start_time_unix_secs")
        started_at = None
        if started_unix:
            try:
                from datetime import datetime, timezone
                started_at = datetime.fromtimestamp(int(started_unix), timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S+00:00")
            except Exception:
                started_at = None
        recording_url = (md.get("recording_url") or data.get("recording_url") or "") or ""

        # Idempotent ledger insert — also our cross-restart de-dup.
        is_new = await context.save_call(
            conversation_id, conv_id, self.account_id, channel=self.channel, peer=peer,
            phone=phone, direction=direction, status=data.get("status") or "done",
            duration_secs=duration, summary=summary, recording_url=recording_url,
            started_at=started_at)
        if not is_new:
            logger.info(f"[ELEVEN:{self.account_id}] call {conversation_id} already ingested")
            return

        # Link the caller to a BAS client by phone (best-effort), so the call lands in
        # the right contact and feeds analytics.
        caller_name = ""
        try:
            from src import bas
            client = await bas.get_client(phone) if phone else None
            caller_name = (client or {}).get("name", "") or ""
            await context.link_client(
                conv_id=conv_id, channel=self.channel, account_id=self.account_id, peer=peer,
                phone=phone, client_ref_key=(client or {}).get("id", "") or "", name=caller_name)
        except Exception as e:
            logger.warning(f"[ELEVEN:{self.account_id}] link_client failed: {e}")

        # Mirror the call into the conversation: a header line + the transcript turns,
        # so the whole thing reads as a call inside "Діалоги".
        when = (started_at or "")[5:16].replace("-", ".") if started_at else ""
        dir_label = "Вихідний" if direction == "outbound" else "Вхідний"
        header = f"📞 {dir_label} дзвінок · {when} · {_fmt_duration(duration)}".strip(" ·")
        if summary:
            header += f"\n📝 {summary}"
        await context.save_message(conv_id=conv_id, role="system", content=header,
                                   channel=self.channel, account_id=self.account_id, peer=peer)
        for turn in (data.get("transcript") or []):
            text = (turn.get("message") or turn.get("text") or "").strip()
            if not text:
                continue
            role = "user" if turn.get("role") == "user" else "assistant"
            await context.save_message(conv_id=conv_id, role=role, content=text,
                                       channel=self.channel, account_id=self.account_id, peer=peer)

        # Best-effort activity-feed event (PG-backed; no-op under USE_MOCK).
        try:
            from src import config
            who = caller_name or phone or "невідомий"
            await config.log_event("call", f"Дзвінок: {who} · {_fmt_duration(duration)}"
                                   + (f" — {summary[:60]}" if summary else ""))
        except Exception:
            pass
        logger.info(f"[ELEVEN:{self.account_id}] ingested call {conversation_id} "
                    f"phone={phone} turns={len(data.get('transcript') or [])}")

    # ── outbound ─────────────────────────────────────────────────────────────
    async def send_text(self, peer: str, text: str) -> OutboundResult:
        # The calls channel only ingests call info; there is no text reply on a phone
        # call. (Outbound dialing could be added later via the ElevenLabs batch-call API.)
        return OutboundResult(
            ok=False,
            error="Канал «Дзвінки» лише приймає інформацію про дзвінки — текстова відповідь недоступна.")

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        return OutboundResult(ok=False, error="Канал «Дзвінки» не підтримує надсилання файлів.")

    async def send_reply(self, peer: str, reply: str) -> None:
        # No AI reply for calls — explicitly inert.
        return

    def peer_for_phone(self, phone: str) -> str:
        return _norm_phone(phone)
