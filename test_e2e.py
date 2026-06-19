"""
End-to-end test: initialises all services and runs a full outbound message flow.
Sends a real Telegram message to Saved Messages (self) to avoid disturbing clients.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    encoding="utf-8",
)
logger = logging.getLogger("e2e")

TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_PHONE    = os.getenv("TG_PHONE", "")
DB_URL      = os.getenv("DATABASE_URL", "")

RESULTS: dict[str, str] = {}


async def step(name: str, coro) -> object:
    logger.info(">>> %s", name)
    try:
        result = await coro
        RESULTS[name] = f"OK  {str(result)[:120]}"
        logger.info("<<< %s: OK", name)
        return result
    except Exception as e:
        RESULTS[name] = f"FAIL  {e}"
        logger.error("<<< %s: FAIL — %s", name, e)
        return None


async def main():
    # ── 1. Init SQLite DB ─────────────────────────────────────────────────────
    from src import context
    await step("1. init_db (SQLite)", context.init_db())

    # ── 2. Connect to PostgreSQL and register pool ────────────────────────────
    import asyncpg
    from sync import scheduler_sync

    pool = None
    try:
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
        # Register pool with scheduler_sync so bas.get_* can find it via _get_pool()
        scheduler_sync._pool = pool
        row = await pool.fetchval("SELECT count(*) FROM products WHERE deleted=false")
        RESULTS["2. PG connect + pool register"] = f"OK  {row} products"
        logger.info(">>> 2. PG connect: OK — %d products", row)
    except Exception as e:
        RESULTS["2. PG connect + pool register"] = f"FAIL  {e}"
        logger.error(">>> 2. PG connect: FAIL — %s", e)

    # ── 3. BAS: get_products from PG ─────────────────────────────────────────
    from src import bas
    products = await step("3. get_products('болт')", bas.get_products("болт"))
    if products:
        p = products[0]
        logger.info("    Result: %s — %.2f грн, stock=%.0f", p["name"], p["price"], p["stock"])

    # ── 4. BAS: get_client by phone ───────────────────────────────────────────
    test_phone = "+380976331066"
    client_data = await step(f"4. get_client({test_phone})", bas.get_client(test_phone))
    if client_data:
        logger.info("    Client: %s / %s", client_data["name"], client_data.get("company", ""))

    # ── 5. get_orders for client ──────────────────────────────────────────────
    orders = []
    if client_data:
        orders = await step("5. get_orders(client_id)", bas.get_orders(client_data["id"]))
        if orders:
            o = orders[0]
            logger.info("    Last: %s %s %.0f грн  items=%d",
                        o.get("number"), o.get("date"), o.get("total", 0), len(o.get("items", [])))

    # ── 6. get_order_status ───────────────────────────────────────────────────
    if orders and orders[0].get("id"):
        status = await step("6. get_order_status(order_id)", bas.get_order_status(orders[0]["id"]))
        logger.info("    Status: %s", status)

    # ── 7. check_supplier ─────────────────────────────────────────────────────
    supplier = await step("7. check_supplier('Болт М8x50', 1000)", bas.check_supplier("Болт М8x50", 1000))
    if supplier:
        logger.info("    Note: %s", supplier.get("note", ""))

    # ── 8. Start Telegram client ──────────────────────────────────────────────
    from telethon import TelegramClient
    logger.info(">>> 8. Starting Telegram client (using existing session)...")
    tg_client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
    tg_ok = False
    try:
        # start() uses session if valid, otherwise prompts
        await tg_client.start(phone=TG_PHONE)
        me = await tg_client.get_me()
        RESULTS["8. Telegram start"] = f"OK  logged in as +{me.phone}"
        logger.info("<<< 8. Telegram: OK — +%s (%s)", me.phone, me.first_name)
        tg_ok = True
    except Exception as e:
        RESULTS["8. Telegram start"] = f"FAIL  {e}"
        logger.error("<<< 8. Telegram start: FAIL — %s", e)

    # ── 9. Outbound: send status report to Saved Messages ─────────────────────
    if tg_ok:
        try:
            lines = [
                f"SVY AI Agent — E2E test {datetime.now().strftime('%d.%m %H:%M')}",
                "",
                f"PG: {RESULTS.get('2. PG connect + pool register', 'N/A')}",
                f"Products: {'OK, знайдено ' + str(len(products)) + ' шт' if products else 'FAIL'}",
                f"Client {test_phone}: {'OK — ' + (client_data or {}).get('name','') if client_data else 'not found'}",
                f"Orders: {'OK, ' + str(len(orders)) + ' шт' if orders else 'none'}",
                f"Supplier: {supplier.get('available','?') if supplier else 'FAIL'}",
            ]
            await tg_client.send_message("me", "\n".join(lines))
            RESULTS["9. outbound to Saved Messages"] = "OK"
            logger.info("<<< 9. Outbound to Saved Messages: OK")
        except Exception as e:
            RESULTS["9. outbound to Saved Messages"] = f"FAIL  {e}"
            logger.error("<<< 9. Outbound FAIL: %s", e)

    # ── 10. create_order (saves to PG, tries manager notify) ──────────────────
    if client_data:
        from src.tools import set_tg_client
        escalation_chat = os.getenv("ESCALATION_CHAT_ID", "")
        peer = int(escalation_chat) if escalation_chat else None
        if tg_ok:
            set_tg_client(tg_client, peer)

        order_result = await step(
            "10. create_order (dry-run to PG)",
            bas.create_order(
                client_id=client_data["id"],
                client_name=client_data["name"],
                client_phone=test_phone,
                company=client_data.get("company", ""),
                city="Київ",
                items=[{"name": "Болт М8x50 DIN 933", "qty": 100, "price": 4.20}],
                comment="Тест E2E — можна видалити",
            )
        )
        logger.info("    Order: %s", order_result)

    # ── 11. Scheduler: link self and run inactive check ───────────────────────
    if tg_ok and client_data:
        me = await tg_client.get_me()
        self_chat_id = str(me.id)

        # Link self → test client
        await context.link_client(
            chat_id=self_chat_id,
            phone=test_phone,
            client_ref_key=client_data["id"],
            name=client_data["name"],
        )
        RESULTS["11a. link_client in SQLite"] = f"OK  chat={self_chat_id} -> {client_data['name']}"
        logger.info(">>> 11a. Linked chat=%s to %s", self_chat_id, client_data["name"])

        # Run scheduler inactive check (threshold=0 → always fires)
        from src import scheduler
        scheduler.set_tg_client(tg_client)

        import unittest.mock as mock
        bas_targets = [{
            "client_ref_key": client_data["id"],
            "name": client_data["name"],
            "phone": test_phone,
        }]
        # Resolve to self so the proactive message lands in Saved Messages
        self_entity = await tg_client.get_me()
        with mock.patch.object(scheduler, "_get_bas_outbound_targets",
                               new=mock.AsyncMock(return_value=bas_targets)), \
             mock.patch.object(scheduler, "resolve_phone_entity",
                               new=mock.AsyncMock(return_value=self_entity)):
            try:
                await scheduler._check_inactive_clients(days_threshold=0)
                RESULTS["11b. scheduler proactive send"] = "OK  (message in Saved Messages)"
                logger.info("<<< 11b. Scheduler inactive check: OK")
            except Exception as e:
                RESULTS["11b. scheduler proactive send"] = f"FAIL  {e}"
                logger.error("<<< 11b. Scheduler inactive check: FAIL — %s", e)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if tg_ok:
        await tg_client.disconnect()
    if pool:
        await pool.close()

    # ── Print summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("E2E TEST SUMMARY")
    print("=" * 60)
    for k, v in RESULTS.items():
        icon = "OK" if v.startswith("OK") else "FAIL"
        print(f"  [{icon}]  {k}: {v[:100]}")
    print("=" * 60)

    failed = [k for k, v in RESULTS.items() if not v.startswith("OK")]
    if failed:
        print(f"\nFAILED ({len(failed)}): {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"\nAll {len(RESULTS)} checks passed!")


asyncio.run(main())
