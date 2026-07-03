import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.index import send_to_client

logger = logging.getLogger(__name__)

app = FastAPI()

USE_MOCK = os.getenv("USE_MOCK", "true").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Inbound webhooks for push channels (WAHA / Viber).
from src.channels import webhooks as channel_webhooks
app.include_router(channel_webhooks.router)


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

    from src import context
    db_path = os.getenv("DB_PATH", "data/history.db")
    conv_id = context.as_conv_id(chat_id)

    # Full history (no limit for dashboard)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT role, content, ts FROM messages WHERE conv_id=? ORDER BY ts ASC",
            (conv_id,),
        ) as cur:
            rows = await cur.fetchall()

    messages = [{"role": r[0], "content": r[1], "ts": r[2]} for r in rows]

    linked = await get_linked_client(conv_id=conv_id)
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

    # Which channel + account this conversation belongs to, so the dashboard can
    # show "from which account this chat is" (multi-account clarity).
    channel, account_id, _peer = context.parse_conv_id(conv_id)
    from src import accounts as _accounts
    acct = await _accounts.get_account(account_id)
    account_label = acct["label"] if acct else None

    return {
        "chat_id": chat_id,
        "messages": messages,
        "client": client_info,
        "ai_paused": ai_paused,
        "channel": channel,
        "account_id": account_id,
        "account_label": account_label,
    }


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


@app.post("/api/chat/{chat_id}/command")
async def api_chat_operator_command(chat_id: str, req: ChatSendRequest):
    """Operator drives the agent like an employee: a free-text instruction (e.g.
    «выстави КП на позицию X, 5000 шт, по 1.25 грн») is executed with the full toolset
    in this chat's context. The КП PDF / order / file go to the client via tools; the
    agent's text reply is returned here to the operator."""
    from src import index
    result = await index.operator_command(chat_id, req.text)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "command failed"))
    return result


@app.post("/api/chat/{chat_id}/ai")
async def api_chat_toggle_ai(chat_id: str, payload: dict):
    """Enable/disable the AI for a single chat (human takeover toggle)."""
    from src import context
    enabled = bool(payload.get("enabled", True))
    await context.set_chat_ai_paused(chat_id, not enabled)
    return {"chat_id": chat_id, "ai_enabled": enabled}


@app.post("/api/chat/{chat_id}/ai-reply")
async def api_chat_ai_reply(chat_id: str):
    """On-demand: tell the AI to compose and send one reply now (used when auto-reply
    is off — the AI answers only when the operator asks it to)."""
    from src import index
    result = await index.ai_reply_now(chat_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "AI reply failed"))
    return result


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


# ── Accounts API (multi-channel / multi-account, dashboard-managed) ───────────

@app.get("/api/accounts")
async def api_accounts_list():
    from src import accounts
    return await accounts.list_accounts()


class AccountCreate(BaseModel):
    channel: str
    label: str = ""
    credentials: dict = {}


@app.post("/api/accounts")
async def api_accounts_create(req: AccountCreate):
    from src import accounts
    if req.channel not in accounts.VALID_CHANNELS:
        raise HTTPException(status_code=400, detail="bad channel")
    creds = dict(req.credentials or {})
    if req.channel in ("whatsapp", "viber", "elevenlabs") and not creds.get("webhook_secret"):
        import secrets as _secrets
        creds["webhook_secret"] = _secrets.token_urlsafe(16)
    new_id = await accounts.add_account(req.channel, req.label or req.channel.title(), creds)
    return {"id": new_id}


@app.patch("/api/accounts/{account_id}")
async def api_accounts_update(account_id: int, payload: dict):
    from src import accounts
    await accounts.update_account(
        account_id,
        label=payload.get("label"),
        credentials=payload.get("credentials"),
        enabled=payload.get("enabled"),
    )
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def api_accounts_delete(account_id: int):
    from src import accounts
    from src.channels import registry
    acct = await accounts.get_account(account_id)
    if acct:
        ad = registry.get(acct["channel"], account_id)
        if ad:
            try:
                await ad.stop()
            except Exception:
                pass
            registry.unregister(acct["channel"], account_id)
    ok = await accounts.delete_account(account_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Не можна видалити основний акаунт — вимкніть його")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/connect")
async def api_accounts_connect(account_id: int):
    """Start/validate an account. Email/Viber connect & set status here; Telegram/
    WhatsApp use the qr/* flow to finish pairing."""
    from src.channels import manager, registry
    from src import accounts
    acct = await accounts.get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="account not found")
    await manager.start_account(account_id)
    ad = registry.get(acct["channel"], account_id)
    health = await ad.healthcheck() if ad else {"status": "error"}
    return {"status": health.get("status", "unknown"), "health": health}


@app.get("/api/accounts/{account_id}/status")
async def api_accounts_status(account_id: int):
    from src.channels import registry
    from src import accounts
    acct = await accounts.get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="account not found")
    ad = registry.get(acct["channel"], account_id)
    return await ad.healthcheck() if ad else {"status": acct["status"]}


@app.post("/api/accounts/{account_id}/qr/start")
async def api_accounts_qr_start(account_id: int):
    from src.channels import registry, manager
    from src import accounts
    acct = await accounts.get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="account not found")
    if acct["channel"] == "telegram":
        from src import tg_auth
        return await tg_auth.start(account_id)
    ad = registry.get(acct["channel"], account_id)
    if ad is None:
        await manager.start_account(account_id)
        ad = registry.get(acct["channel"], account_id)
    if ad is None or not hasattr(ad, "begin_qr"):
        raise HTTPException(status_code=400, detail="QR not supported for this channel")
    return await ad.begin_qr()


