import asyncio
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.tl.types import User

from src import context, scheduler, tools
from src.prompt import build_system_prompt
from src import bas

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_PHONE = os.getenv("TG_PHONE", "")
MANAGER_TG_ID = int(os.getenv("MANAGER_TG_ID", "0") or "0")
ESCALATION_CHAT_ID = os.getenv("ESCALATION_CHAT_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_TOOL_ITERATIONS = 5

# Shared tg_client instance (set in main(), used by send_to_client)
_tg_client = None


async def get_sender_phone(tg_client: TelegramClient, sender: User) -> str:
    """Return phone number of the sender if available."""
    if sender and sender.phone:
        phone = sender.phone
        return phone if phone.startswith("+") else f"+{phone}"
    return ""


async def run_openai(messages: list, sender_phone: str) -> str:
    """Run OpenAI with function calling loop (up to MAX_TOOL_ITERATIONS)."""
    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools.TOOLS_SCHEMA,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        # No tool calls — return text answer
        if not msg.tool_calls:
            return msg.content or ""

        # Append assistant message with tool_calls
        messages.append(msg)

        # Execute all tool calls
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"[TOOL CALL] {fn_name} args={fn_args}")
            result = await tools.execute_tool(fn_name, fn_args, sender_phone)
            logger.info(f"[TOOL RESULT] {fn_name} -> {result[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Fallback: ask for final answer without tools
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
    )
    return response.choices[0].message.content or ""


async def handle_message(tg_client: TelegramClient, event):
    sender: User = await event.get_sender()

    # Ignore non-user senders (bots, channels)
    if not sender or not isinstance(sender, User):
        return

    # Don't reply to ourselves
    if MANAGER_TG_ID and sender.id == MANAGER_TG_ID:
        return

    # Only private chats
    if not event.is_private:
        return

    chat_id = str(event.chat_id)
    user_text = event.raw_text.strip()
    if not user_text:
        return

    logger.info(f"[IN] chat={chat_id} user={sender.id} text={user_text[:80]}")

    # Show typing indicator
    async with tg_client.action(event.chat_id, "typing"):

        # Get sender phone
        sender_phone = await get_sender_phone(tg_client, sender)

        # Fetch client data from BAS
        client_data = None
        orders = []
        if sender_phone:
            client_data = await bas.get_client(sender_phone)
            if client_data:
                orders = await bas.get_orders(client_data["id"])

        # Build system prompt
        system_prompt = build_system_prompt(client_data, orders)

        # Load history
        history = await context.load_history(chat_id, limit=20)

        # Build messages list
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        # Save user message
        await context.save_message(chat_id, "user", user_text)

        # Run OpenAI
        try:
            reply = await run_openai(messages, sender_phone)
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            reply = "Вибачте, сталася помилка. Спробуйте ще раз."

        # Save assistant reply
        await context.save_message(chat_id, "assistant", reply)

    # Send reply
    logger.info(f"[OUT] chat={chat_id} text={reply[:80]}")
    await event.respond(reply)


async def send_to_client(phone: str, text: str) -> dict:
    """Send an outbound message to a client by phone number.
    The text is sent as-is (already composed by caller or AI).
    Also saves the message to history.
    """
    if _tg_client is None:
        return {"ok": False, "error": "Telegram client not running"}

    # Resolve peer by phone
    try:
        entity = await _tg_client.get_entity(phone)
    except Exception as e:
        logger.error(f"[SEND] Cannot resolve {phone}: {e}")
        return {"ok": False, "error": f"Cannot resolve phone: {e}"}

    chat_id = str(entity.id)

    # Fetch client data from BAS to build proper system prompt
    client_data = await bas.get_client(phone)
    orders = []
    if client_data:
        orders = await bas.get_orders(client_data["id"])

    system_prompt = build_system_prompt(client_data, orders)
    history = await context.load_history(chat_id, limit=20)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    try:
        reply = await run_openai(messages, phone)
    except Exception as e:
        logger.error(f"[SEND] OpenAI error: {e}")
        return {"ok": False, "error": str(e)}

    await context.save_message(chat_id, "user", text)
    await context.save_message(chat_id, "assistant", reply)

    try:
        await _tg_client.send_message(entity, reply)
        logger.info(f"[SEND] Sent to {phone}: {reply[:80]}")
    except Exception as e:
        logger.error(f"[SEND] Failed to send to {phone}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "reply": reply}


async def main():
    # Init DB
    os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
    await context.init_db()

    # Init Telegram client
    tg_client = TelegramClient(
        "session/svy_agent",
        TG_API_ID,
        TG_API_HASH,
    )

    global _tg_client
    await tg_client.start(phone=TG_PHONE)
    _tg_client = tg_client
    logger.info("Telegram client started")

    # Pass tg_client to tools for escalation
    escalation_peer = int(ESCALATION_CHAT_ID) if ESCALATION_CHAT_ID else None
    tools.set_tg_client(tg_client, escalation_peer)

    # Start scheduler
    scheduler.start(tg_client)

    # Register message handler
    @tg_client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        await handle_message(tg_client, event)

    logger.info("Listening for messages...")
    await tg_client.run_until_disconnected()

    scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
