"""Shared Telegram APPLICATION identity (api_id / api_hash).

api_id/api_hash identify the app, not an account — accounts sign in via QR.
Baked-in defaults mean a fresh clone connects Telegram with zero .env editing
(launch → dashboard → scan QR, same UX as WAHA). TG_API_ID / TG_API_HASH env
vars still override them when set to real values.
"""
import os

DEFAULT_TG_API_ID = 37369065
DEFAULT_TG_API_HASH = "d7d3d0126b5db2053641cd1160492f56"


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0  # unset or a "your_api_id" placeholder → use the default


TG_API_ID = _int(os.getenv("TG_API_ID")) or DEFAULT_TG_API_ID
_hash = (os.getenv("TG_API_HASH") or "").strip()
TG_API_HASH = _hash if _hash and not _hash.startswith("your_") else DEFAULT_TG_API_HASH
