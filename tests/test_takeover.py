"""Tests for human-takeover: per-chat AI pause and operator send guards."""
import pytest

from src import context, index


async def test_chat_ai_pause_roundtrip(tmp_path, monkeypatch):
    db = str(tmp_path / "takeover.db")
    monkeypatch.setattr(context, "DB_PATH", db)
    await context.init_db()

    assert await context.is_chat_paused("c1") is False
    await context.set_chat_ai_paused("c1", True)
    assert await context.is_chat_paused("c1") is True
    # other chats are unaffected
    assert await context.is_chat_paused("c2") is False
    await context.set_chat_ai_paused("c1", False)
    assert await context.is_chat_paused("c1") is False


async def test_operator_send_no_tg_client(monkeypatch):
    monkeypatch.setattr(index, "_tg_client", None)
    res = await index.operator_send("123", "привіт")
    assert res["ok"] is False


async def test_operator_send_empty_text(monkeypatch):
    class _Tg:
        async def send_message(self, *a, **k):
            raise AssertionError("must not send empty text")

    monkeypatch.setattr(index, "_tg_client", _Tg())
    res = await index.operator_send("123", "   ")
    assert res["ok"] is False


async def test_operator_send_delivers_and_pauses(tmp_path, monkeypatch):
    db = str(tmp_path / "ops.db")
    monkeypatch.setattr(context, "DB_PATH", db)
    await context.init_db()

    sent = []

    class _Tg:
        async def send_message(self, peer, text):
            sent.append((peer, text))

    monkeypatch.setattr(index, "_tg_client", _Tg())
    res = await index.operator_send("555", "Доброго дня, це менеджер")
    assert res["ok"] is True
    assert sent and sent[0][0] == 555  # numeric chat_id resolved to int peer
    # AI must be paused for this chat after a human takes over
    assert await context.is_chat_paused("555") is True
    # the operator message is persisted as the business side
    hist = await context.load_history("555")
    assert hist and hist[-1]["role"] == "assistant"
