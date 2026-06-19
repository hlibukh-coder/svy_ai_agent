"""Dashboard analytics — counts and lists for the BAS-embedded panel."""
import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/history.db")


def _pool():
    try:
        from sync import scheduler_sync
        return scheduler_sync.get_pool()
    except Exception:
        return None


# ── Overview cards ───────────────────────────────────────────────────────────

async def overview() -> dict:
    res = {
        "dialogs_today": 0, "new_leads": 0, "ai_orders_today": 0,
        "ai_sales_today": 0, "need_attention": 0, "unanswered": 0,
    }
    # SQLite: dialogs / leads / unanswered
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM messages WHERE date(ts)=date('now','localtime')"
            ) as c:
                res["dialogs_today"] = (await c.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM (SELECT chat_id, MIN(ts) m FROM messages GROUP BY chat_id) "
                "WHERE date(m)=date('now','localtime')"
            ) as c:
                res["new_leads"] = (await c.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT chat_id, (SELECT role FROM messages m2 WHERE m2.chat_id=m.chat_id "
                "    ORDER BY ts DESC LIMIT 1) last_role FROM messages m GROUP BY chat_id"
                ") WHERE last_role='user'"
            ) as c:
                res["unanswered"] = (await c.fetchone())[0]
    except Exception as e:
        logger.error(f"[STATS] sqlite overview error: {e}")

    # PG: AI orders + sales + escalations
    pool = _pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM orders "
                    "WHERE number LIKE 'AI-%' AND date = CURRENT_DATE"
                )
                res["ai_orders_today"] = row["n"]
                res["ai_sales_today"] = float(row["s"] or 0)
                try:
                    res["need_attention"] = await conn.fetchval(
                        "SELECT COUNT(*) FROM agent_events "
                        "WHERE kind='escalation' AND ts::date = CURRENT_DATE"
                    ) or 0
                except Exception:
                    res["need_attention"] = 0
        except Exception as e:
            logger.error(f"[STATS] pg overview error: {e}")
    return res


# ── Active dialogs ───────────────────────────────────────────────────────────

async def active_dialogs(limit: int = 8) -> list[dict]:
    out = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT m.chat_id, MAX(m.ts) last_ts,
                       (SELECT role FROM messages x WHERE x.chat_id=m.chat_id ORDER BY ts DESC LIMIT 1) last_role,
                       tc.name, tc.phone, tc.client_ref_key
                FROM messages m
                LEFT JOIN telegram_clients tc ON tc.chat_id = m.chat_id
                GROUP BY m.chat_id ORDER BY last_ts DESC LIMIT ?
                """,
                (limit,),
            ) as c:
                rows = await c.fetchall()
        for r in rows:
            chat_id, last_ts, last_role, name, phone, ref = r
            status = "Очікує відповідь" if last_role == "user" else "В роботі"
            out.append({
                "chat_id": chat_id,
                "name": name or phone or f"ID {chat_id}",
                "channel": "Telegram",
                "status": status,
                "last_ts": last_ts,
            })
    except Exception as e:
        logger.error(f"[STATS] active_dialogs error: {e}")
    return out


# ── Sales opportunities ──────────────────────────────────────────────────────

async def opportunities() -> dict:
    res = {"inactive_30d": 0, "reorder_ready": 0, "in_stock_positions": 0}
    pool = _pool()
    if not pool:
        return res
    try:
        async with pool.acquire() as conn:
            res["inactive_30d"] = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                  SELECT c.ref_key, MAX(o.date) last
                  FROM clients c JOIN orders o ON o.client_ref_key=c.ref_key
                  WHERE c.deleted=false AND c.phone IS NOT NULL AND c.phone!=''
                  GROUP BY c.ref_key
                ) t WHERE t.last < CURRENT_DATE - 30
                """
            ) or 0
            res["reorder_ready"] = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                  SELECT c.ref_key, MAX(o.date) last,
                         (MAX(o.date)-MIN(o.date))::float / NULLIF(COUNT(*)-1,0) avg_int
                  FROM clients c JOIN orders o ON o.client_ref_key=c.ref_key
                  WHERE c.deleted=false AND c.phone IS NOT NULL AND c.phone!=''
                  GROUP BY c.ref_key HAVING COUNT(*) >= 2
                ) t
                WHERE ABS((t.last + (GREATEST(7, t.avg_int)-5)::int) - CURRENT_DATE) <= 3
                """
            ) or 0
            res["in_stock_positions"] = await conn.fetchval(
                "SELECT COUNT(*) FROM products WHERE deleted=false AND stock > 0"
            ) or 0
    except Exception as e:
        logger.error(f"[STATS] opportunities error: {e}")
    return res


# ── Channel distribution ─────────────────────────────────────────────────────

async def channels() -> list[dict]:
    """Only Telegram is implemented; others shown at 0 for the layout."""
    tg = 0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(DISTINCT chat_id) FROM messages") as c:
                tg = (await c.fetchone())[0]
    except Exception as e:
        logger.error(f"[STATS] channels error: {e}")
    total = tg or 1
    return [
        {"name": "Telegram", "count": tg, "pct": round(tg * 100 / total)},
        {"name": "WhatsApp", "count": 0, "pct": 0},
        {"name": "Viber", "count": 0, "pct": 0},
        {"name": "Телефон", "count": 0, "pct": 0},
    ]


# ── Campaign preview (estimated reach) ───────────────────────────────────────

async def campaign_preview(kind: str) -> int:
    pool = _pool()
    if not pool:
        return 0
    try:
        async with pool.acquire() as conn:
            if kind == "inactive":
                return await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM (
                      SELECT c.ref_key, MAX(o.date) last
                      FROM clients c JOIN orders o ON o.client_ref_key=c.ref_key
                      WHERE c.deleted=false AND c.phone IS NOT NULL AND c.phone!=''
                      GROUP BY c.ref_key
                    ) t WHERE t.last < CURRENT_DATE - 60
                    """
                ) or 0
            if kind == "reorder":
                return await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM (
                      SELECT c.ref_key, MAX(o.date) last,
                             (MAX(o.date)-MIN(o.date))::float / NULLIF(COUNT(*)-1,0) avg_int
                      FROM clients c JOIN orders o ON o.client_ref_key=c.ref_key
                      WHERE c.deleted=false AND c.phone IS NOT NULL AND c.phone!=''
                      GROUP BY c.ref_key HAVING COUNT(*) >= 2
                    ) t WHERE ABS((t.last + (GREATEST(7, t.avg_int)-5)::int) - CURRENT_DATE) <= 3
                    """
                ) or 0
            if kind == "newproduct":
                return await conn.fetchval(
                    """
                    SELECT COUNT(DISTINCT c.ref_key)
                    FROM clients c JOIN orders o ON o.client_ref_key=c.ref_key
                    WHERE c.deleted=false AND c.phone IS NOT NULL AND c.phone!=''
                    """
                ) or 0
    except Exception as e:
        logger.error(f"[STATS] campaign_preview error: {e}")
    return 0
