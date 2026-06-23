"""Прогон агента по сценариям из реальных диалогов Сергія — проверка стиля и сути."""
import asyncio, json, logging, os
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
C_RESET="\033[0m"; C_USER="\033[96m"; C_BOT="\033[92m"; C_TOOL="\033[93m"; C_HEAD="\033[1;95m"


async def run_turn(messages, phone):
    tool_log = []
    for _ in range(6):
        resp = await client.chat.completions.create(
            model="gpt-4o", messages=messages, tools=tools.TOOLS_SCHEMA, tool_choice="auto")
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", tool_log
        messages.append(msg)
        for tc in msg.tool_calls:
            try: args = json.loads(tc.function.arguments)
            except json.JSONDecodeError: args = {}
            result = await tools.execute_tool(tc.function.name, args, phone)
            tool_log.append((tc.function.name, args, result))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    resp = await client.chat.completions.create(model="gpt-4o", messages=messages)
    return resp.choices[0].message.content or "", tool_log


async def scenario(title, turns):
    print(f"\n{C_HEAD}{'='*82}\n  {title}\n{'='*82}{C_RESET}")
    messages = [{"role": "system", "content": build_system_prompt(None, [])}]
    for user_text in turns:
        print(f"\n{C_USER}👤 КЛІЄНТ: {user_text}{C_RESET}")
        messages.append({"role": "user", "content": user_text})
        reply, tool_log = await run_turn(messages, "")
        for name, args, result in tool_log:
            short = result if len(result) < 240 else result[:240] + "…"
            print(f"{C_TOOL}   🔧 {name}({json.dumps(args, ensure_ascii=False)}) → {short}{C_RESET}")
        messages.append({"role": "assistant", "content": reply})
        print(f"{C_BOT}🤖 АГЕНТ: {reply}{C_RESET}")


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool
    await context.init_db()

    await scenario("1) ОРЕНДА ЗАКЛЬОПОЧНИКА (як Ірина)", [
        "Вітаю. Розмовляли з вами стосовно оренди закльопочника",
        "М5, М6, М8, М10",
        "На вулиці, метал 3 мм. Заклепки ваші. На 5 днів. ФОП.",
    ])
    await scenario("2) ЗАКЛЕПКИ ЗА ТОВЩИНОЮ (як Вадим)", [
        "Сергей, добрый день! Нужны заклепки. Подскажите есть в наличии или аналог?",
        "3+1=4 мм",
        "3000 шт",
    ])
    await scenario("3) МЕТИЗИ DIN + РАХУНОК (як Дмитро)", [
        "Привіт. Потрібно: Гайка з фланцем DIN 6923 M8 - 50 шт, Болт DIN 933 M8*30 - 25 шт. Можеш виставити рахунок?",
    ])
    await scenario("4) ПЕРЕКУП / ПО 2 ШТ (як Рома)", [
        "Привіт. Нам треба буквально по 2 шт кожної гайки зі списку. Є в тебе такі по наявності?",
    ])
    await scenario("5) НАЯВНІСТЬ БОЛТІВ", [
        "Є болти М5, М6, М8, М10?",
    ])
    await pool.close()
    print(f"\n{C_HEAD}{'='*82}\n  ГОТОВО.\n{'='*82}{C_RESET}")


if __name__ == "__main__":
    asyncio.run(main())
