"""Tests for src/scheduler.py — outbound targeting, reorder/inactive logic, delivery."""
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("USE_MOCK", "true")
os.environ["OUTBOUND_THROTTLE_SEC"] = "0"  # no sleeping in tests

import src.scheduler as sched_module


# ── Helpers ───────────────────────────────────────────────────────────────────

def _target(name="Андрій", client_ref_key="ref-001", phone="+380681234567"):
    return {"client_ref_key": client_ref_key, "name": name, "phone": phone}


def _order(days_ago: int, amount=1000.0):
    return {
        "ref_key": "order-1",
        "number": "УЗ-000001",
        "date": datetime.now().date() - timedelta(days=days_ago),
        "amount": amount,
    }


def _patch(targets, orders, compose_ret="AI text", deliver_ret=True):
    """Common patch bundle for job-logic tests. Returns (deliver_mock, compose_mock)."""
    deliver = AsyncMock(return_value=deliver_ret)
    compose = AsyncMock(return_value=compose_ret)
    ctx = [
        patch.object(sched_module, "_tg_client", AsyncMock()),
        patch.object(sched_module, "_deliver", deliver),
        patch.object(sched_module.outbound, "compose_message", compose),
        patch.object(sched_module, "_get_bas_outbound_targets", AsyncMock(return_value=targets)),
        patch.object(sched_module, "_get_orders_for_client", AsyncMock(return_value=orders)),
    ]
    return deliver, compose, ctx


def _enter(ctx):
    for c in ctx:
        c.start()


def _exit(ctx):
    for c in ctx:
        c.stop()


# ── start / stop ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_creates_scheduler():
    sched_module.start(AsyncMock())
    assert sched_module._scheduler is not None
    assert sched_module._scheduler.running
    sched_module.stop()


def test_set_tg_client():
    mock_tg = AsyncMock()
    sched_module.set_tg_client(mock_tg)
    assert sched_module._tg_client is mock_tg


# ── extract_ua_phone (pure) ────────────────────────────────────────────────────

def test_extract_ua_phone_variants():
    from src.telegram_utils import extract_ua_phone
    assert extract_ua_phone("0504442888") == "+380504442888"
    assert extract_ua_phone("0504442888, 0504442888") == "+380504442888"
    assert extract_ua_phone("380512580903") == "+380512580903"
    assert extract_ua_phone("4943535Факс:4590386") is None  # junk, not a mobile
    assert extract_ua_phone("") is None
    assert extract_ua_phone(None) is None


# ── _check_reorder_clients ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reorder_fires_on_due_date():
    """Orders ~20d apart, last ~15d ago → remind window hits today → delivered."""
    deliver, compose, ctx = _patch(
        targets=[_target()],
        orders=[_order(15), _order(35), _order(55)],
    )
    _enter(ctx)
    try:
        await sched_module._check_reorder_clients()
    finally:
        _exit(ctx)
    compose.assert_called_once()
    assert compose.call_args[0][1] == "Андрій"           # name passed to composer
    deliver.assert_called_once()
    assert deliver.call_args[0][0] == "+380681234567"     # phone
    assert deliver.call_args[0][2] == "AI text"           # generated text


@pytest.mark.asyncio
async def test_reorder_skips_when_not_due():
    """Last order 5d ago, cycle ~20d → not due → no delivery."""
    deliver, compose, ctx = _patch(
        targets=[_target()],
        orders=[_order(5), _order(25), _order(45)],
    )
    _enter(ctx)
    try:
        await sched_module._check_reorder_clients()
    finally:
        _exit(ctx)
    deliver.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_needs_two_orders():
    """Single order → cannot estimate cycle → skip."""
    deliver, compose, ctx = _patch(targets=[_target()], orders=[_order(20)])
    _enter(ctx)
    try:
        await sched_module._check_reorder_clients()
    finally:
        _exit(ctx)
    deliver.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_no_send_when_composer_empty():
    """No template fallback: empty composition → nothing delivered."""
    deliver, compose, ctx = _patch(
        targets=[_target()],
        orders=[_order(15), _order(35), _order(55)],
        compose_ret="",
    )
    _enter(ctx)
    try:
        await sched_module._check_reorder_clients()
    finally:
        _exit(ctx)
    compose.assert_called_once()
    deliver.assert_not_called()


# ── _check_inactive_clients ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inactive_fires_for_silent_client():
    """Last order 90d ago, threshold 60 → win-back delivered."""
    deliver, compose, ctx = _patch(targets=[_target(name="Іван")], orders=[_order(90)])
    _enter(ctx)
    try:
        await sched_module._check_inactive_clients(days_threshold=60)
    finally:
        _exit(ctx)
    compose.assert_called_once()
    assert compose.call_args[0][1] == "Іван"
    deliver.assert_called_once_with("+380681234567", "Іван", "AI text")


@pytest.mark.asyncio
async def test_inactive_skips_active_client():
    """Ordered 30d ago, threshold 60 → still active → no message."""
    deliver, compose, ctx = _patch(targets=[_target()], orders=[_order(30)])
    _enter(ctx)
    try:
        await sched_module._check_inactive_clients(days_threshold=60)
    finally:
        _exit(ctx)
    deliver.assert_not_called()


# ── _notify_new_products ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_product_notifies_clients_with_orders():
    deliver = AsyncMock(return_value=True)
    compose = AsyncMock(return_value="AI new product")
    with patch.object(sched_module, "_tg_client", AsyncMock()), \
         patch.object(sched_module, "_deliver", deliver), \
         patch.object(sched_module.outbound, "compose_message", compose), \
         patch.object(sched_module, "_get_bas_outbound_targets", AsyncMock(return_value=[_target()])), \
         patch.object(sched_module, "_get_orders_for_client", AsyncMock(return_value=[_order(10)])), \
         patch("src.bas.get_new_products", AsyncMock(return_value=[{"name": "Гайка М10 А2", "price": 4.5}])):
        await sched_module._notify_new_products()
    compose.assert_called_once()
    deliver.assert_called_once()


@pytest.mark.asyncio
async def test_new_product_none_available_no_send():
    deliver = AsyncMock(return_value=True)
    with patch.object(sched_module, "_tg_client", AsyncMock()), \
         patch.object(sched_module, "_deliver", deliver), \
         patch("src.bas.get_new_products", AsyncMock(return_value=[])):
        await sched_module._notify_new_products()
    deliver.assert_not_called()


# ── _deliver ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deliver_resolves_and_sends():
    """_deliver resolves phone → entity, sends, records history."""
    mock_tg = AsyncMock()
    fake_entity = type("E", (), {"id": 777})()
    save = AsyncMock()
    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch.object(sched_module, "resolve_phone_entity", AsyncMock(return_value=fake_entity)), \
         patch.object(sched_module, "_save_proactive_message", save):
        ok = await sched_module._deliver("+380504442888", "Тест", "привіт")
    assert ok is True
    mock_tg.send_message.assert_called_once_with(fake_entity, "привіт")
    save.assert_called_once_with("777", "привіт")


@pytest.mark.asyncio
async def test_deliver_skips_unreachable():
    """If phone has no Telegram (resolve returns None) → no send, returns False."""
    mock_tg = AsyncMock()
    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch.object(sched_module, "resolve_phone_entity", AsyncMock(return_value=None)):
        ok = await sched_module._deliver("+380000000000", "Нема", "текст")
    assert ok is False
    mock_tg.send_message.assert_not_called()
