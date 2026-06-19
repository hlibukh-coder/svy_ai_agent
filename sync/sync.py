"""Sync logic: pulls data from BAS OData and upserts into PostgreSQL."""
import logging
from datetime import datetime, date

import asyncpg

from sync.client import BASClient

logger = logging.getLogger(__name__)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def sync_products(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_products(since)
    if not items:
        logger.info("[SYNC] products: no data")
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO products (ref_key, name, code, deleted, price, stock)
            VALUES ($1, $2, $3, $4, 0, 0)
            ON CONFLICT (ref_key) DO UPDATE
            SET name       = EXCLUDED.name,
                code       = EXCLUDED.code,
                deleted    = EXCLUDED.deleted,
                updated_at = now()
            """,
            [
                (
                    i.get("Ref_Key", ""),
                    i.get("Description", ""),
                    i.get("Артикул", "") or "",
                    bool(i.get("DeletionMark", False)),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] products: upserted {len(items)}")


async def sync_prices(client: BASClient, pool: asyncpg.Pool):
    """Update product prices from InformationRegister_ЦеныНоменклатуры."""
    items = await client.get_prices()
    if not items:
        logger.info("[SYNC] prices: no data")
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            "UPDATE products SET price = $2, updated_at = now() WHERE ref_key = $1",
            [
                (i.get("Номенклатура_Key", ""), float(i.get("Цена", 0) or 0))
                for i in items
            ],
        )
    logger.info(f"[SYNC] prices: updated {len(items)}")


async def sync_stock(client: BASClient, pool: asyncpg.Pool):
    """Calculate stock from AccumulationRegister_ЗапасыНаСкладах movements (Receipt - Expense)."""
    records = await client.get_stock()
    if not records:
        logger.info("[SYNC] stock: no data")
        return

    totals: dict[str, float] = {}
    for rec in records:
        for row in rec.get("RecordSet", []):
            if not row.get("Active"):
                continue
            prod_key = row.get("Номенклатура_Key", "")
            if not prod_key or prod_key == "00000000-0000-0000-0000-000000000000":
                continue
            qty = float(row.get("Количество", 0) or 0)
            if row.get("RecordType") == "Receipt":
                totals[prod_key] = totals.get(prod_key, 0) + qty
            elif row.get("RecordType") == "Expense":
                totals[prod_key] = totals.get(prod_key, 0) - qty

    rows = [(k, max(v, 0)) for k, v in totals.items()]
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM stock")
        if rows:
            await conn.executemany(
                "INSERT INTO stock (product_ref_key, quantity) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                rows,
            )
            await conn.executemany(
                "UPDATE products SET stock = $2, updated_at = now() WHERE ref_key = $1",
                rows,
            )
    logger.info(f"[SYNC] stock: {len(rows)} products, {sum(v for _, v in rows):.0f} total units")


async def sync_clients(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_clients(since)
    if not items:
        logger.info("[SYNC] clients: no data")
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO clients (ref_key, name, code, phone, company, city, deleted)
            VALUES ($1, $2, $3, $4, $5, '', $6)
            ON CONFLICT (ref_key) DO UPDATE
            SET name       = EXCLUDED.name,
                code       = EXCLUDED.code,
                phone      = EXCLUDED.phone,
                company    = EXCLUDED.company,
                deleted    = EXCLUDED.deleted,
                updated_at = now()
            """,
            [
                (
                    i.get("Ref_Key", ""),
                    i.get("Description", ""),
                    i.get("Code", "") or "",
                    i.get("НомерТелефонаДляПоиска", "") or "",
                    i.get("НаименованиеПолное", "") or "",
                    bool(i.get("DeletionMark", False)),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] clients: upserted {len(items)}")


async def sync_orders(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_orders(since)
    # Only posted (проведённые) documents; field is "Posted" not "Проведен"
    items = [i for i in items if i.get("Posted")]
    if not items:
        logger.info("[SYNC] orders: no posted orders")
        return

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO orders (ref_key, number, date, client_ref_key, amount)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (ref_key) DO NOTHING
            """,
            [
                (
                    i.get("Ref_Key", ""),
                    i.get("Number", "") or "",
                    _parse_date(i.get("Date")),
                    i.get("Контрагент_Key", ""),
                    float(i.get("СуммаДокумента", 0) or 0),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] orders: inserted {len(items)}")


async def load_last_sync(pool: asyncpg.Pool) -> datetime | None:
    """Read persisted last-sync timestamp from DB."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM sync_state WHERE key = 'last_sync'")
        if row:
            return datetime.fromisoformat(row["value"])
    except Exception as e:
        logger.error(f"[SYNC] load_last_sync error: {e}")
    return None


async def save_last_sync(pool: asyncpg.Pool, ts: datetime):
    """Persist last-sync timestamp so incremental sync survives restarts."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sync_state (key, value) VALUES ('last_sync', $1)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                ts.isoformat(),
            )
    except Exception as e:
        logger.error(f"[SYNC] save_last_sync error: {e}")


async def run_full_sync(
    client: BASClient,
    pool: asyncpg.Pool,
    since: datetime | None = None,
):
    logger.info(f"[SYNC] Starting full sync (since={since})")
    await sync_products(client, pool, since)
    await sync_prices(client, pool)
    await sync_stock(client, pool)
    await sync_clients(client, pool, since)
    await sync_orders(client, pool, since)
    logger.info("[SYNC] Full sync done")
