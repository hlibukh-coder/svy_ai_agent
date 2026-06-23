"""
Adapter lifecycle: build a ChannelAdapter for every enabled account, register it,
start it. Called from main.startup() (replacing the single Telegram connect) and
main.shutdown().
"""
import logging

from src import accounts as account_manager
from src.channels import registry
from src.channels.router import route_inbound

logger = logging.getLogger(__name__)


def _adapter_class(channel: str):
    if channel == "telegram":
        from src.channels.telegram_adapter import TelegramAdapter
        return TelegramAdapter
    if channel == "whatsapp":
        from src.channels.waha_adapter import WahaAdapter
        return WahaAdapter
    if channel == "email":
        from src.channels.email_adapter import EmailAdapter
        return EmailAdapter
    if channel == "viber":
        from src.channels.viber_adapter import ViberAdapter
        return ViberAdapter
    return None


async def dispatch(msg) -> None:
    """on_inbound callback shared by every adapter. Resolves the adapter from the
    registry (by msg.channel/account_id) and runs the channel-agnostic router."""
    adapter = registry.get(msg.channel, msg.account_id)
    if adapter is None:
        logger.warning(f"[MANAGER] no adapter for {msg.channel}:{msg.account_id}, dropping inbound")
        return
    try:
        await route_inbound(msg, adapter)
    except Exception as e:
        logger.error(f"[MANAGER] route_inbound failed for {msg.channel}:{msg.account_id}: {e}")


def build_adapter(acct: dict):
    """Construct (but don't start) an adapter from an accounts-table row (with secrets)."""
    cls = _adapter_class(acct["channel"])
    if cls is None:
        logger.warning(f"[MANAGER] unknown channel {acct['channel']} (account {acct['id']})")
        return None
    adapter = cls(acct["id"], acct["label"], acct.get("credentials") or {}, dispatch)
    adapter.session_blob = acct.get("session_blob")
    adapter.meta = acct.get("meta") or {}
    return adapter


async def start_account(account_id: int) -> bool:
    """(Re)build + start a single account by id. Used by the dashboard connect flow."""
    acct = await account_manager.get_account(account_id, include_secrets=True)
    if not acct or not acct["enabled"]:
        return False
    existing = registry.get(acct["channel"], acct["id"])
    if existing is not None:
        try:
            await existing.stop()
        except Exception:
            pass
        registry.unregister(acct["channel"], acct["id"])
    adapter = build_adapter(acct)
    if adapter is None:
        return False
    registry.register(adapter)
    try:
        await adapter.start()
        return True
    except Exception as e:
        logger.error(f"[MANAGER] start {acct['channel']}:{acct['id']} failed: {e}")
        await account_manager.update_status(acct["id"], "error", str(e))
        return False


async def start_all_adapters() -> None:
    accts = await account_manager.list_accounts(include_secrets=True)
    for acct in accts:
        if not acct["enabled"]:
            continue
        adapter = build_adapter(acct)
        if adapter is None:
            continue
        registry.register(adapter)
        try:
            await adapter.start()
        except Exception as e:
            logger.error(f"[MANAGER] start {acct['channel']}:{acct['id']} failed: {e}")
            try:
                await account_manager.update_status(acct["id"], "error", str(e))
            except Exception:
                pass
    logger.info(f"[MANAGER] started {len(registry.all_adapters())} adapter(s)")


async def stop_all_adapters() -> None:
    for adapter in registry.all_adapters():
        try:
            await adapter.stop()
        except Exception as e:
            logger.error(f"[MANAGER] stop {adapter.channel}:{adapter.account_id} failed: {e}")
    registry.clear()
