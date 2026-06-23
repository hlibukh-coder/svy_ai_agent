"""
End-to-end conversation simulator — talks to the agent as if a real client,
WITHOUT Telegram. Mirrors src.index.handle_message logic:
  resolve client by phone -> load orders -> build system prompt -> run OpenAI w/ tools.

Safe in live mode: create_order writes only to local PG (AI- number), escalation
just logs (no tg client wired). Run:  python sim_client.py
"""
import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

import asyncpg
from openai import AsyncOpenAI

from src import bas, context, tools
from src.prompt import build_system_prompt
from sync import scheduler_sync

logging.basicConfig(level=logging.WARNING)  # silence info noise; we print our own

DATABASE_URL = os.getenv("DATABASE_URL", "")
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

C_RESET = "\033[0m"; C_USER = "\033[96m"; C_BOT = "\033[92m"; C_TOOL = "\033[93m"; C_HEAD = "\033[1;95m"


async def run_turn(messages, phone):
    """One agent response with a visible tool-call trace. Returns (reply, tool_log)."""
    tool_log = []
    for _ in range(6):
        resp = await client.chat.completions.create(
            model="gpt-4o", messages=messages, tools=tools.TOOLS_SCHEMA, tool_choice="auto",
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", tool_log
        messages.append(msg)
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            result = await tools.execute_tool(tc.function.name, args, phone)
            tool_log.append((tc.function.name, args, result))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    resp = await client.chat.completions.create(model="gpt-4o", messages=messages)
    return resp.choices[0].message.content or "", tool_log


async def scenario(title, phone, turns):
    print(f"\n{C_HEAD}{'='*78}\n  {title}   (phone={phone or 'НЕИЗВЕСТЕН / новый лид'})\n{'='*78}{C_RESET}")

    client_data = await bas.get_client(phone) if phone else None
    orders = await bas.get_orders(client_data["id"]) if client_data else []
    if client_data:
        print(f"{C_TOOL}  ↳ BAS: клиент найден — {client_data['name']}, заказов в истории: {len(orders)}{C_RESET}")
    else:
        print(f"{C_TOOL}  ↳ BAS: клиент НЕ найден (новый лид){C_RESET}")

    system_prompt = build_system_prompt(client_data, orders)
    messages = [{"role": "system", "content": system_prompt}]

    for user_text in turns:
        print(f"\n{C_USER}👤 КЛИЕНТ: {user_text}{C_RESET}")
        messages.append({"role": "user", "content": user_text})
        reply, tool_log = await run_turn(messages, phone or "")
        for name, args, result in tool_log:
            short = result if len(result) < 280 else result[:280] + "…"
            print(f"{C_TOOL}   🔧 {name}({json.dumps(args, ensure_ascii=False)})")
            print(f"{C_TOOL}      → {short}{C_RESET}")
        messages.append({"role": "assistant", "content": reply})
        print(f"{C_BOT}🤖 АГЕНТ: {reply}{C_RESET}")


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool  # make bas._get_pool() work
    await context.init_db()

    await scenario(
        "СЦЕНАРИЙ 1 — НОВЫЙ ЛИД: подбор товара + оформление заказа",
        None,
        [
            "Доброго дня! Потрібні Т-болти М8х70 нержавіючі. Є в наявності? Яка ціна?",
            "Добре, беру 10 штук. Що потрібно для замовлення?",
            "Олександр, ФОП, м. Київ, телефон 0671112233. Оформляйте.",
        ],
    )

    await scenario(
        "СЦЕНАРИЙ 2 — ПОСТОЯННЫЙ КЛИЕНТ: знает историю, повтор заказа",
        "380508750057",
        [
            "Доброго дня! Хочу повторити останнє замовлення.",
            "Так, все вірно, оформлюйте.",
        ],
    )

    await scenario(
        "СЦЕНАРИЙ 3 — ЭСКАЛАЦИЯ: запрос скидки и отсрочки",
        "380503486899",
        [
            "Дайте знижку 15% і відстрочку платежу на місяць, інакше підемо до конкурентів.",
        ],
    )

    await scenario(
        "СЦЕНАРИЙ 4 — НЕТ НА СКЛАДЕ / уточнение у поставщика",
        None,
        [
            "Потрібно 5000 штук Покрівельних саморізів 4,8х75 RAL 8017. Скільки є і коли можете відвантажити?",
        ],
    )

    await pool.close()
    print(f"\n{C_HEAD}{'='*78}\n  ГОТОВО. Тестовые AI-заказы (если создались) — в локальной таблице orders.\n{'='*78}{C_RESET}")


if __name__ == "__main__":
    asyncio.run(main())
