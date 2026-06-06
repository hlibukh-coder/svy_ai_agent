"""Tests for src/bas.py — BAS OData layer (mock mode)."""
import os
import pytest

os.environ.setdefault("USE_MOCK", "true")

import src.bas as bas_module
from src.bas import get_client, get_orders, get_products, create_order


@pytest.mark.asyncio
async def test_get_client_known_phone():
    result = await get_client("+380681234567")
    assert result is not None
    assert result["id"] == "client_001"
    assert result["name"] == "Андрій"
    assert result["phone"] == "+380681234567"


@pytest.mark.asyncio
async def test_get_client_without_plus():
    result = await get_client("380681234567")
    assert result is not None
    assert result["id"] == "client_001"


@pytest.mark.asyncio
async def test_get_client_unknown_phone():
    result = await get_client("+380000000000")
    assert result is None


@pytest.mark.asyncio
async def test_get_client_second_known():
    result = await get_client("+380679876543")
    assert result is not None
    assert result["id"] == "client_002"
    assert result["name"] == "Оксана"


@pytest.mark.asyncio
async def test_get_orders_returning_client():
    orders = await get_orders("client_001")
    assert isinstance(orders, list)
    assert len(orders) == 1
    assert orders[0]["id"] == "order_001"
    assert orders[0]["total"] == 4200


@pytest.mark.asyncio
async def test_get_orders_no_orders_client():
    orders = await get_orders("client_002")
    assert isinstance(orders, list)
    assert len(orders) == 0


@pytest.mark.asyncio
async def test_get_orders_unknown_client():
    orders = await get_orders("nonexistent_id")
    assert orders == []


@pytest.mark.asyncio
async def test_get_products_by_name():
    results = await get_products("болт м8")
    assert isinstance(results, list)
    assert len(results) > 0
    assert any("М8" in p["name"] for p in results)


@pytest.mark.asyncio
async def test_get_products_by_article():
    results = await get_products("328558")
    assert len(results) == 1
    assert results[0]["article"] == "328558"
    assert results[0]["price"] == 4.20


@pytest.mark.asyncio
async def test_get_products_not_found():
    results = await get_products("несуществующий_товар_xyz")
    assert results == []


@pytest.mark.asyncio
async def test_get_products_case_insensitive():
    results = await get_products("ГАЙКА")
    assert len(results) > 0
    assert any("Гайка" in p["name"] for p in results)


@pytest.mark.asyncio
async def test_create_order_mock():
    items = [{"article": "328558", "name": "Болт М8×50", "qty": 100, "price": 4.20}]
    result = await create_order(
        client_id="client_001",
        client_name="Андрій",
        client_phone="+380681234567",
        company="ТОВ Будмонтаж",
        city="Київ",
        items=items,
        comment="Тест",
    )
    assert result["success"] is True
    assert "order_id" in result
    assert result["total"] == 420.0


@pytest.mark.asyncio
async def test_create_order_total_calculation():
    items = [
        {"article": "328558", "name": "Болт М8×50", "qty": 500, "price": 4.20},
        {"article": "215443", "name": "Гайка М8", "qty": 500, "price": 1.80},
    ]
    result = await create_order(
        client_id="client_001",
        client_name="Андрій",
        client_phone="+380681234567",
        company="",
        city="Київ",
        items=items,
    )
    assert result["success"] is True
    assert result["total"] == pytest.approx(3000.0)
