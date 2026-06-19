"""
Proactive (outbound) messaging scheduler.

Targets BAS clients by phone (Telethon userbot resolves via contact import). Messages
are AI-generated per client. All intervals/thresholds/enable flags are read LIVE from
agent_config (PostgreSQL), so the dashboard can change them without a restart.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src import outbound, config
from src.telegram_utils import resolve_phone_entity, extract_ua_phone

logger = logging.getLogger(__name__)

_tg_client = None
_scheduler = None
_cancel = False          # set True to stop a running broadcast mid-way
_running_kind = None     # which campaign is currently sending (or None)


def set_tg_client(client):
    global _tg_client
    _tg_client = client


def cancel_campaign():
    """Request the currently-running broadcast to stop after the current send."""
    global _cancel
    _cancel = True


def running_campaign():
    return _running_kind


def start(tg_client, send_hour: int = 10):
    global _scheduler
    set_tg_client(tg_client)
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_check_reorder_clients, "cron", hour=send_hour, minute=0, id="reorder_check")
    _scheduler.add_job(_check_inactive_clients, "cron", hour=send_hour, minute=15, id="inactive_check")
    _scheduler.add_job(_notify_new_products, "cron", hour=send_hour, minute=30, id="new_products_check")
    _scheduler.start()
    logger.info(f"Scheduler started (send_hour={send_hour})")


def reschedule(send_hour: int):
    """Re-point the daily jobs to a new hour (called after the UI changes config)."""
    if not _scheduler:
        return
    for jid, minute in (("reorder_check", 0), ("inactive_check", 15), ("new_products_check", 30)):
        try:
            _scheduler.reschedule_job(jid, trigger="cron", hour=send_hour, minute=minute)
        except Exception as e:
            logger.error(f"[SCHEDULER] reschedule {jid} failed: {e}")
    logger.info(f"[SCHEDULER] rescheduled to hour={send_hour}")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


# ── Data access ──────────────────────────────────────────────────────────────

async def _get_bas_outbound_targets() -> list[dict]:
    """BAS clients we can contact: usable phone + at least one order."""
    import os
    if os.getenv("USE_MOCK", "true").lower() == "true":
        return []
    try:
        from sync import scheduler_sync
        pool = scheduler_sync.get_pool()
        if not pool:
            logger.warning("[SCHEDULER] no PG pool for outbound targets")
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.ref_key, c.name, c.phone
                FROM clients c
                WHERE c.deleted = false
                  AND c.phone IS NOT NULL AND c.phone != ''
                  AND EXISTS (SELECT 1 FROM orders o WHERE o.client_ref_key = c.ref_key)
                """
            )
        targets = []
        for r in rows:
            phone = extract_ua_phone(r["phone"])
            if phone:
                targets.append({"client_ref_key": r["ref_key"], "name": r["name"] or "Клієнт", "phone": phone})
        return targets
    except Exception as e:
        logger.error(f"[SCHEDULER] _get_bas_outbound_targets error: {e}")
        return []


async def _get_orders_for_client(client_ref_key: str) -> list[dict]:
    import os
    if os.getenv("USE_MOCK", "true").lower() == "true":
        return []
    try:
        from sync import scheduler_sync
        pool = scheduler_sync.get_pool()
        if not pool:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ref_key, number, date, amount FROM orders "
                "WHERE client_ref_key = $1 ORDER BY date DESC LIMIT 10",
                client_ref_key,
            )
        return [{"ref_key": r["ref_key"], "number": r["number"], "date": r["date"],
                 "amount": float(r["amount"] or 0)} for r in rows]
    except Exception as e:
        logger.error(f"[SCHEDULER] _get_orders_for_client error: {e}")
        return []


async def _save_proactive_message(chat_id: str, text: str):
    from src import context
    try:
        await context.save_message(chat_id, "assistant", text)
    except Exception as e:
        logger.error(f"[SCHEDULER] save_message error: {e}")


# ── Delivery ─────────────────────────────────────────────────────────────────

