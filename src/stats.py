"""Dashboard analytics — counts and lists for the BAS-embedded panel."""
import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/history.db")

# Canonical channel buckets + display names (donut layout expects these names).
CHANNEL_NAMES = {"telegram": "Telegram", "whatsapp": "WhatsApp", "viber": "Viber", "email": "Email"}


def _channel_name(key: str) -> str:
    return CHANNEL_NAMES.get(key or "telegram", (key or "telegram").title())


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
                SELECT m.conv_id, m.channel, m.account_id, MAX(m.ts) last_ts,
                       (SELECT role FROM messages x WHERE x.conv_id=m.conv_id ORDER BY ts DESC LIMIT 1) last_role,
                       c.name, c.phone, a.label AS account_label
                FROM messages m
                LEFT JOIN contacts c ON c.conv_id = m.conv_id
                LEFT JOIN accounts a ON a.id = m.account_id
                GROUP BY m.conv_id ORDER BY last_ts DESC LIMIT ?
                """,
                (limit,),
            ) as c:
                rows = await c.fetchall()
        for r in rows:
            conv_id, channel, account_id, last_ts, last_role, name, phone, account_label = r
            status = "Очікує відповідь" if last_role == "user" else "В роботі"
            out.append({
                "chat_id": conv_id,
                "conv_id": conv_id,
                "name": name or phone or f"ID {conv_id}",
                "channel": _channel_name(channel),
                "account": account_label or "",
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
    """Real per-channel conversation counts, with per-account breakdown."""
    counts: dict[str, int] = {}
    by_account: dict[str, list] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel, COUNT(DISTINCT conv_id) FROM messages GROUP BY channel"
            ) as c:
                for ch, n in await c.fetchall():
                    counts[ch or "telegram"] = counts.get(ch or "telegram", 0) + n
            async with db.execute(
                """
                SELECT m.channel, m.account_id, a.label, COUNT(DISTINCT m.conv_id) n
                FROM messages m LEFT JOIN accounts a ON a.id = m.account_id
                GROUP BY m.channel, m.account_id
                """
            ) as c:
                for ch, acc, label, n in await c.fetchall():
                    by_account.setdefault(ch or "telegram", []).append(
                        {"account_id": acc, "label": label or f"#{acc}", "count": n})
    except Exception as e:
        logger.error(f"[STATS] channels error: {e}")
    total = sum(counts.values()) or 1
    out = []
    # Canonical buckets always present (for the donut), plus any extra channels seen.
    for key in list(CHANNEL_NAMES) + [k for k in counts if k not in CHANNEL_NAMES]:
        n = counts.get(key, 0)
        out.append({"name": _channel_name(key), "key": key, "count": n,
                    "pct": round(n * 100 / total), "by_account": by_account.get(key, [])})
    return out


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


# ── Attribution: dialogs/leads/orders/sales by channel AND account ───────────

async def by_channel() -> list[dict]:
    """What / where / from which channel+account: dialogs + leads (SQLite) joined
    with orders + sales + escalations (PG, attributed at write time)."""
    rows: dict[tuple, dict] = {}
    labels: dict[int, str] = {}

    def _row(ch, acc):
        return rows.setdefault((ch or "telegram", acc), {
            "dialogs": 0, "leads": 0, "orders": 0, "sales": 0.0, "escalations": 0})

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel, account_id, COUNT(DISTINCT conv_id) FROM messages GROUP BY channel, account_id"
            ) as c:
                for ch, acc, n in await c.fetchall():
                    _row(ch, acc)["dialogs"] = n
            async with db.execute(
                "SELECT channel, account_id, COUNT(*) FROM ("
                "  SELECT channel, account_id, conv_id, MIN(ts) m FROM messages GROUP BY conv_id"
                ") WHERE date(m)=date('now','localtime') GROUP BY channel, account_id"
            ) as c:
                for ch, acc, n in await c.fetchall():
                    _row(ch, acc)["leads"] = n
            async with db.execute("SELECT id, label FROM accounts") as c:
                for i, l in await c.fetchall():
                    labels[i] = l
    except Exception as e:
        logger.error(f"[STATS] by_channel sqlite error: {e}")

    pool = _pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                for r in await conn.fetch(
                    "SELECT channel, account_id, COUNT(*) n, COALESCE(SUM(amount),0) s "
                    "FROM orders WHERE number LIKE 'AI-%' GROUP BY channel, account_id"
                ):
                    row = _row(r["channel"], r["account_id"])
                    row["orders"] = r["n"]
                    row["sales"] = float(r["s"] or 0)
                for r in await conn.fetch(
                    "SELECT meta->>'channel' ch, meta->>'account_id' acc, COUNT(*) n "
                    "FROM agent_events WHERE kind='escalation' GROUP BY 1, 2"
                ):
                    acc = r["acc"]
                    acc = int(acc) if acc and str(acc).isdigit() else None
                    _row(r["ch"], acc)["escalations"] = r["n"]
        except Exception as e:
            logger.error(f"[STATS] by_channel pg error: {e}")

    out = []
    for (ch, acc), v in rows.items():
        out.append({
            "channel": _channel_name(ch), "channel_key": ch or "telegram",
            "account_id": acc, "account_label": labels.get(acc, f"#{acc}" if acc else "—"),
            **v,
        })
    out.sort(key=lambda x: (x["channel"], x["account_id"] or 0))
    return out
