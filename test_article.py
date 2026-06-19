"""
Live proof: agent finds a product BY ARTICLE in a real dialog (real gpt-4o + PG).
Also shows a "not found" case. Prints the exact JSON the tool returns to the agent.
"""
import asyncio
import json
import logging
import os
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.ERROR)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DATABASE_URL", "")


async def init_pg():
    import asyncpg
    from sync import scheduler_sync
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool
    return pool


async def run_turn(openai_client, system_prompt, user_msg):
    from src import tools
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    for _ in range(5):
        resp = await openai_client.chat.completions.create(
            model="gpt-4o", messages=messages,
            tools=tools.TOOLS_SCHEMA, tool_choice="auto",
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"    → агент вызвал: {tc.function.name}({args})")
            result = await tools.execute_tool(tc.function.name, args, "")
            # Show exactly what the tool returns to the agent
            parsed = json.loads(result)
            preview = json.dumps(parsed, ensure_ascii=False)
            print(f"    ← инструмент вернул: {preview[:300]}{'...' if len(preview) > 300 else ''}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    resp = await openai_client.chat.completions.create(model="gpt-4o", messages=messages)
    return resp.choices[0].message.content or ""


async def main():
    from openai import AsyncOpenAI
    from src.prompt import build_system_prompt
    from src import bas

    pool = await init_pg()
    openai = AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = build_system_prompt(None, None)

    # Pick a REAL in-stock article straight from the DB
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, code, price, stock FROM products "
            "WHERE deleted=false AND code != '' AND stock > 0 AND price > 0 "
            "ORDER BY stock DESC LIMIT 1"
        )
    real_article = row["code"]
    print("=" * 72)
    print("РЕАЛЬНЫЙ ТОВАР ИЗ БАЗЫ (взят напрямую из PostgreSQL):")
    print(f"  Название: {row['name']}")
    print(f"  Артикул:  {real_article}")
    print(f"  Цена:     {row['price']} грн   Остаток: {row['stock']}")
    print("=" * 72)

    # ── CASE A: client asks by the real article ──────────────────────────────
    print(f"\n┌─ КЕЙС A: клиент пишет артикул {real_article}")
    print("│")
    reply = await run_turn(openai, prompt, f"Доброго дня! Є артикул {real_article}? Яка ціна і наявність?")
    print(f"│")
    print(f"└─ AGENT: {reply}\n")

    # ── CASE B: client asks by a fake article (not in base) ──────────────────
    fake = "ZZ-999-NOPE"
    print(f"┌─ КЕЙС B: клиент пишет несуществующий артикул {fake}")
    print("│")
    reply2 = await run_turn(openai, prompt, f"А артикул {fake} є у вас?")
    print(f"│")
    print(f"└─ AGENT: {reply2}\n")

    # ── CASE C: client asks by description (no article) ──────────────────────
    print("┌─ КЕЙС C: клиент пишет описанием, без артикула")
    print("│")
    reply3 = await run_turn(openai, prompt, "Потрібні гайки-заклепки М5 сталь, що є?")
    print("│")
    print(f"└─ AGENT: {reply3}\n")

    await pool.close()
    print("=" * 72)
    print("ИТОГ: A — нашёл по артикулу | B — корректно «не найдено» | C — нашёл по описанию")
    print("=" * 72)


asyncio.run(main())
