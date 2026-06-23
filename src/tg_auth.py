"""
QR-login dispatcher for Telegram accounts. Thin layer over the per-account
TelegramAdapter (src/channels/telegram_adapter.py), keyed by account_id.

Flow (frontend), per account:
  1. POST /api/accounts/{id}/qr/start  → {svg, status}
  2. user scans QR in Telegram (Settings → Devices → Link Desktop Device)
  3. frontend polls GET /api/accounts/{id}/qr/poll every ~2s
       - {status:"waiting", svg?}   {status:"password"}   {status:"authorized"}
  4. if password: POST /api/accounts/{id}/qr/password {password}

The legacy single session is account id=1; the old /api/telegram/qr/* endpoints map to it.
"""
import logging

from src.accounts import LEGACY_TG_ACCOUNT_ID
from src.channels import manager, registry

logger = logging.getLogger(__name__)


async def _adapter(account_id: int, autostart: bool = False):
    adapter = registry.get("telegram", account_id)
    if adapter is None and autostart:
        await manager.start_account(account_id)
        adapter = registry.get("telegram", account_id)
    return adapter


async def status(account_id: int = LEGACY_TG_ACCOUNT_ID) -> dict:
    adapter = await _adapter(account_id)
    if adapter is None:
        return {"status": "disconnected"}
    return await adapter.healthcheck()


async def start(account_id: int = LEGACY_TG_ACCOUNT_ID) -> dict:
    adapter = await _adapter(account_id, autostart=True)
    if adapter is None:
        return {"status": "error", "error": "no such telegram account"}
    return await adapter.begin_qr()


async def poll(account_id: int = LEGACY_TG_ACCOUNT_ID) -> dict:
    adapter = await _adapter(account_id)
    if adapter is None:
        return {"status": "disconnected"}
    return await adapter.qr_poll()


async def submit_password(password: str, account_id: int = LEGACY_TG_ACCOUNT_ID) -> dict:
    adapter = await _adapter(account_id)
    if adapter is None:
        return {"status": "disconnected"}
    return await adapter.qr_submit_password(password)
