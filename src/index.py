import asyncio
import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.tl.types import User

from src import context, scheduler, tools, config
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

_tg_client = None

# Regex to extract Ukrainian/international phone numbers from text
_PHONE_RE = re.compile(r"(?:\+?38)?0\d{9}|\+\d{10,13}")


def _extract_phone(text: str) -> str | None:
    """Extract and normalise a phone number from free text."""
    match = _PHONE_RE.search(text.replace(" ", "").replace("-", ""))
    if not match:
        return None
    raw = match.group()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+38{digits}"
    if len(digits) == 12:
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("38"):
        return f"+{digits}"
    return None


async def _resolve_and_link(chat_id: str, phone: str) -> dict | None:
    """Try to find client in PG/BAS by phone and persist the link.

    If the phone is not yet a BAS client, we still remember the phone (with an
    empty client_ref_key) so the dashboard and follow-up messages keep it. Such
    rows are excluded from proactive scheduler messaging by design.
    """
    client_data = await bas.get_client(phone)
    if client_data:
        await context.link_client(
            chat_id,
            phone,
            client_data.get("id", ""),
            client_data.get("name", ""),
        )
        logger.info(f"[IDENTITY] Auto-linked chat={chat_id} → {client_data.get('name')}")
    else:
        # New client — remember the phone even without a BAS match
        await context.link_client(chat_id, phone, "", "")
        logger.info(f"[IDENTITY] Stored phone {phone} for chat={chat_id} (not in BAS yet)")
    return client_data


async def get_sender_phone(tg_client: TelegramClient, sender: User) -> str:
    """Return phone number of the sender if Telegram shares it (contacts only)."""
    if sender and sender.phone:
        phone = sender.phone
        return phone if phone.startswith("+") else f"+{phone}"
    return ""


async def run_openai(messages: list, sender_phone: str) -> str:
    """Run OpenAI with function calling loop (up to MAX_TOOL_ITERATIONS)."""
    for _ in range(MAX_TOOL_ITERATIONS):
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
            logger.info(f"[TOOL RESULT] {fn_name} -> {result[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Fallback: final answer without tools
    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
    )
    return response.choices[0].message.content or ""


async def handle_message(tg_client: TelegramClient, event):
    sender: User = await event.get_sender()

    if not sender or not isinstance(sender, User):
        return
    if MANAGER_TG_ID and sender.id == MANAGER_TG_ID:
        return
    if not event.is_private:
        return

    chat_id = str(event.chat_id)
    user_text = event.raw_text.strip()
    if not user_text:
        return

    logger.info(f"[IN] chat={chat_id} user={sender.id} text={user_text[:80]}")

    # Master switch: if the agent is paused, record the message but don't reply
    if not await config.get_value("agent_enabled", True):
        await context.save_message(chat_id, "user", user_text)
        logger.info(f"[IN] agent paused — saved but not replying ({chat_id})")
        return

    # Hint for initial setup: log chat_id of anyone who messages us
    if not ESCALATION_CHAT_ID:
        logger.info(f"[HINT] ESCALATION_CHAT_ID not set. Add to .env: ESCALATION_CHAT_ID={chat_id}")

    async with tg_client.action(event.chat_id, "typing"):

        # ── Step 1: resolve client identity ──────────────────────────────────
        linked = await context.get_linked_client(chat_id)

        client_data = None
        phone = ""

        if linked:
            # Already identified — load from PG directly
            phone = linked["phone"] or ""
            if linked["client_ref_key"]:
                client_data = await bas.get_client(phone) if phone else None
                if not client_data and linked["client_ref_key"]:
                    # Build minimal client_data from stored info
                    client_data = {
                        "id": linked["client_ref_key"],
                        "name": linked["name"],
                        "phone": phone,
                        "company": "",
                        "city": "",
                    }
            elif phone:
                # Phone known but no BAS match yet — re-check (manager may have added them)
                client_data = await _resolve_and_link(chat_id, phone)
        else:
            # Try Telegram-provided phone (only works if user is in contacts)
            tg_phone = await get_sender_phone(tg_client, sender)
            if tg_phone:
                client_data = await _resolve_and_link(chat_id, tg_phone)
                phone = tg_phone
            else:
                # Try to extract phone from the message itself
                extracted = _extract_phone(user_text)
                if extracted:
                    client_data = await _resolve_and_link(chat_id, extracted)
                    phone = extracted

        # ── Step 2: load orders if client known ──────────────────────────────
        orders = []
        if client_data:
            orders = await bas.get_orders(client_data["id"])

        # ── Step 3: build prompt and run agent ───────────────────────────────
        cfg_prompt = await config.get_value("system_prompt", "")
        system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
        history = await context.load_history(chat_id, limit=20)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        await context.save_message(chat_id, "user", user_text)

        try:
            reply = await run_openai(messages, phone)
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            reply = "Вибачте, сталася помилка. Спробуйте ще раз."

        # ── Step 4: check if reply contains a phone (client gave it mid-chat) ─
        if not linked and not phone:
            extracted = _extract_phone(reply)  # agent might repeat it back
            # Also check user text again more carefully
            extracted = _extract_phone(user_text) or extracted
            if extracted:
                await _resolve_and_link(chat_id, extracted)

        await context.save_message(chat_id, "assistant", reply)

    logger.info(f"[OUT] chat={chat_id} text={reply[:80]}")
    await event.respond(reply)


