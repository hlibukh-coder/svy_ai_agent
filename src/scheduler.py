import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.prompt import MOCK_CLIENTS

logger = logging.getLogger(__name__)

_tg_client = None
_scheduler = None


def set_tg_client(client):
    global _tg_client
    _tg_client = client


def start(tg_client):
    global _scheduler
    set_tg_client(tg_client)
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _check_reorder_clients,
        trigger="cron",
        hour=10,
        minute=0,
        id="reorder_check",
    )
    _scheduler.start()
    logger.info("Scheduler started")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


async def _check_reorder_clients():
    logger.info("[SCHEDULER] Running reorder check")
    if not _tg_client:
        logger.warning("[SCHEDULER] No tg client set")
        return

    from src.prompt import MOCK_CLIENTS as clients
    today = datetime.now().date()

    for phone, client in clients.items():
        orders = client.get("orders", [])
        if len(orders) < 1:
            continue

        # Calculate average reorder interval
        if len(orders) >= 2:
            dates = []
            for o in orders:
                try:
                    dates.append(datetime.strptime(o["date"], "%d.%m.%Y").date())
                except Exception:
                    pass
            if len(dates) >= 2:
                dates.sort()
                intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
                avg_interval = sum(intervals) / len(intervals)
            else:
                avg_interval = 30
        else:
            avg_interval = 30

        last_order = orders[0]
        try:
            last_date = datetime.strptime(last_order["date"], "%d.%m.%Y").date()
        except Exception:
            continue

        remind_date = last_date + timedelta(days=int(avg_interval) - 5)
        if remind_date != today:
            continue

        name = client.get("name", "")
        last_items = last_order.get("items", [])
        if not last_items:
            continue

        item = last_items[0]
        item_name = item.get("name", "")
        item_qty = item.get("qty", "")

        text = (
            f"{name}, привіт! Зазвичай ви замовляєте {item_name} раз на {int(avg_interval)} днів. "
            f"Підготувати такий самий замовлення?"
        )

        try:
            await _tg_client.send_message(phone, text)
            logger.info(f"[SCHEDULER] Sent reorder reminder to {phone}")
        except Exception as e:
            logger.error(f"[SCHEDULER] Failed to send to {phone}: {e}")
