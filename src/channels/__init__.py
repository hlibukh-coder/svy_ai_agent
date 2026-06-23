"""Multi-channel messaging layer: adapters, registry, router, manager."""
from src.channels.base import Attachment, ChannelAdapter, InboundMessage, OutboundResult

__all__ = ["Attachment", "ChannelAdapter", "InboundMessage", "OutboundResult"]