async def _deliver(phone: str, name: str, text: str) -> bool:
    """Resolve phone → entity → send → record history + event. True if sent."""
    if not _tg_client:
        logger.warning("[SCHEDULER] no tg client set")
        return False
    entity = await resolve_phone_entity(_tg_client, phone, name)
    if not entity:
        logger.info(f"[SCHEDULER] cannot reach {name} ({phone}) on Telegram — skipped")
        return False
    try:
        await _tg_client.send_message(entity, text)
        await _save_proactive_message(str(getattr(entity, "id", phone)), text)
        await config.log_event("outbound", f"Відправлено повідомлення: {name}", {"phone": phone})
        logger.info(f"[SCHEDULER] sent to {name} ({phone})")
        return True
    except Exception as e:
        logger.error(f"[SCHEDULER] send failed to {name} ({phone}): {e}")
        return False


# ── Proactive jobs (each reads live config) ──────────────────────────────────

async def _check_reorder_clients() -> int:
    logger.info("[SCHEDULER] Running reorder check")
    cfg = await config.get_all()
    if not cfg.get("agent_enabled", True) or not cfg.get("reorder_enabled", True):
        logger.info("[SCHEDULER] reorder skipped (agent paused or disabled)")
        return 0
    window = int(cfg.get("reorder_window_days", 1))
    throttle = float(cfg.get("throttle_sec", 2))
    cap = int(cfg.get("max_per_run", 50))

    targets = await _get_bas_outbound_targets()
    today = datetime.now().date()
    sent = 0
    for entry in targets:
        if sent >= cap or _cancel:
            break
        name, phone = entry["name"], entry["phone"]
        orders = await _get_orders_for_client(entry["client_ref_key"])
        dates = [o["date"] for o in orders if o.get("date")]
        if len(dates) < 2:
            continue
        sorted_dates = sorted(dates)
        intervals = [(sorted_dates[i + 1] - sorted_dates[i]).days for i in range(len(sorted_dates) - 1)]
        avg_interval = max(7, int(sum(intervals) / len(intervals)))
        last_date = max(dates)
        remind_date = last_date + timedelta(days=avg_interval - 5)
        if abs((remind_date - today).days) > window:
            continue
        days_since = (today - last_date).days
        ctx = (
            f"Постоянный клиент, всего заказов: {len(orders)}. Последний {days_since} дней назад. "
            f"Обычно заказывает раз в ~{avg_interval} дней — подошёл срок повторной закупки. "
            f"Состав прошлых заказов неизвестен — НЕ называй конкретные товары. "
            f"Цель: мягко предложить подготовить новый заказ."
        )
        text = await outbound.compose_message("повторная закупка (срок подошёл)", name, ctx)
        if text and await _deliver(phone, name, text):
            sent += 1
            await asyncio.sleep(throttle)
    logger.info(f"[SCHEDULER] reorder check done, sent={sent}")
    return sent


async def _check_inactive_clients(days_threshold: int | None = None) -> int:
    logger.info("[SCHEDULER] Running inactive clients check")
    cfg = await config.get_all()
    if not cfg.get("agent_enabled", True) or not cfg.get("inactive_enabled", True):
        logger.info("[SCHEDULER] inactive skipped (agent paused or disabled)")
        return 0
    threshold = days_threshold if days_threshold is not None else int(cfg.get("inactive_days", 60))
    throttle = float(cfg.get("throttle_sec", 2))
    cap = int(cfg.get("max_per_run", 50))

    targets = await _get_bas_outbound_targets()
    today = datetime.now().date()
    cutoff = today - timedelta(days=threshold)
    sent = 0
    for entry in targets:
        if sent >= cap or _cancel:
            break
        name, phone = entry["name"], entry["phone"]
        orders = await _get_orders_for_client(entry["client_ref_key"])
        dates = [o["date"] for o in orders if o.get("date")]
        if not dates:
            continue
        last_date = max(dates)
        if last_date >= cutoff:
            continue
        days_silent = (today - last_date).days
        ctx = (
            f"Раньше клиент покупал, но давно молчит — {days_silent} дней без заказов "
            f"(всего заказов: {len(orders)}). Цель: аккуратно выяснить причину — всё ли устраивает, "
            f"не ушёл ли к другому поставщику. Если намекнёт на цену — дай понять что готовы обсудить условия. "
            f"Не дави, конкретные товары не называй."
        )
        text = await outbound.compose_message("возврат клиента / давно не общались", name, ctx)
        if text and await _deliver(phone, name, text):
            sent += 1
            await asyncio.sleep(throttle)
    logger.info(f"[SCHEDULER] inactive check done, sent={sent}")
    return sent


