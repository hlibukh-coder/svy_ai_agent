"""Tests for the multi-channel / multi-account layer."""
import json

import pytest

from src import accounts, bas, context
from src.channels import registry, router
from src.channels.base import InboundMessage
from src.channels.waha_adapter import WahaAdapter, phone_to_chatid, chatid_to_phone
from src.channels.viber_adapter import ViberAdapter


@pytest.fixture
async def tmpdb(tmp_path, monkeypatch):
    db = str(tmp_path / "ch.db")
    monkeypatch.setattr(context, "DB_PATH", db)
    monkeypatch.setattr(accounts, "DB_PATH", db)
    await context.init_db()
    return db


# ── product code search ───────────────────────────────────────────────────────

def test_normalize_code():
    assert bas._normalize_code("din 933") == "DIN933"
    assert bas._normalize_code("DIN-933") == "DIN933"
    assert bas._normalize_code("d.i.n 933") == "DIN933"
    assert bas._normalize_code("") == ""


# ── conv_id helpers ───────────────────────────────────────────────────────────

def test_conv_id_helpers():
    assert context.as_conv_id("12345") == "telegram:1:12345"
    assert context.as_conv_id("whatsapp:2:380@c.us") == "whatsapp:2:380@c.us"
    assert context.parse_conv_id("email:3:a@b.com") == ("email", 3, "a@b.com")
    # bare legacy id round-trips
    assert context.parse_conv_id("999") == ("telegram", 1, "999")


# ── context multi-channel ─────────────────────────────────────────────────────

async def test_context_multichannel(tmpdb):
    conv = "whatsapp:2:380501112233@c.us"
    await context.save_message(conv_id=conv, role="user", content="привіт")
    await context.save_message(conv_id=conv, role="assistant", content="вітаю")
    hist = await context.load_history(conv_id=conv)
    assert [m["role"] for m in hist] == ["user", "assistant"]

    await context.link_client(conv_id=conv, phone="+380501112233", client_ref_key="c1", name="Іван")
    linked = await context.get_linked_client(conv_id=conv)
    assert linked["phone"] == "+380501112233"
    assert linked["channel"] == "whatsapp" and linked["account_id"] == 2

    chats = await context.get_all_chats()
    row = next(c for c in chats if c["conv_id"] == conv)
    assert row["channel"] == "whatsapp"
    # legacy bare chat_id still works through the back-compat path
    await context.save_message("777", "user", "legacy")
    h2 = await context.load_history("777")
    assert h2 and h2[0]["content"] == "legacy"


# ── accounts CRUD ─────────────────────────────────────────────────────────────

async def test_accounts_crud(tmpdb):
    accs = await accounts.list_accounts()
    assert any(a["id"] == 1 and a["channel"] == "telegram" for a in accs)  # legacy seed

    aid = await accounts.add_account("whatsapp", "WA #1",
                                     {"base_url": "http://x", "api_key": "k"})
    got = await accounts.get_account(aid, include_secrets=True)
    assert got["credentials"]["base_url"] == "http://x"

    await accounts.update_status(aid, "authorized")
    assert (await accounts.get_account(aid))["status"] == "authorized"

    await accounts.save_session(aid, "session-blob")
    assert (await accounts.get_account(aid, include_secrets=True))["session_blob"] == "session-blob"

    await accounts.set_enabled(aid, False)
    assert (await accounts.get_account(aid))["enabled"] is False

    assert await accounts.delete_account(aid) is True
    assert await accounts.delete_account(1) is False  # legacy can't be deleted


# ── WAHA adapter ──────────────────────────────────────────────────────────────

def test_waha_peer_conversion():
    assert phone_to_chatid("+380501112233") == "380501112233@c.us"
    assert chatid_to_phone("380501112233@c.us") == "+380501112233"


async def test_waha_webhook_parse_and_dedup():
    captured = []

    async def on_inbound(msg):
        captured.append(msg)

    ad = WahaAdapter(2, "WA", {"base_url": "http://x", "session_name": "default"}, on_inbound)
    payload = {"event": "message", "session": "default", "payload": {
        "id": "AAA", "from": "380501112233@c.us", "fromMe": False,
        "body": "є болти?", "notifyName": "Іван"}}
    await ad.handle_webhook(payload)
    await ad.handle_webhook(payload)        # duplicate id → ignored
    # our own echo → ignored
    await ad.handle_webhook({"event": "message", "payload": {"id": "B", "fromMe": True, "body": "x"}})

    assert len(captured) == 1
    m = captured[0]
    assert m.channel == "whatsapp" and m.account_id == 2
    assert m.peer == "380501112233@c.us"
    assert m.sender_phone == "+380501112233"
    assert m.text == "є болти?"