async def send_to_client(phone: str, text: str) -> dict:
    """Send outbound message to a client by phone; agent composes the reply."""
    if _tg_client is None:
        return {"ok": False, "error": "Telegram client not running"}

    try:
        entity = await _tg_client.get_entity(phone)
    except Exception as e:
        logger.error(f"[SEND] Cannot resolve {phone}: {e}")
        return {"ok": False, "error": f"Cannot resolve phone: {e}"}

    chat_id = str(entity.id)

    client_data = await bas.get_client(phone)
    orders = []
    if client_data:
        orders = await bas.get_orders(client_data["id"])

    cfg_prompt = await config.get_value("system_prompt", "")
    system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
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


async def activate_client(tg_client, start_scheduler: bool = True):
    """Wire an AUTHORIZED client into the running app: globals, handler, scheduler.

    Called both on normal startup and right after a successful QR login from the UI.
    """
    global _tg_client
    _tg_client = tg_client
    escalation_peer = int(ESCALATION_CHAT_ID) if ESCALATION_CHAT_ID else None
    tools.set_tg_client(tg_client, escalation_peer)
    tg_client.add_event_handler(
        lambda e: handle_message(tg_client, e),
        events.NewMessage(incoming=True),
    )
    if start_scheduler:
        send_hour = int(await config.get_value("send_hour", 10) or 10)
        scheduler.start(tg_client, send_hour)
    logger.info("[TG] client activated (handler + scheduler)")
    return tg_client


async def connect_and_register(start_scheduler: bool = True):
    """
    Connect using an EXISTING session (non-interactive) and wire up handlers +
    proactive scheduler. Returns the client, or None if the session isn't
    authorized yet (use the dashboard QR login, or run `python src/index.py`).
    """
    os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
    await context.init_db()

    tg_client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
    await tg_client.connect()
    if not await tg_client.is_user_authorized():
        logger.warning("[TG] session not authorized — use dashboard QR login")
        await tg_client.disconnect()
        return None

    return await activate_client(tg_client, start_scheduler=start_scheduler)


async def main():
    """Standalone runner — also handles INTERACTIVE first login (SMS code)."""
    os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
    await context.init_db()

    tg_client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)

    global _tg_client
    await tg_client.start(phone=TG_PHONE)
    _tg_client = tg_client
    logger.info("Telegram client started")

    escalation_peer = int(ESCALATION_CHAT_ID) if ESCALATION_CHAT_ID else None
    tools.set_tg_client(tg_client, escalation_peer)

    send_hour = int(await config.get_value("send_hour", 10) or 10)
    scheduler.start(tg_client, send_hour)

    @tg_client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        await handle_message(tg_client, event)

    logger.info("Listening for messages...")
    await tg_client.run_until_disconnected()

    scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
