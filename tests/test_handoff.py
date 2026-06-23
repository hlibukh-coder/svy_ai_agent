"""Tests for production hand-off logic: reply splitting, promised-handoff detection,
and the escalation fallback to Saved Messages so a hand-off is never lost."""
import pytest

from src import index, tools


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_split_reply_single():
    assert index._split_reply("Привіт! Чим допомогти?") == ["Привіт! Чим допомогти?"]


def test_split_reply_multi():
    parts = index._split_reply("Перше.\n\nДруге.\n\nТретє.")
    assert parts == ["Перше.", "Друге.", "Третє."]


def test_split_reply_keeps_question_list_together():
    # tire-list separated by single newlines must NOT be split
    parts = index._split_reply("Підкажіть:\n- де ставити?\n- яка товщина?")
    assert len(parts) == 1


def test_split_reply_caps_parts():
    parts = index._split_reply("\n\n".join(f"п{i}" for i in range(8)), max_parts=4)
    assert len(parts) == 4


def test_split_reply_empty():
    assert index._split_reply("") == []


def test_promised_handoff_detects():
    assert index._promised_handoff("Зараз передам менеджеру, він напише вам найближчим часом")
    assert index._promised_handoff("передам запит менеджеру")
    assert not index._promised_handoff("Болт М8 в наявності, 5 грн/шт")


# ── async: multi-message send + escalation fallback ───────────────────────────

class _FakeAction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeTg:
    def action(self, *a, **k):
        return _FakeAction()


class _FakeEvent:
    chat_id = 123

    def __init__(self):
        self.sent = []

    async def respond(self, text):
        self.sent.append(text)


async def test_send_reply_sends_multiple_messages(monkeypatch):
    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(index.asyncio, "sleep", _no_sleep)
    ev = _FakeEvent()
    await index._send_reply(_FakeTg(), ev, "Перше.\n\nДруге.\n\nТретє.")
    assert ev.sent == ["Перше.", "Друге.", "Третє."]


class _RecTg:
    def __init__(self):
        self.sent = []

    async def send_message(self, peer, text):
        self.sent.append((peer, text))


async def test_escalation_falls_back_to_saved_messages(monkeypatch):
    rec = _RecTg()
    monkeypatch.setattr(tools, "_tg_client", rec)
    monkeypatch.setattr(tools, "_escalation_peer", None)
    await tools._send_escalation("complaint", "клієнт скаржиться", "+380501112233")
    assert rec.sent, "escalation must be delivered, not dropped"
    assert rec.sent[0][0] == "me"


async def test_escalation_uses_configured_peer(monkeypatch):
    rec = _RecTg()
    monkeypatch.setattr(tools, "_tg_client", rec)
    monkeypatch.setattr(tools, "_escalation_peer", 555)
    await tools._send_escalation("order_created", "нове замовлення", "")
    assert rec.sent[0][0] == 555


# ── hardening from adversarial review ─────────────────────────────────────────

def test_split_reply_hard_caps_long_message():
    parts = index._split_reply("a" * 9000)
    assert parts and all(len(p) <= index.TG_MSG_LIMIT for p in parts)
    assert sum(len(p) for p in parts) >= 8000  # nothing dropped


def test_split_reply_caps_long_merged_tail():
    # >max_parts blocks of 2000 chars each must still all be <= limit after merge
    parts = index._split_reply("\n\n".join("x" * 2000 for _ in range(6)))
    assert all(len(p) <= index.TG_MSG_LIMIT for p in parts)


def test_promised_handoff_new_phrasings():
    assert index._promised_handoff("Передам ваш запит менеджеру")
    assert index._promised_handoff("Зараз покличу менеджера")
    assert index._promised_handoff("Передаю менеджеру деталі")
    assert index._promised_handoff("Менеджер зв'яжеться з вами найближчим часом")


def test_promised_handoff_ignores_negation():
    assert not index._promised_handoff("Я не передам менеджеру без вашої згоди")


class _FlakyEvent:
    chat_id = 1

    def __init__(self):
        self.sent = []

    async def respond(self, text):
        if "BOOM" in text:
            raise RuntimeError("send failed")
        self.sent.append(text)


async def test_send_reply_isolates_failures(monkeypatch):
    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(index.asyncio, "sleep", _no_sleep)
    ev = _FlakyEvent()
    await index._send_reply(_FakeTg(), ev, "OK1.\n\nBOOM.\n\nOK2.")
    assert ev.sent == ["OK1.", "OK2."]  # one failure must not abort the rest


async def test_ensure_handoff_escalates_on_promise(monkeypatch):
    calls = []

    async def _fake_exec(name, args, phone):
        calls.append(name)
        return "{}"

    monkeypatch.setattr(index.tools, "execute_tool", _fake_exec)
    await index._ensure_handoff(
        "Зараз передам менеджеру, він напише найближчим часом", set(), "summary", "+380"
    )
    assert calls == ["notify_manager"]


async def test_ensure_handoff_skips_when_create_order_already_ran(monkeypatch):
    calls = []

    async def _fake_exec(name, args, phone):
        calls.append(name)
        return "{}"

    monkeypatch.setattr(index.tools, "execute_tool", _fake_exec)
    await index._ensure_handoff(
        "Замовлення прийнято, передам менеджеру", {"create_order"}, "s", ""
    )
    assert calls == []  # create_order already satisfies the completion guarantee
