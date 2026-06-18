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
    _scheduler.add_job(
        _check_inactive_clients,
        trigger="cron",
        hour=10,
        minute=15,
        id="inactive_check",
    )
    _scheduler.add_job(
        _notify_new_products,
        trigger="cron",
        hour=10,
        minute=30,
        id="new_products_check",
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

        text = (
            f"{name}, привіт! Зазвичай ви замовляєте {item_name} раз на {int(avg_interval)} днів. "
            f"Підготувати такий самий замовлення?"
        )

        try:
            await _tg_client.send_message(phone, text)
            logger.info(f"[SCHEDULER] Sent reorder reminder to {phone}")
        except Exception as e:
            logger.error(f"[SCHEDULER] Failed to send to {phone}: {e}")


async def _check_inactive_clients(days_threshold: int = 60):
    """Send a re-engagement message to clients who haven't ordered in `days_threshold` days."""
    logger.info("[SCHEDULER] Running inactive clients check")
    if not _tg_client:
        logger.warning("[SCHEDULER] No tg client set")
        return

    from src import bas
    inactive = await bas.get_inactive_clients(days_threshold)

    for entry in inactive:
        phone = entry.get("phone", "")
        name = entry.get("name", "")
        last_date = entry.get("last_order_date", "")
        if not phone or not name:
            continue

        text = (
            f"{name}, добрий день! Давно не спілкувались. "
            f"Хотів уточнити — все влаштовує, чи зараз закуповуєтесь в іншого постачальника?"
        )

        try:
            await _tg_client.send_message(phone, text)
            logger.info(f"[SCHEDULER] Sent inactive reminder to {phone} (last order: {last_date})")
        except Exception as e:
            logger.error(f"[SCHEDULER] Failed to send inactive reminder to {phone}: {e}")


async def _notify_new_products():
    """Notify clients about new products that match their purchase history."""
    logger.info("[SCHEDULER] Running new products notification")
    if not _tg_client:
        logger.warning("[SCHEDULER] No tg client set")
        return

    from src import bas
    new_products = await bas.get_new_products()
    if not new_products:
        logger.info("[SCHEDULER] No new products to notify about")
        return

    from src.prompt import MOCK_CLIENTS as clients

    for phone, client in clients.items():
        orders = client.get("orders", [])
        if not orders:
            continue

        # Collect keywords from client's order history
        ordered_keywords = set()
        for order in orders:
            for item in order.get("items", []):
                words = item.get("name", "").lower().split()
                ordered_keywords.update(words)

        # Find new products relevant to this client
        relevant = []
        for product in new_products:
            product_words = product["name"].lower().split()
            if any(w in ordered_keywords for w in product_words if len(w) > 3):
                relevant.append(product)

        if not relevant:
            continue

        name = client.get("name", "")
        product = relevant[0]
        product_name = product["name"]

        text = (
            f"{name}, добрий день! З'явився товар, який може вас зацікавити: {product_name}. "
            f"Показати ціну та наявність?"
        )

        try:
            await _tg_client.send_message(phone, text)
            logger.info(f"[SCHEDULER] Sent new product notification to {phone}: {product_name}")
        except Exception as e:
            logger.error(f"[SCHEDULER] Failed to send new product notification to {phone}: {e}")
