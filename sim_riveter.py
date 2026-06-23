"""
Проверка стиля общения агента на сценарии из примера менеджера
(оренда закльопочника, заклёпки М5/М6/М8/М10). Без Telegram — напрямую
через OpenAI + tools + живой BAS/PG, как в sim_client.py.

Запуск:  python sim_riveter.py
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

logging.basicConfig(level=logging.WARNING)

DATABASE_URL = os.getenv("DATABASE_URL", "")
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

C_RESET = "\033[0m"; C_USER = "\033[96m"; C_BOT = "\033[92m"; C_TOOL = "\033[93m"; C_HEAD = "\033[1;95m"


async def run_turn(messages, phone):
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
    print(f"\n{C_HEAD}{'='*78}\n  {title}   (phone={phone or 'НЕВІДОМИЙ / новий лід'})\n{'='*78}{C_RESET}")
    client_data = await bas.get_client(phone) if phone else None
    orders = await bas.get_orders(client_data["id"]) if client_data else []
    if client_data:
        print(f"{C_TOOL}  ↳ BAS: клієнт знайдений — {client_data['name']}, замовлень: {len(orders)}{C_RESET}")
    else:
        print(f"{C_TOOL}  ↳ BAS: клієнт не знайдений (новий лід){C_RESET}")

    system_prompt = build_system_prompt(client_data, orders)
    messages = [{"role": "system", "content": system_prompt}]

    for user_text in turns:
        print(f"\n{C_USER}👤 КЛІЄНТ: {user_text}{C_RESET}")
        messages.append({"role": "user", "content": user_text})
        reply, tool_log = await run_turn(messages, phone or "")
        for name, args, result in tool_log:
            short = result if len(result) < 320 else result[:320] + "…"
            print(f"{C_TOOL}   🔧 {name}({json.dumps(args, ensure_ascii=False)})")
            print(f"{C_TOOL}      → {short}{C_RESET}")
        messages.append({"role": "assistant", "content": reply})
        print(f"{C_BOT}🤖 АГЕНТ: {reply}{C_RESET}")


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool
    await context.init_db()

    await scenario(
        "СЦЕНАРІЙ — ОРЕНДА ЗАКЛЬОПОЧНИКА (як у прикладі менеджера)",
        None,
        [
            "Вітаю. Розмовляли з вами стосовно оренди закльопочника",
            "М5, М6, М8, М10",
            "На вулиці, метал 3 мм. Заклепки потрібні ваші. На 5 днів.",
        ],
    )

    await pool.close()
    print(f"\n{C_HEAD}{'='*78}\n  ГОТОВО.\n{'='*78}{C_RESET}")


if __name__ == "__main__":
    asyncio.run(main())
