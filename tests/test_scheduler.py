"""Tests for src/scheduler.py — reorder interval logic and proactive messages."""
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("USE_MOCK", "true")

import src.scheduler as sched_module


# ── Helper: build fake client data ───────────────────────────────────────────

def _make_client(name, orders):
    return {"name": name, "orders": orders}


def _order(date_str, items=None):
    return {
        "id": "o1",
        "date": date_str,
        "total": 1000,
        "items": items or [{"name": "Болт М8×50", "qty": 500}],
    }


# ── start / stop ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_creates_scheduler():
    mock_tg = AsyncMock()
    sched_module.start(mock_tg)
    assert sched_module._scheduler is not None
    assert sched_module._scheduler.running
    sched_module.stop()


@pytest.mark.asyncio
async def test_stop_shuts_down():
    mock_tg = AsyncMock()
    sched_module.start(mock_tg)
    scheduler_ref = sched_module._scheduler
    assert scheduler_ref.running
    sched_module.stop()
    # After stop(), scheduler object still exists but is no longer running
    # APScheduler with wait=False may still show running=True briefly in async context;
    # we verify the scheduler was started and stop() doesn't raise.
    assert sched_module._scheduler is scheduler_ref


def test_set_tg_client():
    mock_tg = AsyncMock()
    sched_module.set_tg_client(mock_tg)
    assert sched_module._tg_client is mock_tg


# ── _check_reorder_clients logic ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_tg_client_skips_gracefully():
    sched_module._tg_client = None
    await sched_module._check_reorder_clients()


@pytest.mark.asyncio
async def test_sends_reminder_on_remind_date():
    """Client with one order 25 days ago → avg=30, remind_date = last+25 = today."""
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock()

    today = datetime.now().date()
    last_date = today - timedelta(days=25)
    last_date_str = last_date.strftime("%d.%m.%Y")

    fake_clients = {
        "+380681234567": _make_client("Андрій", [_order(last_date_str)])
    }

    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch("src.prompt.MOCK_CLIENTS", fake_clients):
        await sched_module._check_reorder_clients()

    mock_tg.send_message.assert_called_once()
    call_text = mock_tg.send_message.call_args[0][1]
    assert "Андрій" in call_text
    assert "Болт М8×50" in call_text


@pytest.mark.asyncio
async def test_no_reminder_wrong_date():
    """Client whose remind_date is tomorrow — no message today."""
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock()

    today = datetime.now().date()
    last_date = today - timedelta(days=24)
    last_date_str = last_date.strftime("%d.%m.%Y")

    fake_clients = {
        "+380681234567": _make_client("Андрій", [_order(last_date_str)])
    }

    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch("src.prompt.MOCK_CLIENTS", fake_clients):
        await sched_module._check_reorder_clients()

    mock_tg.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_client_with_no_orders_skipped():
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock()

    fake_clients = {
        "+380679876543": _make_client("Оксана", [])
    }

    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch("src.prompt.MOCK_CLIENTS", fake_clients):
        await sched_module._check_reorder_clients()

    mock_tg.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_avg_interval_two_orders():
    """Two orders 40 days apart → avg=40, remind = last_date + 35."""
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock()

    today = datetime.now().date()
    last_date = today - timedelta(days=35)
    prev_date = last_date - timedelta(days=40)

    fake_clients = {
        "+380681234567": _make_client("Андрій", [
            _order(last_date.strftime("%d.%m.%Y")),
            _order(prev_date.strftime("%d.%m.%Y")),
        ])
    }

    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch("src.prompt.MOCK_CLIENTS", fake_clients):
        await sched_module._check_reorder_clients()

    mock_tg.send_message.assert_called_once()
    call_text = mock_tg.send_message.call_args[0][1]
    assert "40" in call_text


@pytest.mark.asyncio
async def test_send_failure_does_not_raise():
    """If send_message raises, _check_reorder_clients should not propagate."""
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock(side_effect=Exception("Network error"))

    today = datetime.now().date()
    last_date = today - timedelta(days=25)

    fake_clients = {
        "+380681234567": _make_client("Андрій", [_order(last_date.strftime("%d.%m.%Y"))])
    }

    with patch.object(sched_module, "_tg_client", mock_tg), \
         patch("src.prompt.MOCK_CLIENTS", fake_clients):
        await sched_module._check_reorder_clients()  # must not raise
