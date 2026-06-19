"""Periodic BAS → PostgreSQL sync scheduler."""
import logging
import os
from datetime import datetime

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sync.client import BASClient
from sync.sync import run_full_sync, load_last_sync, save_last_sync

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "15"))

_scheduler: AsyncIOScheduler | None = None
_last_sync: datetime | None = None
_pool: asyncpg.Pool | None = None
_bas_client: BASClient | None = None


def get_pool() -> asyncpg.Pool | None:
    return _pool


async def init(pool: asyncpg.Pool, bas_client: BASClient):
    """Call once on app startup to set up the sync scheduler."""
    global _pool, _bas_client, _scheduler, _last_sync
    _pool = pool
    _bas_client = bas_client

    # Restore last sync time so we don't repeat a full sync after every restart
    _last_sync = await load_last_sync(pool)
    if _last_sync:
        logger.info(f"[SYNC SCHEDULER] Resuming incremental sync from {_last_sync}")

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _sync_job,
        trigger="interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="bas_sync",
    )
    _scheduler.start()
    logger.info(f"[SYNC SCHEDULER] started, interval={SYNC_INTERVAL_MINUTES}m")


async def _sync_job():
    global _last_sync
    if not _pool or not _bas_client:
        logger.warning("[SYNC SCHEDULER] pool or client not set, skipping")
        return
    logger.info("[SYNC SCHEDULER] Running scheduled sync")
    since = _last_sync
    try:
        await run_full_sync(_bas_client, _pool, since)
        _last_sync = datetime.now()
        await save_last_sync(_pool, _last_sync)
    except Exception as e:
        logger.error(f"[SYNC SCHEDULER] sync failed: {e}")


async def run_now():
    """Trigger an immediate full sync (e.g. on app startup)."""
    global _last_sync
    if not _pool or not _bas_client:
        logger.warning("[SYNC SCHEDULER] pool or client not set, cannot run now")
        return
    logger.info("[SYNC SCHEDULER] Running initial full sync now")
    try:
        await run_full_sync(_bas_client, _pool, since=None)
        _last_sync = datetime.now()
        await save_last_sync(_pool, _last_sync)
    except Exception as e:
        logger.error(f"[SYNC SCHEDULER] initial sync failed: {e}")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("[SYNC SCHEDULER] stopped")
