"""
Live outbound proof: REAL AI-generated proactive messages (no templates), targeting
a BAS client straight from the database. Runs the actual scheduler functions with
real gpt-4o. Telegram resolve+send are mocked so nothing is actually sent.
"""
import asyncio
import datetime as dt
import logging
import os
import sys
import unittest.mock as mock

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.ERROR)

DB_URL = os.getenv("DATABASE_URL", "")


async def main():
    import asyncpg
    from sync import scheduler_sync
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool

    from src import scheduler, bas

    phone = "+380504442888"
    client = await bas.get_client(phone)
    orders = await bas.get_orders(client["id"])
    print("=" * 72)
    print(f"КЛІЄНТ З BAS: {client['name']}  |  заказов: {len(orders)}")
    print(f"Последний заказ: №{orders[0]['number']} от {orders[0]['date']}")
    print("Телефон берётся ИЗ BAS, бот пишет первым (резолв через контакт-импорт).")
    print("=" * 72)

    # Capture what _deliver would send (mock resolve → fake entity, mock send)
    sent: list = []
    fake_entity = type("E", (), {"id": 555111})()
    mock_tg = mock.AsyncMock()

    async def _send(entity, text):
        sent.append(text)
    mock_tg.send_message = _send
    scheduler.set_tg_client(mock_tg)

    # BAS target straight from DB (phone + history)
    targets = [{"client_ref_key": client["id"], "name": client["name"], "phone": phone}]

    base = [
        mock.patch.object(scheduler, "_get_bas_outbound_targets",
                          new=mock.AsyncMock(return_value=targets)),
        mock.patch.object(scheduler, "resolve_phone_entity",
                          new=mock.AsyncMock(return_value=fake_entity)),
        mock.patch.object(scheduler, "_save_proactive_message", new=mock.AsyncMock()),
    ]
    for p in base:
        p.start()

    # Normalize real order dates → date objects
    norm = []
    for o in orders:
        d = o.get("date")
        if isinstance(d, str):
            try:
                d = dt.date.fromisoformat(d[:10])
            except Exception:
                continue
        if d:
            norm.append({"date": d})

    today = dt.date.today()

    # ── 1. REORDER (craft dates so the cycle is due today) ──────────────────────
    print("\n[1] ПОВТОРНАЯ ЗАКУПКА (reorder)")
    print("-" * 72)
    sent.clear()
    due_orders = [
        {"date": today - dt.timedelta(days=15)},
        {"date": today - dt.timedelta(days=35)},
        {"date": today - dt.timedelta(days=55)},
    ]
    with mock.patch.object(scheduler, "_get_orders_for_client",
                           new=mock.AsyncMock(return_value=due_orders)):
        await scheduler._check_reorder_clients()
    for m in sent:
        print(f"  BOT → {client['name']}:\n  «{m}»")

    # ── 2. WIN-BACK (real history, threshold 0 forces it) ───────────────────────
    print("\n[2] ВОЗВРАТ КЛИЕНТА (win-back)")
    print("-" * 72)
    sent.clear()
    with mock.patch.object(scheduler, "_get_orders_for_client",
                           new=mock.AsyncMock(return_value=norm)):
        await scheduler._check_inactive_clients(days_threshold=0)
    for m in sent:
        print(f"  BOT → {client['name']}:\n  «{m}»")

    # ── 3. NEW PRODUCT ──────────────────────────────────────────────────────────
    print("\n[3] НОВЫЙ ТОВАР")
    print("-" * 72)
    sent.clear()
    with mock.patch.object(scheduler, "_get_orders_for_client",
                           new=mock.AsyncMock(return_value=norm)), \
         mock.patch.object(bas, "get_new_products",
                           new=mock.AsyncMock(return_value=[
                               {"name": "DIN985 Гайка М10 самостоп нерж А2", "price": 4.5}])):
        await scheduler._notify_new_products()
    for m in sent:
        print(f"  BOT → {client['name']}:\n  «{m}»")

    for p in base:
        p.stop()
    await pool.close()

    print("\n" + "=" * 72)
    print("Цель берётся из BAS по телефону. Текст генерит gpt-4o персонально. Шаблонов нет.")
    print("=" * 72)


asyncio.run(main())
