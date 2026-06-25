"""
Agent configuration & event log — stored in PostgreSQL (agent_config, agent_events).

The dashboard (incl. the BAS-embedded panel) reads/writes these. The scheduler and
prompt builder read live config, so changing intervals/prompt from the UI takes
effect without a code change.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Defaults used when a key is missing / no DB. Editable from the Settings UI.
DEFAULTS: dict = {
    "agent_enabled": True,        # master switch: off → bot stops replying + no outbound
    "auto_reply": False,          # off → AI stays silent on inbound; replies ONLY when the
                                  #       operator triggers it ("AI, відповісти") per chat
    "system_prompt": "",          # "" → use code SYSTEM_PROMPT_BASE
    "send_hour": 10,              # hour of day proactive jobs run
    "reorder_enabled": True,
    "reorder_window_days": 1,     # fire within ±N days of the estimated reorder date
    "inactive_enabled": True,
    "inactive_days": 60,          # silence threshold for win-back
    "newproduct_enabled": True,
    "throttle_sec": 2,            # delay between outbound sends
    "max_per_run": 50,            # cap per campaign run
}


def _get_pool():
    try:
        from sync import scheduler_sync
        return scheduler_sync.get_pool()
    except Exception:
        return None


async def ensure_tables():
    """Create config/event tables if missing. Safe to call on every startup."""
    pool = _get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_config ("
                "  key TEXT PRIMARY KEY,"
                "  value JSONB NOT NULL,"
                "  updated_at TIMESTAMPTZ DEFAULT now())"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_events ("
                "  id SERIAL PRIMARY KEY,"
                "  ts TIMESTAMPTZ DEFAULT now(),"
                "  kind TEXT NOT NULL,"
                "  title TEXT NOT NULL,"
                "  meta JSONB DEFAULT '{}'::jsonb)"
            )
            # Channel/account attribution on AI orders (idempotent; orders table from migration.sql).
            try:
                await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS channel TEXT")
                await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS account_id INTEGER")
            except Exception as e:
                logger.warning(f"[CONFIG] orders attribution columns: {e}")
        logger.info("[CONFIG] tables ready")
    except Exception as e:
        logger.error(f"[CONFIG] ensure_tables error: {e}")


async def get_all() -> dict:
    """Return full config = DEFAULTS overlaid with stored values."""
    cfg = dict(DEFAULTS)
    pool = _get_pool()
    if not pool:
        return cfg
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM agent_config")
        for r in rows:
            v = r["value"]
            cfg[r["key"]] = json.loads(v) if isinstance(v, str) else v
    except Exception as e:
        logger.error(f"[CONFIG] get_all error: {e}")
    return cfg


async def get_value(key: str, default=None):
    cfg = await get_all()
    if key in cfg:
        return cfg[key]
    return default if default is not None else DEFAULTS.get(key)


async def set_many(values: dict):
    """Upsert several config keys at once."""
    pool = _get_pool()
    if not pool:
        logger.warning("[CONFIG] no pool, set_many skipped")
        return
    try:
        async with pool.acquire() as conn:
            for k, v in values.items():
                await conn.execute(
                    "INSERT INTO agent_config (key, value, updated_at) "
                    "VALUES ($1, $2::jsonb, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    k, json.dumps(v),
                )
        logger.info(f"[CONFIG] updated keys: {list(values)}")
    except Exception as e:
        logger.error(f"[CONFIG] set_many error: {e}")


async def log_event(kind: str, title: str, meta: dict | None = None):
    """Record an action for the 'Последние действия AI' feed."""
    pool = _get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_events (kind, title, meta) VALUES ($1, $2, $3::jsonb)",
                kind, title, json.dumps(meta or {}),
            )
    except Exception as e:
        logger.error(f"[CONFIG] log_event error: {e}")


async def recent_events(limit: int = 12) -> list[dict]:
    pool = _get_pool()
    if not pool:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ts, kind, title FROM agent_events ORDER BY ts DESC LIMIT $1", limit
            )
        return [{"ts": str(r["ts"]), "kind": r["kind"], "title": r["title"]} for r in rows]
    except Exception as e:
        logger.error(f"[CONFIG] recent_events error: {e}")
        return []
