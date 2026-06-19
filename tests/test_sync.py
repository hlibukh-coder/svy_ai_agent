"""Tests for sync module — correct document types, order items storage, last_sync persistence."""
import inspect
import os
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("USE_MOCK", "true")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_order(ref_key="order-001", posted=True, товары=None):
    # BAS real field names: Posted (not Проведен), Number (not Номер)
    return {
        "Ref_Key": ref_key,
        "Number": "ЗКП-000001",
        "Date": "2026-05-15T00:00:00",
        "Контрагент_Key": "client-001",
        "СуммаДокумента": 4200.0,
        "Posted": posted,
    }


class _MockConn:
    """Captures executemany/execute calls for assertions."""

    def __init__(self):
        self.orders_inserted: list = []
        self.items_inserted: list = []
        self.deleted_order_ids: list = []

    async def executemany(self, sql: str, rows):
        if "INSERT INTO orders" in sql:
            self.orders_inserted.extend(rows)
        elif "INSERT INTO order_items" in sql:
            self.items_inserted.extend(rows)

    async def execute(self, sql: str, *args):
        if "DELETE FROM order_items" in sql and args:
            self.deleted_order_ids.extend(args[0])

    async def fetchrow(self, *a, **kw):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _MockPool:
    def __init__(self, conn: _MockConn):
        self._conn = conn

    def acquire(self):
        return self._conn


# ── Document type sanity checks ───────────────────────────────────────────────

def test_client_orders_uses_zak_pokupatelya():
    """BASClient.get_orders must target Document_ЗаказПокупателя, not Расходная."""
    from sync.client import BASClient
    source = inspect.getsource(BASClient.get_orders)
    assert "Document_ЗаказПокупателя" in source, "get_orders must use Document_ЗаказПокупателя"
    assert "Document_РасходнаяНакладная" not in source, "get_orders must NOT use РасходнаяНакладная"


def test_client_order_items_uses_zak_pokupatelya():
    """BASClient.get_order_items must target Document_ЗаказПокупателя."""
    from sync.client import BASClient
    source = inspect.getsource(BASClient.get_order_items)
    assert "Document_ЗаказПокупателя" in source
    assert "Document_РасходнаяНакладная" not in source


def test_client_orders_does_not_use_expand():
    """BASClient.get_orders must NOT use $expand=Товары — BAS returns HTTP 400 for it."""
    from sync.client import BASClient
    source = inspect.getsource(BASClient.get_orders)
    assert "$expand" not in source, "get_orders must not use $expand (not supported by this BAS)"


def test_client_orders_uses_correct_field_names():
    """BASClient.get_orders must use Posted and Number, not Проведен and Номер."""
    from sync.client import BASClient
    source = inspect.getsource(BASClient.get_orders)
    assert "Posted" in source
    assert "Number" in source
    assert "Проведен" not in source
    assert "Номер" not in source


# ── sync_orders — order rows ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_orders_inserts_order_row():
    from sync.sync import sync_orders

    class Client:
        async def get_orders(self, since=None):
            return [_make_order()]

    conn = _MockConn()
    await sync_orders(Client(), _MockPool(conn))

    assert len(conn.orders_inserted) == 1
    row = conn.orders_inserted[0]
    assert row[0] == "order-001"   # ref_key
    assert row[1] == "ЗКП-000001"  # number (from Number field)
    assert row[3] == "client-001"  # client_ref_key
    assert row[4] == 4200.0        # amount


@pytest.mark.asyncio
async def test_sync_orders_skips_unposted():
    from sync.sync import sync_orders

    class Client:
        async def get_orders(self, since=None):
            return [_make_order(posted=False)]

    conn = _MockConn()
    await sync_orders(Client(), _MockPool(conn))

    assert conn.orders_inserted == []


@pytest.mark.asyncio
async def test_sync_orders_handles_multiple_orders():
    from sync.sync import sync_orders

    class Client:
        async def get_orders(self, since=None):
            return [_make_order("order-001"), _make_order("order-002")]

    conn = _MockConn()
    await sync_orders(Client(), _MockPool(conn))

    assert len(conn.orders_inserted) == 2
    assert {r[0] for r in conn.orders_inserted} == {"order-001", "order-002"}


# ── last_sync persistence ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_last_sync_returns_none_when_missing():
    from sync.sync import load_last_sync

    class Conn:
        async def fetchrow(self, *a, **kw): return None
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class Pool:
        def acquire(self): return Conn()

    result = await load_last_sync(Pool())
    assert result is None


@pytest.mark.asyncio
async def test_load_last_sync_parses_iso_datetime():
    from sync.sync import load_last_sync

    stored = "2026-05-15T12:00:00"

    class Conn:
        async def fetchrow(self, *a, **kw): return {"value": stored}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class Pool:
        def acquire(self): return Conn()

    result = await load_last_sync(Pool())
    assert result == datetime(2026, 5, 15, 12, 0, 0)


@pytest.mark.asyncio
async def test_save_last_sync_upserts_value():
    from sync.sync import save_last_sync

    executed = []

    class Conn:
        async def execute(self, sql, *args): executed.append((sql, args))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class Pool:
        def acquire(self): return Conn()

    ts = datetime(2026, 6, 1, 9, 0, 0)
    await save_last_sync(Pool(), ts)

    assert executed
    sql, args = executed[0]
    assert "last_sync" in sql
    assert "2026-06-01T09:00:00" in args[0]


# ── Agent reacts to order items (via build_system_prompt) ─────────────────────

def test_system_prompt_includes_items_from_pg_style_orders():
    """build_system_prompt must surface order items so the agent can suggest repeats."""
    from src.prompt import build_system_prompt

    client = {"id": "c1", "name": "Андрій", "company": "ТОВ Будмонтаж", "city": "Київ"}
    # Same format as PG get_orders returns
    orders = [
        {
            "id": "order-001",
            "number": "ЗКП-000001",
            "date": "2026-05-15",
            "total": 4200.0,
            "items": [
                {"name": "Болт М8×50 DIN 933 цинк", "qty": 500},
                {"name": "Гайка М8 DIN 934 цинк", "qty": 500},
            ],
        }
    ]
    prompt = build_system_prompt(client, orders)

    assert "Болт М8×50 DIN 933 цинк" in prompt
    assert "ПОСТОЯННЫЙ" in prompt
    assert "4200" in prompt


def test_system_prompt_no_items_does_not_crash():
    """build_system_prompt must not crash when order has empty items list."""
    from src.prompt import build_system_prompt

    client = {"id": "c1", "name": "Оксана"}
    orders = [{"id": "o1", "date": "2026-05-01", "total": 1000, "items": []}]
    prompt = build_system_prompt(client, orders)
    assert "Оксана" in prompt


# ── Scheduler outbound targeting ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_bas_outbound_targets_mock_mode():
    """In USE_MOCK mode, outbound targeting returns empty (no PG)."""
    import src.scheduler as sched

    with patch.dict(os.environ, {"USE_MOCK": "true"}):
        result = await sched._get_bas_outbound_targets()

    assert result == []
