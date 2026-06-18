"""Sync logic: pulls data from BAS OData and upserts into PostgreSQL."""
import logging
from datetime import datetime

import asyncpg

from sync.client import BASClient

logger = logging.getLogger(__name__)


async def sync_products(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_products(since)
    if not items:
        logger.info("[SYNC] products: no data")
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO products (ref_key, name, code, deleted, price, stock)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (ref_key) DO UPDATE
            SET name       = EXCLUDED.name,
                code       = EXCLUDED.code,
                deleted    = EXCLUDED.deleted,
                price      = EXCLUDED.price,
                stock      = EXCLUDED.stock,
                updated_at = now()
            """,
            [
                (
                    i.get("Ref_Key", ""),
                    i.get("Description", ""),
                    i.get("Код", ""),
                    bool(i.get("DeletionMark", False)),
                    float(i.get("ЦенаПродажи", 0) or 0),
                    float(i.get("Остаток", 0) or 0),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] products: upserted {len(items)}")


async def sync_stock(client: BASClient, pool: asyncpg.Pool):
    items = await client.get_stock()
    if not items:
        logger.info("[SYNC] stock: no data")
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM stock")
        await conn.executemany(
            "INSERT INTO stock (product_ref_key, quantity) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [
                (i.get("Номенклатура_Key", ""), float(i.get("КоличествоBalance", 0) or 0))
                for i in items
            ],
        )
    logger.info(f"[SYNC] stock: refreshed {len(items)} rows")


async def sync_clients(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_clients(since)
    if not items:
        logger.info("[SYNC] clients: no data")
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO clients (ref_key, name, code, phone, company, city, deleted)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (ref_key) DO UPDATE
            SET name       = EXCLUDED.name,
                code       = EXCLUDED.code,
                phone      = EXCLUDED.phone,
                company    = EXCLUDED.company,
                city       = EXCLUDED.city,
                deleted    = EXCLUDED.deleted,
                updated_at = now()
            """,
            [
                (
                    i.get("Ref_Key", ""),
                    i.get("Description", ""),
                    i.get("Код", ""),
                    i.get("НомерТелефона", "") or "",
                    i.get("НаименованиеПолное", "") or "",
                    i.get("Город", "") or "",
                    bool(i.get("DeletionMark", False)),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] clients: upserted {len(items)}")


async def sync_orders(client: BASClient, pool: asyncpg.Pool, since: datetime | None = None):
    items = await client.get_orders(since)
    # Only posted (проведённые) documents
    items = [i for i in items if i.get("Проведен")]
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
                    i.get("Номер", "") or "",
                    i.get("Date", "")[:10] if i.get("Date") else None,
                    i.get("Контрагент_Key", ""),
                    float(i.get("СуммаДокумента", 0) or 0),
                )
                for i in items
            ],
        )
    logger.info(f"[SYNC] orders: inserted {len(items)}")


async def run_full_sync(
    client: BASClient,
    pool: asyncpg.Pool,
    since: datetime | None = None,
):
    logger.info(f"[SYNC] Starting full sync (since={since})")
    await sync_products(client, pool, since)
    await sync_clients(client, pool, since)
    await sync_orders(client, pool, since)
    await sync_stock(client, pool)
    logger.info("[SYNC] Full sync done")
