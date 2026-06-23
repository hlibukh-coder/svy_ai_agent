import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.index import send_to_client

logger = logging.getLogger(__name__)

app = FastAPI()

USE_MOCK = os.getenv("USE_MOCK", "true").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", "")


class SendRequest(BaseModel):
    phone: str
    text: str


@app.get("/")
async def root():
    return {"status": "ok", "dashboard": "/dashboard"}


@app.post("/send")
async def send_message(req: SendRequest):
    if not req.phone or not req.text:
        raise HTTPException(status_code=400, detail="phone and text are required")
    result = await send_to_client(req.phone, req.text)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result


# ── Dashboard API ─────────────────────────────────────────────────────────────

@app.get("/api/chats")
async def api_chats():
    """Return all chats sorted by last activity."""
    from src.context import get_all_chats
    chats = await get_all_chats(limit=200)

    # Enrich with PG client info if available
    if not USE_MOCK and DATABASE_URL:
        try:
            from sync import scheduler_sync
            pool = scheduler_sync.get_pool()
            if pool:
                async with pool.acquire() as conn:
                    for ch in chats:
                        if ch.get("client_ref_key"):
                            row = await conn.fetchrow(
                                "SELECT name, phone, company FROM clients WHERE ref_key=$1",
                                ch["client_ref_key"],
                            )
                            if row:
                                ch["name"] = ch["name"] or row["name"]
                                ch["phone"] = ch["phone"] or row["phone"]
                                ch["company"] = row["company"] or ""
        except Exception as e:
            logger.warning(f"[DASHBOARD] PG enrich error: {e}")

    return chats


@app.get("/api/chat/{chat_id}")
async def api_chat(chat_id: str):
    """Return full conversation + client info for a chat."""
    from src.context import load_history, get_linked_client
    import aiosqlite

    db_path = os.getenv("DB_PATH", "data/history.db")

    # Full history (no limit for dashboard)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT role, content, ts FROM messages WHERE chat_id=? ORDER BY ts ASC",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()

    messages = [{"role": r[0], "content": r[1], "ts": r[2]} for r in rows]

    linked = await get_linked_client(chat_id)
    client_info = {}

    if linked and not USE_MOCK and DATABASE_URL:
        try:
            from sync import scheduler_sync
            pool = scheduler_sync.get_pool()
            if pool and linked.get("client_ref_key"):
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT name, phone, company, city FROM clients WHERE ref_key=$1",
                        linked["client_ref_key"],
                    )
                    if row:
                        client_info = dict(row)
                    orders = await conn.fetch(
                        "SELECT number, date, amount FROM orders WHERE client_ref_key=$1 ORDER BY date DESC LIMIT 5",
                        linked["client_ref_key"],
                    )
                    client_info["orders"] = [
                        {"number": o["number"], "date": str(o["date"]), "amount": float(o["amount"] or 0)}
                        for o in orders
                    ]
        except Exception as e:
            logger.warning(f"[DASHBOARD] PG client info error: {e}")

    if not client_info and linked:
        client_info = {"name": linked.get("name", ""), "phone": linked.get("phone", "")}

    from src.context import is_chat_paused
    ai_paused = await is_chat_paused(chat_id)

    return {"chat_id": chat_id, "messages": messages, "client": client_info, "ai_paused": ai_paused}


class ChatSendRequest(BaseModel):
    text: str


@app.post("/api/chat/{chat_id}/send")
async def api_chat_operator_send(chat_id: str, req: ChatSendRequest):
    """Operator (human) sends a raw message into a chat; pauses the AI for this chat."""
    from src.index import operator_send
    result = await operator_send(chat_id, req.text)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "send failed"))
    return result


@app.post("/api/chat/{chat_id}/ai")
async def api_chat_toggle_ai(chat_id: str, payload: dict):
    """Enable/disable the AI for a single chat (human takeover toggle)."""
    from src import context
    enabled = bool(payload.get("enabled", True))
    await context.set_chat_ai_paused(chat_id, not enabled)
    return {"chat_id": chat_id, "ai_enabled": enabled}


# ── Analytics API (overview / opportunities / channels / actions) ─────────────

@app.get("/api/stats/overview")
async def api_overview():
    from src import stats
    return await stats.overview()


@app.get("/api/stats/active-dialogs")
async def api_active_dialogs():
    from src import stats
    return await stats.active_dialogs(limit=8)


@app.get("/api/stats/opportunities")
async def api_opportunities():
    from src import stats
    return await stats.opportunities()


@app.get("/api/stats/channels")
async def api_channels():
    from src import stats
    return await stats.channels()


@app.get("/api/stats/recent-actions")
async def api_recent_actions():
    from src import config
    return await config.recent_events(limit=12)


# ── Clients list (for manual targeting) ──────────────────────────────────────