@app.get("/api/accounts/{account_id}/qr/poll")
async def api_accounts_qr_poll(account_id: int):
    from src.channels import registry
    from src import accounts
    acct = await accounts.get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="account not found")
    if acct["channel"] == "telegram":
        from src import tg_auth
        return await tg_auth.poll(account_id)
    ad = registry.get(acct["channel"], account_id)
    if ad is None or not hasattr(ad, "qr_poll"):
        return {"status": "disconnected"}
    return await ad.qr_poll()


@app.post("/api/accounts/{account_id}/qr/password")
async def api_accounts_qr_password(account_id: int, payload: dict):
    from src import tg_auth
    return await tg_auth.submit_password(payload.get("password", ""), account_id)


@app.get("/api/accounts/{account_id}/webhook")
async def api_accounts_webhook(account_id: int):
    """The inbound webhook URL to paste into the provider (ElevenLabs post-call
    webhook, WAHA, Viber). Includes the per-account ?token secret."""
    from src import accounts as _accounts
    acct = await _accounts.get_account(account_id, include_secrets=True)
    if not acct:
        raise HTTPException(status_code=404, detail="account not found")
    paths = {"elevenlabs": "elevenlabs", "whatsapp": "waha", "viber": "viber"}
    ch = acct["channel"]
    if ch not in paths:
        return {"url": "", "note": "цей канал не використовує вебхук"}
    public = os.getenv("PUBLIC_URL", "").rstrip("/") or "https://<ваш-домен>"
    secret = (acct.get("credentials") or {}).get("webhook_secret", "")
    url = f"{public}/webhooks/{paths[ch]}/{account_id}" + (f"?token={secret}" if secret else "")
    return {"url": url, "channel": ch}


# ── Operator file send + per-channel analytics ───────────────────────────────

@app.post("/api/chat/{chat_id}/send-file")
async def api_chat_send_file(chat_id: str, payload: dict):
    """Operator sends a file (doc_id from docs/ or 'invoice') into a conversation."""
    from src.index import operator_send_file
    from src.tools import _resolve_doc
    from src import context
    conv_id = context.as_conv_id(chat_id)
    channel, account_id, peer = context.parse_conv_id(conv_id)
    conv = {"conv_id": conv_id, "channel": channel, "account_id": account_id, "peer": peer, "phone": ""}
    linked = await context.get_linked_client(conv_id=conv_id)
    if linked:
        conv["phone"] = linked.get("phone", "") or ""
    doc = await _resolve_doc(payload.get("doc_id", ""), conv)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не знайдено")
    result = await operator_send_file(conv_id, doc["src"], caption=payload.get("caption", ""),
                                      filename=doc["filename"], mimetype=doc["mimetype"])
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "send failed"))
    return result


@app.post("/api/chat/{chat_id}/upload")
async def api_chat_upload(chat_id: str, file: UploadFile = File(...), caption: str = Form("")):
    """Operator uploads an arbitrary file (PDF / photo / document) from the
    dashboard chat and it is sent into the conversation via its channel adapter."""
    from src.index import operator_send_file
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Порожній файл")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл завеликий (макс. 50 МБ)")
    import mimetypes
    mimetype = (file.content_type or "").strip()
    if not mimetype or mimetype == "application/octet-stream":
        mimetype = mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    result = await operator_send_file(chat_id, data, caption=caption,
                                      filename=file.filename or "file", mimetype=mimetype)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "send failed"))
    return result


@app.get("/api/stats/by-channel")
async def api_stats_by_channel():
    from src import stats
    return await stats.by_channel()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # SQLite history + accounts table always exist (even in USE_MOCK) so channels
    # can be managed from the dashboard regardless of PostgreSQL.
    try:
        os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
        from src import context
        await context.init_db()
    except Exception as e:
        logger.error(f"[STARTUP] DB init failed: {e}")

    if not USE_MOCK and DATABASE_URL:
        try:
            import asyncpg
            from sync.client import BASClient
            from sync import scheduler_sync
            from src import config

            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            bas_client = BASClient()
            # Ensure the FULL schema exists in whatever DB DATABASE_URL points at
            # (local Postgres OR a fresh managed cloud DB like Neon) — otherwise the
            # first sync fails with UndefinedTable. migration.sql is all IF NOT EXISTS.
            try:
                mig = Path(__file__).parent / "sync" / "migration.sql"
                if mig.exists():
                    async with pool.acquire() as _c:
                        await _c.execute(mig.read_text(encoding="utf-8"))
                    logger.info("[STARTUP] schema applied (migration.sql)")
            except Exception as e:
                logger.error(f"[STARTUP] schema apply failed: {e}")
            await scheduler_sync.init(pool, bas_client)
            await config.ensure_tables()          # agent_config + agent_events + orders attribution
            asyncio.create_task(scheduler_sync.run_now())
            logger.info("[STARTUP] BAS sync scheduler started")
        except Exception as e:
            logger.error(f"[STARTUP] Failed to start sync: {e}")
    else:
        logger.info("[STARTUP] USE_MOCK or no DATABASE_URL — skipping PG sync")

    # Start ALL channel adapters (telegram/whatsapp/email/viber) from the accounts table
    # in the BACKGROUND, so a slow/unreachable channel never blocks the dashboard.
    # The legacy Telegram session (id=1) is included so existing behavior is preserved.
    async def _start_adapters():
        try:
            from src.channels import manager
            await manager.start_all_adapters()
            logger.info("[STARTUP] channel adapters started")
        except Exception as e:
            logger.error(f"[STARTUP] adapters start failed: {e}")
    asyncio.create_task(_start_adapters())


@app.on_event("shutdown")
async def shutdown():
    try:
        from src.channels import manager
        await manager.stop_all_adapters()
    except Exception:
        pass
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
