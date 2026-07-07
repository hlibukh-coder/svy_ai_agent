"""
Local storage for chat files.

- Inbound attachments (photo/document a client sends in Telegram/WhatsApp/Viber)
  are saved under FILES_DIR and linked to their `messages` row, so the operator
  can open/save them from the dashboard (GET /api/files/{message_id}).
- The outbox is for channels that can only send media by PUBLIC URL (Viber):
  bytes are written under FILES_DIR/outbox and served at /files/outbox/{name}
  with an unguessable uuid name, so the provider can fetch the file from us.
"""
import logging
import os
import re
import uuid

import httpx

from src.channels.base import Attachment

logger = logging.getLogger(__name__)

FILES_DIR = os.getenv("FILES_DIR", "data/files")
MAX_INBOUND_BYTES = 50 * 1024 * 1024


def files_dir() -> str:
    os.makedirs(FILES_DIR, exist_ok=True)
    return FILES_DIR


def _safe_name(name: str) -> str:
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r"[^\w.\-()\[\] ]+", "_", name, flags=re.UNICODE)
    return name[:120] or "file"


async def save_attachment(att: Attachment) -> dict | None:
    """Persist one inbound attachment (bytes / local path / remote URL) into
    FILES_DIR. Returns {"path": <rel>, "filename", "mimetype", "size"} for
    messages.file_* columns, or None if it could not be saved."""
    try:
        data = att.data
        if data is None and att.path:
            p = os.path.abspath(att.path)
            root = os.path.abspath(files_dir())
            if p.startswith(root + os.sep) and os.path.isfile(p):
                # Already downloaded straight into FILES_DIR (Telegram) — keep as is.
                return {"path": os.path.relpath(p, root),
                        "filename": att.filename or os.path.basename(p),
                        "mimetype": att.mimetype or "application/octet-stream",
                        "size": os.path.getsize(p)}
            with open(p, "rb") as fh:
                data = fh.read()
        if data is None and att.url:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(att.url)
                r.raise_for_status()
                data = r.content
        if data is None:
            return None
        if len(data) > MAX_INBOUND_BYTES:
            logger.warning(f"[FILES] attachment '{att.filename}' too big ({len(data)}b) — skipped")
            return None
        rel = f"{uuid.uuid4().hex[:8]}_{_safe_name(att.filename)}"
        with open(os.path.join(files_dir(), rel), "wb") as fh:
            fh.write(data)
        return {"path": rel, "filename": att.filename or rel,
                "mimetype": att.mimetype or "application/octet-stream", "size": len(data)}
    except Exception as e:
        logger.error(f"[FILES] save attachment '{att.filename}' failed: {e}")
        return None


def resolve(rel_path: str) -> str | None:
    """Absolute path of a stored file, or None if missing/outside FILES_DIR."""
    root = os.path.abspath(files_dir())
    p = os.path.abspath(os.path.join(root, str(rel_path or "")))
    return p if p.startswith(root + os.sep) and os.path.isfile(p) else None


# ── outbox (publicly served files for URL-only channels like Viber) ───────────

def _outbox_dir() -> str:
    d = os.path.join(files_dir(), "outbox")
    os.makedirs(d, exist_ok=True)
    return d


def outbox_put(data: bytes, filename: str) -> str:
    """Store bytes under an unguessable name (extension kept — Viber requires it
    in the media URL) and return that name for /files/outbox/{name}."""
    ext = os.path.splitext(_safe_name(filename))[1].lower()
    name = uuid.uuid4().hex + ext
    with open(os.path.join(_outbox_dir(), name), "wb") as fh:
        fh.write(data)
    return name


def outbox_resolve(name: str) -> str | None:
    root = os.path.abspath(_outbox_dir())
    p = os.path.abspath(os.path.join(root, str(name or "")))
    return p if p.startswith(root + os.sep) and os.path.isfile(p) else None