async def test_viber_sleeps_without_token():
    ad = ViberAdapter(4, "VB", {}, lambda m: None)
    res = await ad.send_text("u1", "hi")
    assert res.ok is False and res.error == "viber_no_token"


# ── router (channel-agnostic core) ────────────────────────────────────────────

async def test_router_dispatch(tmpdb, monkeypatch):
    from src import index, config

    async def fake_run_openai(messages, phone, conv=None):
        assert conv and conv["channel"] == "whatsapp"
        return "Вітаю! Чим допомогти?", set()

    monkeypatch.setattr(index, "run_openai", fake_run_openai)

    # auto-reply defaults to manual; this test exercises the reply mechanic, so turn it on.
    _orig_get_value = config.get_value
    async def fake_get_value(key, default=None):
        return True if key == "auto_reply" else await _orig_get_value(key, default)
    monkeypatch.setattr(config, "get_value", fake_get_value)

    sent = []

    class FakeAdapter:
        channel = "whatsapp"
        account_id = 2
        async def send_reply(self, peer, reply):
            sent.append((peer, reply))

    msg = InboundMessage(channel="whatsapp", account_id=2, peer="380@c.us", text="привіт")
    await router.route_inbound(msg, FakeAdapter())

    assert sent == [("380@c.us", "Вітаю! Чим допомогти?")]
    hist = await context.load_history(conv_id="whatsapp:2:380@c.us")
    assert [m["role"] for m in hist] == ["user", "assistant"]


async def test_router_manual_mode_silent(tmpdb, monkeypatch):
    """auto_reply off (the default) → the AI records the inbound but stays silent;
    it answers only when the operator triggers it on demand."""
    from src import index

    async def fake_run_openai(messages, phone, conv=None):
        raise AssertionError("AI must not run in manual mode")

    monkeypatch.setattr(index, "run_openai", fake_run_openai)

    sent = []

    class FakeAdapter:
        channel = "whatsapp"
        account_id = 2
        async def send_reply(self, peer, reply):
            sent.append((peer, reply))

    msg = InboundMessage(channel="whatsapp", account_id=2, peer="380@c.us", text="привіт")
    await router.route_inbound(msg, FakeAdapter())

    assert sent == []
    hist = await context.load_history(conv_id="whatsapp:2:380@c.us")
    assert [m["role"] for m in hist] == ["user"]


async def test_router_respects_human_takeover(tmpdb, monkeypatch):
    from src import index
    called = {"n": 0}

    async def fake_run_openai(*a, **k):
        called["n"] += 1
        return "x", set()

    monkeypatch.setattr(index, "run_openai", fake_run_openai)
    conv = "whatsapp:2:999@c.us"
    await context.set_chat_ai_paused(conv_id=conv, paused=True)

    sent = []

    class FakeAdapter:
        channel = "whatsapp"; account_id = 2
        async def send_reply(self, peer, reply):
            sent.append(reply)

    await router.route_inbound(
        InboundMessage(channel="whatsapp", account_id=2, peer="999@c.us", text="алло"),
        FakeAdapter())
    assert called["n"] == 0 and sent == []          # AI silent
    hist = await context.load_history(conv_id=conv)
    assert hist and hist[-1]["role"] == "user"      # message still recorded


# ── send_file resolution (AI tool) ────────────────────────────────────────────

async def test_resolve_doc_pricelist():
    from src import tools
    doc = await tools._resolve_doc("pricelist", {})
    assert doc and doc["filename"].startswith("pricelist")


async def test_generate_invoice_mock():
    # MOCK_CLIENTS has +380681234567 with an order
    from src import tools
    doc = await tools._resolve_doc("invoice", {"phone": "+380681234567"})
    assert doc is not None
    assert isinstance(doc["src"], (bytes, bytearray))
    assert b"\xd0" in doc["src"]  # cyrillic UTF-8 content