async def _notify_new_products() -> int:
    logger.info("[SCHEDULER] Running new products notification")
    cfg = await config.get_all()
    if not cfg.get("agent_enabled", True) or not cfg.get("newproduct_enabled", True):
        logger.info("[SCHEDULER] new-product skipped (agent paused or disabled)")
        return 0
    throttle = float(cfg.get("throttle_sec", 2))
    cap = int(cfg.get("max_per_run", 50))

    from src import bas
    new_products = await bas.get_new_products()
    if not new_products:
        logger.info("[SCHEDULER] No new products to notify about")
        return 0
    product = new_products[0]
    product_name = product.get("name", "")
    if not product_name:
        return 0
    price = product.get("price", 0)
    price_str = f", цена {price} грн/шт" if price else ""

    targets = await _get_bas_outbound_targets()
    sent = 0
    for entry in targets:
        if sent >= cap or _cancel:
            break
        name, phone = entry["name"], entry["phone"]
        orders = await _get_orders_for_client(entry["client_ref_key"])
        if not orders:
            continue
        ctx = (
            f"Постоянный клиент (всего заказов: {len(orders)}). Появился новый товар: "
            f"«{product_name}»{price_str}. Цель: персонально сообщить о новинке и предложить "
            f"показать цену/наличие. Используй ТОЛЬКО это название товара, других не придумывай."
        )
        text = await outbound.compose_message("новый товар в наличии", name, ctx)
        if text and await _deliver(phone, name, text):
            sent += 1
            await asyncio.sleep(throttle)
    logger.info(f"[SCHEDULER] new-product check done, sent={sent}")
    return sent


# ── Manual send to specific clients ──────────────────────────────────────────

async def run_manual(client_ref_keys: list[str], message: str = "", kind: str = "") -> int:
    """Send to a hand-picked list of clients (by ref_key). Either exact text or AI-composed."""
    global _cancel, _running_kind
    _cancel = False
    _running_kind = "manual"
    import os
    if os.getenv("USE_MOCK", "true").lower() == "true":
        _running_kind = None
        return 0
    try:
        from sync import scheduler_sync
        pool = scheduler_sync.get_pool()
        if not pool:
            logger.warning("[SCHEDULER] no PG pool for manual send")
            return 0
        cfg = await config.get_all()
        throttle = float(cfg.get("throttle_sec", 2))
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ref_key::text AS ref_key, name, phone FROM clients "
                "WHERE ref_key::text = ANY($1::text[])",
                client_ref_keys,
            )
        sent = 0
        for r in rows:
            if _cancel:
                break
            phone = extract_ua_phone(r["phone"] or "")
            if not phone:
                continue
            name = r["name"] or "Клієнт"
            if message:
                text = message
            else:
                orders = await _get_orders_for_client(r["ref_key"])
                ctx = "Клієнт компанії СВЮ.КЛУБ."
                if orders:
                    ctx += f" Всього замовлень: {len(orders)}, останнє {orders[0].get('date', '')}."
                text = await outbound.compose_message(kind or "персональне повідомлення", name, ctx)
            if text and await _deliver(phone, name, text):
                sent += 1
                await asyncio.sleep(throttle)
        logger.info(f"[SCHEDULER] manual send done, sent={sent}")
        return sent
    except Exception as e:
        logger.error(f"[SCHEDULER] run_manual error: {e}")
        return 0
    finally:
        _running_kind = None
        _cancel = False


# ── Manual campaign launch (from dashboard) ──────────────────────────────────

async def run_campaign(kind: str) -> int:
    """Run a campaign on demand. Returns number of messages sent."""
    global _cancel, _running_kind
    _cancel = False
    _running_kind = kind
    try:
        if kind == "reorder":
            return await _check_reorder_clients()
        if kind == "inactive":
            return await _check_inactive_clients()
        if kind == "newproduct":
            return await _notify_new_products()
        logger.warning(f"[SCHEDULER] unknown campaign kind: {kind}")
        return 0
    finally:
        _running_kind = None
        _cancel = False
