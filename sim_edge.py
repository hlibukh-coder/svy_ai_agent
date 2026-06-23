"""Широкая батарея edge-кейсов реального менеджера — для оценки и доводки логики."""
import asyncio, json, logging, os
from dotenv import load_dotenv
load_dotenv()
import asyncpg
from openai import AsyncOpenAI
from src import bas, context, tools
from src.prompt import build_system_prompt
from sync import scheduler_sync

logging.basicConfig(level=logging.ERROR)
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


async def scenario(num, title, turns):
    print(f"\n{C_HEAD}{'='*84}\n  [{num}] {title}\n{'='*84}{C_RESET}")
    messages = [{"role": "system", "content": build_system_prompt(None, [])}]
    for user_text in turns:
        print(f"\n{C_USER}👤 {user_text}{C_RESET}")
        messages.append({"role": "user", "content": user_text})
        reply, tool_log = await run_turn(messages, "")
        for name, args, result in tool_log:
            short = result if len(result) < 180 else result[:180] + "…"
            print(f"{C_TOOL}   🔧 {name}({json.dumps(args, ensure_ascii=False)}) → {short}{C_RESET}")
        messages.append({"role": "assistant", "content": reply})
        print(f"{C_BOT}🤖 {reply}{C_RESET}")


SCENARIOS = [
    ("01 Привітання без запиту", ["Привет"]),
    ("02 Розпливчастий запит", ["Почем стрейч?"]),
    ("03 Заперечення по ціні", ["Дорого. У конкурента дешевше беру."]),
    ("04 Запит знижки/відстрочки", ["Дайте знижку 15% і відстрочку платежу на місяць"]),
    ("05 Метизи список + рахунок", ["Привіт. Потрібно: Гайка DIN 934 M8 - 200 шт, Болт DIN 933 M8x30 - 100 шт. Виставиш рахунок?"]),
    ("06 Заклепки за товщиною", ["Доброго дня! Потрібні витяжні заклепки. Є аналог?", "пакет 4 мм", "5000 шт, нержавійка"]),
    ("07 Оренда закльопочника", ["Вітаю, цікавить оренда закльопочника", "М6, М8", "На вулиці, метал 3мм, заклепки ваші, на 5 днів, ФОП"]),
    ("08 Плутанина інструменту", ["Мені потрібен закльопочник. А яким ставлять клепальну гайку М8?"]),
    ("09 Доставка/Нова Пошта", ["Скільки коштує доставка і коли відправите?"]),
    ("10 Непрофільний товар", ["У вас є зварювальний апарат або болгарка?"]),
    ("11 Оплата готівкою", ["А готівкою можна оплатити оренду?"]),
    ("12 Дав пошту/реквізити", ["Виставляйте рахунок на ТОВ Альфа, пошта buh@alfa.ua, без ПДВ"]),
    ("13 Скарга на брак", ["Минулого разу прислали брак, частина заклепок не тримає"]),
    ("14 Питання чи це бот", ["Это бот или живой человек?"]),
    ("15 Часткова наявність", ["Треба 5000 шт заклепок 4,8х16,5. Скільки є і коли відвантажите?"]),
    ("16 Пошук по артикулу/DIN", ["Є болт DIN 933 M8x30 А2? Скільки коштує?"]),
]


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    scheduler_sync._pool = pool
    await context.init_db()
    for i, (title, turns) in enumerate(SCENARIOS, 1):
        await scenario(f"{i:02d}", title, turns)
    await pool.close()
    print(f"\n{C_HEAD}{'='*84}\n  ГОТОВО.\n{'='*84}{C_RESET}")


if __name__ == "__main__":
    asyncio.run(main())
