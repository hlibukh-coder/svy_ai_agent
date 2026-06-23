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

# max_retries: SDK retries 429 / transient errors with exponential backoff so a
# rate-limit spike doesn't drop a client message.
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, max_retries=4)

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


async def run_openai(messages: list, sender_phone: str) -> tuple[str, set]:
    """Run OpenAI with function calling loop. Returns (reply, set_of_called_tools)."""
    called: set = set()
    for _ in range(MAX_TOOL_ITERATIONS):
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools.TOOLS_SCHEMA,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or "", called

        messages.append(msg)

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            called.add(fn_name)
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
    return response.choices[0].message.content or "", called


TG_MSG_LIMIT = 4000  # Telegram hard limit is 4096; keep margin for safety.

# Agent promises a hand-off when a hand-off verb (передам/перекажу/покличу/залучу/
# зв'яжу) co-occurs with a manager token, or when it promises a manager will write
# "найближчим часом". Regex (not substrings) so common Ukrainian phrasings match.
_HANDOFF_RE = re.compile(
    r"(переда\w*|перекаж\w*|поклич\w*|залуч\w*|з[вʼ'`]?яж\w*)[^.\n]{0,40}"
    r"(менеджер|спеціаліст|колег)"
    r"|(менеджер|спеціаліст)\w*[^.\n]{0,40}(зв[вʼ'`]?яж\w*|напише|відповіст|зателефон)"
    r"|напише\s+(вам\s+)?найближч"
    r"|зв[вʼ'`]?яж\w*\s+з\s+вами\s+найближч",
    re.IGNORECASE,
)
# Don't fire on a NEGATED promise ("я НЕ передам менеджеру без вашої згоди").
_HANDOFF_NEG_RE = re.compile(
    r"\b(не|ні)\b[^.\n]{0,15}(переда\w*|перекаж\w*|поклич\w*|залуч\w*)",
    re.IGNORECASE,
)


def _promised_handoff(reply: str) -> bool:
    low = (reply or "").lower()
    if not low:
        return False
    if _HANDOFF_NEG_RE.search(low):
        return False
    return bool(_HANDOFF_RE.search(low))


async def _ensure_handoff(reply: str, called: set, summary: str, phone: str):
    """Completion guarantee: if the agent PROMISED a hand-off but no terminal tool
    (notify_manager / create_order) fired, escalate anyway so nothing dead-ends."""
    if called & {"notify_manager", "create_order"}:
        return
    if not _promised_handoff(reply):
        return
    try:
        await tools.execute_tool(
            "notify_manager",
            {"reason": "complex_question", "summary": (reply[:300] or (summary or "")[:300])},
            phone,
        )
        logger.info("[SAFETY] auto-escalated a promised hand-off")
    except Exception as e:
        logger.error(f"[SAFETY] auto-escalation failed: {e}")


