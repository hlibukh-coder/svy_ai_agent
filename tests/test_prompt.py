"""Tests for src/prompt.py — mock data and build_system_prompt."""
import pytest
from src.prompt import MOCK_CLIENTS, MOCK_PRODUCTS, build_system_prompt


def test_mock_clients_structure():
    assert isinstance(MOCK_CLIENTS, dict)
    for phone, client in MOCK_CLIENTS.items():
        assert phone.startswith("+")
        assert "id" in client
        assert "name" in client
        assert "orders" in client
        assert isinstance(client["orders"], list)


def test_mock_products_structure():
    assert isinstance(MOCK_PRODUCTS, list)
    assert len(MOCK_PRODUCTS) > 0
    for p in MOCK_PRODUCTS:
        assert "article" in p
        assert "name" in p
        assert "price" in p
        assert "stock" in p
        assert isinstance(p["price"], float)
        assert isinstance(p["stock"], int)


def test_build_system_prompt_new_client():
    prompt = build_system_prompt(None, None)
    assert "НОВЫЙ" in prompt
    assert "С.В.Ю" in prompt


def test_build_system_prompt_known_client_no_orders():
    client = {"id": "c1", "name": "Оксана", "company": "ФОП Ковальчук", "city": "Харків"}
    prompt = build_system_prompt(client, [])
    assert "Оксана" in prompt
    assert "заказов ещё не было" in prompt
    assert "Клиент — ПОСТОЯННЫЙ" not in prompt


def test_build_system_prompt_returning_client():
    client = {"id": "c1", "name": "Андрій", "company": "ТОВ Будмонтаж", "city": "Київ"}
    orders = [
        {
            "id": "order_001",
            "date": "15.05.2026",
            "total": 4200,
            "items": [{"name": "Болт М8×50", "qty": 500}],
        }
    ]
    prompt = build_system_prompt(client, orders)
    assert "Андрій" in prompt
    assert "Клиент — ПОСТОЯННЫЙ" in prompt
    assert "Болт М8×50" in prompt
    assert "4200" in prompt


def test_build_system_prompt_contains_base_rules():
    prompt = build_system_prompt(None, None)
    assert "get_products" in prompt
    assert "notify_manager" in prompt
    assert "ЭСКАЛАЦИЯ" in prompt


def test_build_system_prompt_client_with_phone():
    client = {"id": "c1", "name": "Тест", "phone": "+380681234567"}
    prompt = build_system_prompt(client, [])
    assert "+380681234567" in prompt
