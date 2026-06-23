"""
Adapter registry — the single source of truth mapping a running account to its
ChannelAdapter, by (channel, account_id) and by conv_id prefix.
"""
import logging

from src.context import parse_conv_id

logger = logging.getLogger(__name__)

# (channel, account_id) -> ChannelAdapter
_adapters: dict[tuple[str, int], object] = {}


def register(adapter) -> None:
    _adapters[adapter.key] = adapter
    logger.info(f"[REGISTRY] registered {adapter.channel}:{adapter.account_id} ({adapter.label})")


def unregister(channel: str, account_id: int) -> None:
    _adapters.pop((channel, int(account_id)), None)


def get(channel: str, account_id: int):
    return _adapters.get((channel, int(account_id)))


def get_by_conv(conv_id: str):
    channel, account_id, _peer = parse_conv_id(conv_id)
    return _adapters.get((channel, account_id))


def all_adapters() -> list:
    return list(_adapters.values())


def adapters_for_channel(channel: str) -> list:
    return [a for k, a in _adapters.items() if k[0] == channel]


def default_telegram():
    """The legacy/default Telegram adapter (account id=1), used by outbound paths
    that aren't conversation-scoped (scheduler campaigns, /send by phone)."""
    from src.context import LEGACY_TG_ACCOUNT_ID
    return _adapters.get(("telegram", LEGACY_TG_ACCOUNT_ID)) or next(
        (a for k, a in _adapters.items() if k[0] == "telegram"), None
    )


def clear() -> None:
    _adapters.clear()
