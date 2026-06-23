"""Tests for src/tools.py — execute_tool, TOOLS_SCHEMA, escalation."""
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("ESCALATION_CHAT_ID", "123456")

import src.tools as tools_module
from src.tools import execute_tool, TOOLS_SCHEMA, set_tg_client


# ── Schema tests ──────────────────────────────────────────────────────────────

def test_tools_schema_is_list():
    assert isinstance(TOOLS_SCHEMA, list)
    assert len(TOOLS_SCHEMA) == 8


def test_tools_schema_names():
    names = {t["function"]["name"] for t in TOOLS_SCHEMA}
    assert names == {
        "get_products", "get_client", "get_orders", "create_order",
        "get_order_status", "check_supplier", "notify_manager", "send_file",
    }


def test_tools_schema_required_fields():
    for tool in TOOLS_SCHEMA:
        assert tool["type"] == "function"
        assert "name" in tool["function"]
        assert "description" in tool["function"]
        assert "parameters" in tool["function"]


def test_notify_manager_enum_values():
    nm = next(t for t in TOOLS_SCHEMA if t["function"]["name"] == "notify_manager")
    enum_vals = nm["function"]["parameters"]["properties"]["reason"]["enum"]
    assert "complaint" in enum_vals
    assert "discount_request" in enum_vals
    assert "client_request" in enum_vals


# ── execute_tool tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tool_get_products_found():
    result = await execute_tool("get_products", {"query": "болт м8"})
    data = json.loads(result)
    assert "products" in data
    assert len(data["products"]) > 0


@pytest.mark.asyncio
async def test_execute_tool_get_products_not_found():
    result = await execute_tool("get_products", {"query": "несуществующий_xyz"})
    data = json.loads(result)
    assert "result" in data
    assert "не найден" in data["result"]


@pytest.mark.asyncio
async def test_execute_tool_get_client_found():
    result = await execute_tool("get_client", {"phone": "+380681234567"})
    data = json.loads(result)
    assert "client" in data
    assert data["client"]["id"] == "client_001"


@pytest.mark.asyncio
async def test_execute_tool_get_client_not_found():
    result = await execute_tool("get_client", {"phone": "+380000000000"})
    data = json.loads(result)
    assert "result" in data
    assert "не найден" in data["result"]


@pytest.mark.asyncio
async def test_execute_tool_get_orders():
    result = await execute_tool("get_orders", {"client_id": "client_001"})
    data = json.loads(result)
    assert "orders" in data
    assert len(data["orders"]) == 1


@pytest.mark.asyncio
async def test_execute_tool_get_orders_empty():
    result = await execute_tool("get_orders", {"client_id": "client_002"})
    data = json.loads(result)
    assert data["orders"] == []


@pytest.mark.asyncio
async def test_execute_tool_create_order():
    args = {
        "client_id": "client_001",
        "client_name": "Андрій",
        "client_phone": "+380681234567",
        "company": "ТОВ Будмонтаж",
        "city": "Київ",
        "items": [{"article": "328558", "name": "Болт М8×50", "qty": 100, "price": 4.20}],
        "comment": "",
    }
    result = await execute_tool("create_order", args)
    data = json.loads(result)
    assert data["success"] is True
    assert "order_id" in data


@pytest.mark.asyncio
async def test_execute_tool_unknown_tool():
    result = await execute_tool("unknown_tool", {})
    data = json.loads(result)
    assert "error" in data
    assert "Unknown tool" in data["error"]


@pytest.mark.asyncio
async def test_execute_tool_notify_manager_no_client():
    """notify_manager without tg client set — should not raise, just log warning."""
    set_tg_client(None, None)
    result = await execute_tool(
        "notify_manager",
        {"reason": "complaint", "summary": "Клієнт незадоволений"},
        sender_phone="+380681234567",
    )
    data = json.loads(result)
    assert "Менеджер уведомлён" in data["result"]


@pytest.mark.asyncio
async def test_execute_tool_notify_manager_with_tg_client():
    mock_tg = AsyncMock()
    mock_tg.send_message = AsyncMock()
    set_tg_client(mock_tg, escalation_peer="123456")

    result = await execute_tool(
        "notify_manager",
        {"reason": "discount_request", "summary": "Просить знижку 10%"},
        sender_phone="+380681234567",
    )
    data = json.loads(result)
    assert "Менеджер уведомлён" in data["result"]
    mock_tg.send_message.assert_called_once()
    call_args = mock_tg.send_message.call_args
    assert "discount_request" in call_args[0][1] or "discount_request" in str(call_args)
