"""
Comprehensive conversation test — real OpenAI (gpt-4o) + real PostgreSQL data.
Covers every use case from the product spec. No Telegram needed.

Spec coverage:
  1. Новый клиент            — подбор товара, оформление
  2. Постоянный клиент       — история из BAS, повтор заказа
  3. Создание заказа         — товар в наличии → create_order вызван
  4. Статус заказа           — get_order_status из PG
  5. Нет на складе           — check_supplier → эскалация
  6. Доставка/оплата         — нет данных → не выдумывает, эскалация
  7. Явная эскалация         — "хочу человека" → notify_manager
  8. Возврат / цена          — возражение по цене
  9. OUTBOUND повторная      — scheduler reorder
 10. OUTBOUND потерянный     — scheduler inactive
 11. OUTBOUND новый товар    — scheduler new product
"""
import asyncio
import json
import logging
import os
import sys
import unittest.mock as mock

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.ERROR)  # quiet — only show conversation

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DATABASE_URL", "")

# Tracks which tools were called across the whole run (for coverage matrix)
TOOLS_CALLED: set[str] = set()
COVERAGE: dict[str, str] = {}


async def init_pg():
    import asyncpg
    from sync import scheduler_sync
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool
    return pool


async def chat(openai_client, system_prompt: str, history: list, user_msg: str,
               sender_phone: str = "") -> str:
    from src import tools

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})

    for _ in range(6):
        resp = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools.TOOLS_SCHEMA,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(msg)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            TOOLS_CALLED.add(fn_name)
            fn_args = json.loads(tc.function.arguments)
            arg_preview = ', '.join(f'{k}={repr(v)[:34]}' for k, v in fn_args.items())
            print(f"  [tool: {fn_name}({arg_preview})]")
            result = await tools.execute_tool(fn_name, fn_args, sender_phone)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    resp = await openai_client.chat.completions.create(model="gpt-4o", messages=messages)
    return resp.choices[0].message.content or ""


async def run_scenario(title, openai_client, system_prompt, turns, sender_phone=""):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)
    history = []
    last_reply = ""
    for role, text in turns:
        if role == "user":
            print(f"\n  CLIENT > {text}")
            reply = await chat(openai_client, system_prompt, history, text, sender_phone)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            print(f"  AGENT  > {reply}\n")
            last_reply = reply
        else:
            history.append({"role": role, "content": text})
    return last_reply, history