@app.get("/api/clients")
async def api_clients(search: str = "", limit: int = 50, offset: int = 0):
    if USE_MOCK or not DATABASE_URL:
        return []
    try:
        from sync import scheduler_sync
        pool = scheduler_sync.get_pool()
        if not pool:
            return []
        pattern = f"%{search}%" if search else "%"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.ref_key::text AS ref_key, c.name, c.phone, c.company,
                       MAX(o.date)::text AS last_order
                FROM clients c
                LEFT JOIN orders o ON o.client_ref_key = c.ref_key
                WHERE c.deleted = false
                  AND c.phone IS NOT NULL AND c.phone != ''
                  AND (c.name ILIKE $1 OR c.phone ILIKE $1 OR c.company ILIKE $1)
                GROUP BY c.ref_key, c.name, c.phone, c.company
                ORDER BY MAX(o.date) DESC NULLS LAST
                LIMIT $2 OFFSET $3
                """,
                pattern, limit, offset,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[CLIENTS] list error: {e}")
        return []


# ── Campaigns API ─────────────────────────────────────────────────────────────

@app.get("/api/campaigns/preview")
async def api_campaign_preview(kind: str):
    from src import stats
    return {"kind": kind, "count": await stats.campaign_preview(kind)}


@app.post("/api/campaigns/run")
async def api_campaign_run(payload: dict):
    from src import scheduler, config
    kind = payload.get("kind", "")
    if kind not in ("reorder", "inactive", "newproduct"):
        raise HTTPException(status_code=400, detail="bad kind")
    if scheduler._tg_client is None:
        raise HTTPException(status_code=409, detail="Telegram не підключено — підключіть через QR у Налаштуваннях")
    if not await config.get_value("agent_enabled", True):
        raise HTTPException(status_code=409, detail="Агент на паузі — увімкніть його кнопкою зверху")
    if scheduler.running_campaign():
        raise HTTPException(status_code=409, detail=f"Вже виконується розсилка: {scheduler.running_campaign()}")
    from src import stats
    est = await stats.campaign_preview(kind)
    asyncio.create_task(scheduler.run_campaign(kind))
    return {"started": True, "kind": kind, "estimated": est}


class ManualSendRequest(BaseModel):
    client_ref_keys: list[str]
    message: str = ""
    kind: str = ""


@app.post("/api/campaigns/send-manual")
async def api_send_manual(req: ManualSendRequest):
    from src import scheduler, config
    if scheduler._tg_client is None:
        raise HTTPException(status_code=409, detail="Telegram не підключено — підключіть через QR у Налаштуваннях")
    if not await config.get_value("agent_enabled", True):
        raise HTTPException(status_code=409, detail="Агент на паузі — увімкніть його кнопкою зверху")
    if not req.client_ref_keys:
        raise HTTPException(status_code=400, detail="Не обрано клієнтів")
    if not req.message and not req.kind:
        raise HTTPException(status_code=400, detail="Вкажіть текст або тип AI-кампанії")
    asyncio.create_task(scheduler.run_manual(req.client_ref_keys, req.message, req.kind))
    return {"started": True, "count": len(req.client_ref_keys)}


@app.post("/api/campaigns/stop")
async def api_campaign_stop():
    from src import scheduler
    running = scheduler.running_campaign()
    scheduler.cancel_campaign()
    return {"stopped": True, "was_running": running}


# ── Agent master switch ───────────────────────────────────────────────────────

@app.get("/api/agent/state")
async def api_agent_state():
    from src import config, scheduler
    return {
        "agent_enabled": bool(await config.get_value("agent_enabled", True)),
        "running_campaign": scheduler.running_campaign(),
    }


@app.post("/api/agent/toggle")
async def api_agent_toggle(payload: dict):
    from src import config
    enabled = bool(payload.get("enabled", True))
    await config.set_many({"agent_enabled": enabled})
    await config.log_event("agent", "AI-агента увімкнено" if enabled else "AI-агента поставлено на паузу")
    return {"agent_enabled": enabled}


# ── Config API (prompt + intervals) ───────────────────────────────────────────

@app.get("/api/config")
async def api_get_config():
    from src import config
    return await config.get_all()


@app.post("/api/config")
async def api_set_config(payload: dict):
    from src import config, scheduler
    await config.set_many(payload)
    if "send_hour" in payload:
        try:
            scheduler.reschedule(int(payload["send_hour"]))
        except Exception:
            pass
    return {"ok": True, "config": await config.get_all()}


# ── Telegram QR login API ─────────────────────────────────────────────────────

@app.get("/api/telegram/status")
async def api_tg_status():
    from src import tg_auth
    return await tg_auth.status()


@app.post("/api/telegram/qr/start")
async def api_tg_qr_start():
    from src import tg_auth
    return await tg_auth.start()


@app.get("/api/telegram/qr/poll")
async def api_tg_qr_poll():
    from src import tg_auth
    return await tg_auth.poll()


@app.post("/api/telegram/qr/password")
async def api_tg_qr_password(payload: dict):
    from src import tg_auth
    return await tg_auth.submit_password(payload.get("password", ""))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    if USE_MOCK or not DATABASE_URL:
        logger.info("[STARTUP] USE_MOCK=true or no DATABASE_URL — skipping PG/bot")
        return
    try:
        import asyncpg
        from sync.client import BASClient
        from sync import scheduler_sync
        from src import config

        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        bas_client = BASClient()
        await scheduler_sync.init(pool, bas_client)
        await config.ensure_tables()          # agent_config + agent_events
        asyncio.create_task(scheduler_sync.run_now())
        logger.info("[STARTUP] BAS sync scheduler started")
    except Exception as e:
        logger.error(f"[STARTUP] Failed to start sync: {e}")

    # Connect Telegram in the SAME process so the dashboard can launch campaigns
    try:
        from src.index import connect_and_register
        client = await connect_and_register(start_scheduler=True)
        if client:
            logger.info("[STARTUP] Telegram bot + proactive scheduler running")
        else:
            logger.warning("[STARTUP] Telegram not authorized — dashboard works, sending disabled")
    except Exception as e:
        logger.error(f"[STARTUP] Telegram connect failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    if not USE_MOCK and DATABASE_URL:
        try:
            from sync import scheduler_sync
            scheduler_sync.stop()
        except Exception:
            pass
        try:
            from src import scheduler
            scheduler.stop()
        except Exception:
            pass