def _hard_chunks(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split one block into <=limit pieces on newline/space, hard-cut as last resort."""
    text = text.strip()
    if not text:
        return []
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _split_reply(reply: str, max_parts: int = 4) -> list[str]:
    """Split an agent reply into separate Telegram messages on blank lines, the way
    a real manager sends several short messages. Question-lists (single-newline lines
    with leading "- ") stay in one block. Capped to avoid spam, and every part is
    forced under Telegram's length limit."""
    reply = (reply or "").strip()
    if not reply:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", reply) if b.strip()]
    if not blocks:
        return []
    if len(blocks) > max_parts:
        blocks = blocks[: max_parts - 1] + ["\n\n".join(blocks[max_parts - 1:])]
    parts: list[str] = []
    for b in blocks:
        parts.extend(_hard_chunks(b))
    return parts


async def _send_reply(tg_client, event, reply: str):
    """Send the reply as one or several natural messages with typing indicators.
    Each send is isolated so a single failure can't abort the rest."""
    parts = _split_reply(reply)
    if not parts:
        return
    for i, part in enumerate(parts):
        if i:
            try:
                async with tg_client.action(event.chat_id, "typing"):
                    await asyncio.sleep(min(2.0, 0.5 + len(part) / 140))
            except Exception:
                pass
        try:
            await event.respond(part)
        except Exception as e:
            logger.error(f"[SEND] respond failed: {e}")


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

    # Per-chat human takeover: an operator is handling this chat → record but stay silent
    if await context.is_chat_paused(chat_id):
        await context.save_message(chat_id, "user", user_text)
        logger.info(f"[IN] chat {chat_id} under human control — AI silent")
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

        ok = True
        try:
            reply, called = await run_openai(messages, phone)
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            reply, called, ok = "Вибачте, сталася помилка. Спробуйте ще раз.", set(), False

        # ── Step 3b: completion guarantee — a promised hand-off MUST really escalate
        if ok:
            await _ensure_handoff(reply, called, user_text, phone)

        # Never send an empty message (e.g. a tool-only turn) — give a real next step
        if not (reply or "").strip():
            reply = (
                "Дякую! Передав ваш запит, менеджер зв'яжеться з вами найближчим часом."
                if called & {"create_order", "notify_manager"}
                else "Хвилинку, уточню і повернусь до вас."
            )

        # ── Step 4: check if reply contains a phone (client gave it mid-chat) ─
        if not linked and not phone:
            extracted = _extract_phone(reply)  # agent might repeat it back
            # Also check user text again more carefully
            extracted = _extract_phone(user_text) or extracted
            if extracted:
                await _resolve_and_link(chat_id, extracted)

        await context.save_message(chat_id, "assistant", reply)

    logger.info(f"[OUT] chat={chat_id} text={reply[:80]}")
    await _send_reply(tg_client, event, reply)


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
        reply, _called = await run_openai(messages, phone)
    except Exception as e:
        logger.error(f"[SEND] OpenAI error: {e}")
        return {"ok": False, "error": str(e)}

    await _ensure_handoff(reply, _called, text, phone)
    if not (reply or "").strip():
        reply = "Дякую! Передав ваш запит, менеджер зв'яжеться з вами найближчим часом."

    await context.save_message(chat_id, "user", text)
    await context.save_message(chat_id, "assistant", reply)

    try:
        await _tg_client.send_message(entity, reply)
        logger.info(f"[SEND] Sent to {phone}: {reply[:80]}")
    except Exception as e:
        logger.error(f"[SEND] Failed to send to {phone}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "reply": reply}


async def operator_send(chat_id: str, text: str) -> dict:
    """Send a RAW operator (human) message into a chat and pause the AI there.

    Used by the dashboard so a manager can take over a conversation. The message is
    saved to history (as the business side) and the AI stops auto-replying in this
    chat until it is re-enabled from the dashboard.
    """
    if _tg_client is None:
        return {"ok": False, "error": "Telegram не підключено"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Порожнє повідомлення"}
    try:
        peer = int(chat_id)
    except (TypeError, ValueError):
        peer = chat_id
    try:
        await _tg_client.send_message(peer, text)
    except Exception as e:
        logger.error(f"[OPERATOR] send to {chat_id} failed: {e}")
        return {"ok": False, "error": str(e)}
    await context.save_message(chat_id, "assistant", text)
    await context.set_chat_ai_paused(chat_id, True)  # human took over this chat
    logger.info(f"[OPERATOR] sent to {chat_id}; AI paused for this chat")
    return {"ok": True}


async def activate_client(tg_client, start_scheduler: bool = True):
    """Wire an AUTHORIZED client into the running app: globals, handler, scheduler.

    Called both on normal startup and right after a successful QR login from the UI.
    """
    global _tg_client
    _tg_client = tg_client
    escalation_peer = int(ESCALATION_CHAT_ID) if ESCALATION_CHAT_ID else None
    tools.set_tg_client(tg_client, escalation_peer)
    if escalation_peer is None:
        logger.warning(
            "[TG] ESCALATION_CHAT_ID не задан — передачи менеджеру и уведомления о "
            "заказах идут в Saved Messages бота. Укажите ESCALATION_CHAT_ID для "
            "отдельного чата менеджера."
        )
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
