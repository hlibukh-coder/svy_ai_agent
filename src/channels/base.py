"""
Channel adapter contract + normalized message types.

Every channel (Telegram, WhatsApp/WAHA, Email, Viber) implements ChannelAdapter.
Inbound messages from any channel are normalized into an InboundMessage and handed
to a single shared router (src/channels/router.py), so the AI brain is fully
decoupled from Telegram.
"""
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    mimetype: str = "application/octet-stream"
    data: bytes | None = None   # inline bytes
    path: str | None = None     # local path, if already on disk
    url: str | None = None      # remote URL (WAHA/Viber can send/receive by URL)
    size: int | None = None


@dataclass
class InboundMessage:
    channel: str                 # "telegram" | "whatsapp" | "email" | "viber"
    account_id: int              # which of OUR accounts received it
    peer: str                    # channel-native address to reply to
    text: str = ""
    conv_id: str = ""            # filled by the router if empty
    sender_phone: str = ""
    sender_email: str = ""
    sender_name: str = ""
    external_id: str = ""        # provider message id, for dedup
    thread_ref: str = ""         # email Message-ID / References for threading
    subject: str = ""            # email subject (used to thread the reply)
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict | None = None


@dataclass
class OutboundResult:
    ok: bool
    error: str = ""
    external_id: str = ""


class ChannelAdapter(ABC):
    """One instance per connected account."""

    channel: str = "base"

    def __init__(self, account_id: int, label: str, credentials: dict, on_inbound):
        self.account_id = int(account_id)
        self.label = label
        self.credentials = credentials or {}
        # on_inbound: async callable(InboundMessage) -> None  (the shared router)
        self._on_inbound = on_inbound
        # Set by the manager from the accounts table before start():
        self.session_blob = None    # Telethon StringSession / WAHA state / IMAP cursor
        self.meta: dict = {}         # non-secret display info (legacy_session, phone, ...)
        # Inbound dedup (WAHA/Viber retry on non-2xx; IMAP may re-deliver).
        self._seen_ids: set = set()
        self._seen_order: deque = deque()

    def is_duplicate(self, external_id: str) -> bool:
        """True if this provider message id was already handled (bounded LRU)."""
        if not external_id:
            return False
        if external_id in self._seen_ids:
            return True
        self._seen_ids.add(external_id)
        self._seen_order.append(external_id)
        if len(self._seen_order) > 1000:
            old = self._seen_order.popleft()
            self._seen_ids.discard(old)
        return False

    @property
    def key(self) -> tuple[str, int]:
        return (self.channel, self.account_id)

    def conv_id(self, peer: str) -> str:
        return f"{self.channel}:{self.account_id}:{peer}"

    # ── lifecycle ────────────────────────────────────────────────────────────
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # ── outbound ─────────────────────────────────────────────────────────────
    @abstractmethod
    async def send_text(self, peer: str, text: str) -> OutboundResult: ...

    @abstractmethod
    async def send_file(self, peer: str, file: "str | bytes | Path",
                        caption: str = "", filename: str = "",
                        mimetype: str = "") -> OutboundResult: ...

    async def send_reply(self, peer: str, reply: str) -> None:
        """Default: split a reply into several short messages like a real manager
        (reuses the existing Telegram splitter). Adapters override where the channel
        wants a single message (e.g. email)."""
        from src.index import _split_reply  # lazy to avoid import cycle
        for part in _split_reply(reply):
            res = await self.send_text(peer, part)
            if not res.ok:
                logger.error(f"[{self.channel}:{self.account_id}] send part failed: {res.error}")

    # ── inbound webhook (push channels override) ─────────────────────────────
    async def handle_webhook(self, payload: dict) -> None:
        logger.warning(f"[{self.channel}:{self.account_id}] handle_webhook not implemented")

    def peer_for_phone(self, phone: str) -> str:
        """Convert a +phone to this channel's native peer (overridden per channel)."""
        return phone

    # ── capability hooks (sane defaults) ─────────────────────────────────────
    async def healthcheck(self) -> dict:
        return {"status": "unknown"}

    def supports_typing(self) -> bool:
        return False

    def max_file_bytes(self) -> int:
        return 50 * 1024 * 1024