async def main():
    from openai import AsyncOpenAI
    from src.prompt import build_system_prompt
    from src import bas, tools, scheduler

    print("\nInitialising PG pool...")
    pool = await init_pg()
    tools.set_tg_client(None, None)  # escalation logs only
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # Find a real in-stock product for the order-creation scenario
    in_stock = await bas.get_products("Т-болт М8")
    stock_item = next((p for p in in_stock if p["stock"] > 0), None)
    print(f"  [in-stock test item: {stock_item['name'] if stock_item else 'NONE'} "
          f"(stock={stock_item['stock'] if stock_item else 0})]")

    # ════════════════════════════════════════════════════════════════════════
    # 1. NEW CLIENT
    # ════════════════════════════════════════════════════════════════════════
    prompt_new = build_system_prompt(None, None)
    await run_scenario(
        "КЕЙС 1 — НОВИЙ КЛІЄНТ (підбір товару)",
        openai_client, prompt_new,
        [
            ("user", "Доброго дня! Є гвинти М6 з нержавійки?"),
            ("user", "Потрібно 200 штук. Скільки коштує?"),
        ],
    )
    COVERAGE["1. Новий клієнт — підбір товару"] = "get_products" in TOOLS_CALLED

    # ════════════════════════════════════════════════════════════════════════
    # 2. RETURNING CLIENT
    # ════════════════════════════════════════════════════════════════════════
    returning_phone = "+380504442888"
    client_data = await bas.get_client(returning_phone)
    orders = await bas.get_orders(client_data["id"]) if client_data else []
    prompt_ret = build_system_prompt(client_data, orders)
    print(f"\n  [returning client: {client_data['name'] if client_data else 'NONE'}, "
          f"{len(orders)} orders]")
    reply2, _ = await run_scenario(
        f"КЕЙС 2 — ПОСТІЙНИЙ КЛІЄНТ ({client_data['name'] if client_data else '?'})",
        openai_client, prompt_ret,
        [
            ("user", "Добрий день! Хочу повторити замовлення"),
        ],
    )
    # Should greet by name and not invent products
    COVERAGE["2. Постійний клієнт — впізнав по імені"] = (
        bool(client_data) and client_data["name"].split()[0] in reply2
    )

    # ════════════════════════════════════════════════════════════════════════
    # 3. SUCCESSFUL ORDER CREATION (in-stock item)
    # ════════════════════════════════════════════════════════════════════════
    TOOLS_CALLED.discard("create_order")
    if stock_item:
        qty = max(1, int(stock_item["stock"]) // 2)
        await run_scenario(
            "КЕЙС 3 — СТВОРЕННЯ ЗАМОВЛЕННЯ (товар у наявності)",
            openai_client, prompt_new,
            [
                ("user", f"Потрібен {stock_item['name']}, {qty} шт"),
                ("user", "Мене звати Олег, фізособа"),
                ("user", "Місто Київ"),
                ("user", "Мій телефон 0991234567, оформлюйте"),
            ],
            sender_phone="+380991234567",
        )
    COVERAGE["3. Створення заказу — create_order викликаний"] = "create_order" in TOOLS_CALLED

    # ════════════════════════════════════════════════════════════════════════
    # 4. ORDER STATUS
    # ════════════════════════════════════════════════════════════════════════
    TOOLS_CALLED.discard("get_order_status")
    order_number = orders[0]["number"] if orders else "—"
    reply4, _ = await run_scenario(
        f"КЕЙС 4 — СТАТУС ЗАМОВЛЕННЯ ({order_number})",
        openai_client, prompt_ret,
        [("user", f"Який статус мого замовлення {order_number}?")],
    )
    COVERAGE["4. Статус заказу — get_order_status з PG"] = "get_order_status" in TOOLS_CALLED

    # ════════════════════════════════════════════════════════════════════════
    # 5. NOT ENOUGH STOCK → SUPPLIER → ESCALATION
    # ════════════════════════════════════════════════════════════════════════
    TOOLS_CALLED.discard("check_supplier")
    reply5, _ = await run_scenario(
        "КЕЙС 5 — НЕМА НА СКЛАДІ → поставщик/менеджер",
        openai_client, prompt_new,
        [("user", "Потрібно 500000 болтів М20х100, є стільки?")],
    )
    low5 = reply5.lower()
    # Spec-compliant either way: call check_supplier OR offer to check with supplier/manager
    COVERAGE["5. Нема на складі — поставщик/менеджер"] = (
        "check_supplier" in TOOLS_CALLED
        or "постачальник" in low5 or "поставщик" in low5 or "менеджер" in low5
    )

    # ════════════════════════════════════════════════════════════════════════
    # 6. DELIVERY / PAYMENT QUESTION (no data → must not invent)
    # ════════════════════════════════════════════════════════════════════════
    reply6, _ = await run_scenario(
        "КЕЙС 6 — ПИТАННЯ ПРО ДОСТАВКУ/ОПЛАТУ (нема даних)",
        openai_client, prompt_ret,
        [("user", "Яка у вас відстрочка платежу і скільки днів доставка по Києву?")],
    )
    # Agent must defer to manager, not invent specific terms
    low = reply6.lower()
    COVERAGE["6. Доставка/оплата — не вигадує, скеровує до менеджера"] = (
        "менеджер" in low or "уточн" in low or "підтверд" in low
    )

    # ════════════════════════════════════════════════════════════════════════
    # 7. EXPLICIT HUMAN ESCALATION
    # ════════════════════════════════════════════════════════════════════════
    TOOLS_CALLED.discard("notify_manager")
    await run_scenario(
        "КЕЙС 7 — ЯВНА ЕСКАЛАЦІЯ ДО ЛЮДИНИ",
        openai_client, prompt_ret,
        [("user", "Хочу поспілкуватися з живим менеджером, а не з ботом")],
    )
    COVERAGE["7. Явна ескалація — notify_manager"] = "notify_manager" in TOOLS_CALLED

    # ════════════════════════════════════════════════════════════════════════
    # 8. PRICE OBJECTION / WIN-BACK
    # ════════════════════════════════════════════════════════════════════════
    reply8, _ = await run_scenario(
        "КЕЙС 8 — ВОЗРАЖЕННЯ ПО ЦІНІ / ПОВЕРНЕННЯ",
        openai_client, prompt_ret,
        [("user", "У вас дорого, я тепер беру в іншого постачальника")],
    )
    low8 = reply8.lower()
    COVERAGE["8. Возраження по ціні — пропонує рішення/менеджера"] = (
        "менеджер" in low8 or "цін" in low8 or "альтернатив" in low8 or "умов" in low8
    )

    # ════════════════════════════════════════════════════════════════════════
    # OUTBOUND 9–11 — exercise the REAL scheduler functions with a mock TG client
    # ════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 72)
    print("  OUTBOUND (реальні функції планувальника, mock Telegram)")
    print("=" * 72)

    sent_messages: list = []
    fake_entity = type("E", (), {"id": 999000})()
    mock_tg = mock.AsyncMock()
    async def _capture(entity, text):
        sent_messages.append(text)
        print(f"\n  BOT → {entity.id}:\n  \"{text}\"")
    mock_tg.send_message = _capture
    scheduler.set_tg_client(mock_tg)

    bas_targets = [{
        "client_ref_key": client_data["id"] if client_data else "x",
        "name": client_data["name"] if client_data else "Клієнт",
        "phone": returning_phone,
    }]
    import datetime as _dt
    today = _dt.date.today()
    due_orders = [
        {"date": today - _dt.timedelta(days=15)},
        {"date": today - _dt.timedelta(days=35)},
        {"date": today - _dt.timedelta(days=55)},
    ]

    with mock.patch.object(scheduler, "_get_bas_outbound_targets",
                           new=mock.AsyncMock(return_value=bas_targets)), \
         mock.patch.object(scheduler, "resolve_phone_entity",
                           new=mock.AsyncMock(return_value=fake_entity)), \
         mock.patch.object(scheduler, "_get_orders_for_client",
                           new=mock.AsyncMock(return_value=due_orders)), \
         mock.patch.object(scheduler, "_save_proactive_message", new=mock.AsyncMock()):

        # 9. Reorder — due_orders make the cycle due today
        sent_messages.clear()
        await scheduler._check_reorder_clients()
        COVERAGE["9. OUTBOUND повторна закупівля (reorder)"] = len(sent_messages) > 0

        # 10. Inactive win-back — threshold 0 forces a message
        sent_messages.clear()
        await scheduler._check_inactive_clients(days_threshold=0)
        COVERAGE["10. OUTBOUND повернення клієнта (inactive)"] = len(sent_messages) > 0

        # 11. New product notification
        sent_messages.clear()
        with mock.patch.object(
            bas, "get_new_products",
            new=mock.AsyncMock(return_value=[{"name": "Поліамідна шайба М10 4мм", "price": 2.3}]),
        ):
            await scheduler._notify_new_products()
        COVERAGE["11. OUTBOUND новий товар (new product)"] = len(sent_messages) > 0

    await pool.close()

    # ════════════════════════════════════════════════════════════════════════
    # COVERAGE MATRIX
    # ════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 72)
    print("  МАТРИЦЯ ПОКРИТТЯ (спека → результат)")
    print("=" * 72)
    all_ok = True
    for k, v in COVERAGE.items():
        icon = "PASS" if v else "FAIL"
        if not v:
            all_ok = False
        print(f"  [{icon}]  {k}")
    print(f"\n  Tools exercised: {', '.join(sorted(TOOLS_CALLED))}")
    print("=" * 72)
    print(f"  {'ALL SCENARIOS PASS' if all_ok else 'SOME SCENARIOS FAILED'}")
    print("=" * 72)


asyncio.run(main())
