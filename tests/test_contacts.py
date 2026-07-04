"""Tests for real contact names + phone-typed (non-agent) outgoing capture."""
from types import SimpleNamespace

from src import context
from src.channels import router
from src.channels.base import InboundMessage
from src.channels.telegram_adapter import TelegramAdapter, mark_sent, _display_name


async def _fresh_db(tmp_path, monkeypatch, name="contacts.db"):
    db = str(tmp_path / name)
    monkeypatch.setattr(context, "DB_PATH", db)
    await context.init_db()
    return db


# ── contact profile upsert ────────────────────────────────────────────────────

async def test_upsert_creates_and_keeps_bas_link(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    conv = "telegram:1:555"
    await context.link_client("555", phone="+380501112233", client_ref_key="bas-1",
                              name="ПРИМТЕКС", conv_id=conv)
    # profile refresh must NOT clobber the BAS link, empty values must not erase
    await context.upsert_contact_profile(conv, name="Іван Петренко", phone="")
    linked = await context.get_linked_client(conv_id=conv)
    assert linked["name"] == "Іван Петренко"
    assert linked["client_ref_key"] == "bas-1"
    assert linked["phone"] == "+380501112233"
    await context.upsert_contact_profile(conv, name="", phone="")
    linked = await context.get_linked_client(conv_id=conv)
    assert linked["name"] == "Іван Петренко"  # unchanged


async def test_convs_without_name_listed(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    await context.save_message(conv_id="telegram:1:111", role="user", content="хто це")
    await context.save_message(conv_id="telegram:1:222", role="user", content="привіт")
    await context.upsert_contact_profile("telegram:1:222", name="Оля")
    convs = await context.telegram_convs_without_name(1)
    assert "telegram:1:111" in convs and "telegram:1:222" not in convs


async def test_router_fills_name_in_manual_mode(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    msg = InboundMessage(channel="telegram", account_id=1, peer="777",
                         text="Добрий день", sender_name="Сергій Коваль",
                         sender_phone="+380671234567")
    await router.route_inbound(msg, adapter=None)  # DEFAULTS: auto_reply off
    linked = await context.get_linked_client(conv_id="telegram:1:777")
    assert linked and linked["name"] == "Сергій Коваль"
    hist = await context.load_history(conv_id="telegram:1:777")
    assert hist and hist[-1]["content"] == "Добрий день"


def test_display_name_variants():
    assert _display_name(SimpleNamespace(first_name="Іван", last_name="Франко",
                                         username="ivan")) == "Іван Франко"
    assert _display_name(SimpleNamespace(first_name="", last_name="",
                                         username="ivan")) == "ivan"


# ── phone-typed outgoing capture ──────────────────────────────────────────────

def _adapter():
    a = object.__new__(TelegramAdapter)
    a.account_id = 1
    a._me_id = 42
    return a


def _event(mid=900, chat=555, text="пишу з телефону", media=None, private=True):
    return SimpleNamespace(is_private=private, id=mid, chat_id=chat, raw_text=text,
                           message=SimpleNamespace(media=media, photo=None))


async def test_phone_sent_message_recorded_and_pauses_ai(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    await _adapter()._on_outgoing(_event())
    hist = await context.load_history(conv_id="telegram:1:555")
    assert hist and hist[-1]["role"] == "assistant"
    assert hist[-1]["content"] == "пишу з телефону"
    assert await context.is_chat_paused(conv_id="telegram:1:555") is True


async def test_programmatic_send_not_duplicated(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    mark_sent(SimpleNamespace(id=901))
    await _adapter()._on_outgoing(_event(mid=901))
    assert await context.load_history(conv_id="telegram:1:555") == []


async def test_self_and_group_chats_skipped(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    await _adapter()._on_outgoing(_event(chat=42))            # saved messages
    await _adapter()._on_outgoing(_event(private=False))      # group
    assert await context.load_history(conv_id="telegram:1:42") == []
    assert await context.load_history(conv_id="telegram:1:555") == []


async def test_phone_sent_file_recorded_as_placeholder(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    await _adapter()._on_outgoing(_event(text="", media=object()))
    hist = await context.load_history(conv_id="telegram:1:555")
    assert hist and hist[-1]["content"] == "[файл]"
