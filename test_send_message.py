"""
Test script: send a message to @s_cggf as a new lead through full flow.
Run AFTER auth_telegram.py to ensure session is authorized.
"""
import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from openai import AsyncOpenAI
from telethon import TelegramClient

from src.prompt import build_system_prompt
from src.bas import get_client, get_orders
from src import context, tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_PHONE = os.getenv("TG_PHONE", "")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

TARGET_USERNAME = "@s_cggf"
# Simulated incoming message from new lead (no phone in BAS)
SIMULATED_USER_MESSAGE = "Привіт! Скільки коштують болти М8?"

MAX_TOOL_ITERATIONS = 5


async def run_openai_flow(messages: list, sender_phone: str) -> str:
    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools.TOOLS_SCHEMA,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg)

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"[TOOL CALL] {fn_name} args={fn_args}")
            result = await tools.execute_tool(fn_name, fn_args, sender_phone)
            logger.info(f"[TOOL RESULT] {fn_name} -> {result[:300]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
    )
    return response.choices[0].message.content or ""


async def main():
    # Init DB
    os.makedirs("data", exist_ok=True)
    await context.init_db()

    # Init Telegram client
    tg_client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
    await tg_client.connect()

    authorized = await tg_client.is_user_authorized()
    if not authorized:
        logger.error("❌ Telegram session not authorized! Run auth_telegram.py first.")
        await tg_client.disconnect()
        return

    me = await tg_client.get_me()
    logger.info(f"✅ Authorized as: {me.first_name} (+{me.phone})")

    # Set tg_client for tools (escalation)
    tools.set_tg_client(tg_client, None)

    # Resolve target user
    try:
        target = await tg_client.get_entity(TARGET_USERNAME)
        logger.info(f"✅ Target user: {target.first_name} (id={target.id})")
    except Exception as e:
        logger.error(f"❌ Cannot resolve {TARGET_USERNAME}: {e}")
        await tg_client.disconnect()
        return

    # Simulate new lead (no phone → no BAS data)
    sender_phone = ""
    client_data = None
    orders = []

    # Build system prompt for new lead
    system_prompt = build_system_prompt(client_data, orders)
    logger.info(f"[SYSTEM PROMPT TYPE] new lead")

    # Use target.id as chat_id for history
    chat_id = str(target.id)

    # Load history
    history = await context.load_history(chat_id, limit=20)

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": SIMULATED_USER_MESSAGE})

    # Save user message to history
    await context.save_message(chat_id, "user", SIMULATED_USER_MESSAGE)

    logger.info(f"[IN] Simulated message: {SIMULATED_USER_MESSAGE}")

    # Run OpenAI with full tool-calling flow
    try:
        reply = await run_openai_flow(messages, sender_phone)
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Вибачте, сталася помилка. Спробуйте ще раз."

    # Save assistant reply
    await context.save_message(chat_id, "assistant", reply)

    logger.info(f"[OUT] Reply: {reply}")

    # Send message via Telegram
    await tg_client.send_message(target, reply)
    logger.info(f"✅ Message sent to {TARGET_USERNAME}")

    await tg_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
