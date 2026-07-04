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
from src.tg_app import TG_API_ID, TG_API_HASH  # baked-in app creds, env overrides
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


async def run_openai(messages: list, sender_phone: str, conv: dict | None = None) -> tuple[str, set]:
    """Run OpenAI with function calling loop. Returns (reply, set_of_called_tools).
    `conv` carries the channel/account/peer context so tools (send_file, escalation)
    can act on the originating conversation."""
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
            result = await tools.execute_tool(fn_name, fn_args, sender_phone, conv=conv)
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


async def _ensure_handoff(reply: str, called: set, summary: str, phone: str, conv: dict | None = None):
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
            conv=conv,
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


# NOTE: inbound message handling now lives in the channel-agnostic router
# (src/channels/router.py); the Telegram-specific glue is in
# src/channels/telegram_adapter.py. `_send_reply` and the helpers above are kept
# for reuse/back-compat.


async def send_to_client(phone: str, text: str, channel: str = "telegram",
                         account_id: int | None = None) -> dict:
    """Send an outbound message to a client by phone; the agent composes the reply.
    Routes through the channel adapter registry (Telegram by default)."""
    from src.channels import registry

    entity = None
    if channel == "telegram":
        adapter = registry.get("telegram", account_id) if account_id else registry.default_telegram()
        tgclient = (adapter.client if adapter else None) or _tg_client
        if tgclient is None:
            return {"ok": False, "error": "Telegram client not running"}
        try:
            entity = await tgclient.get_entity(phone)
        except Exception as e:
            logger.error(f"[SEND] Cannot resolve {phone}: {e}")
            return {"ok": False, "error": f"Cannot resolve phone: {e}"}
        peer = str(entity.id)
        acc = adapter.account_id if adapter else context.LEGACY_TG_ACCOUNT_ID
    else:
        adapter = (registry.get(channel, account_id) if account_id
                   else next(iter(registry.adapters_for_channel(channel)), None))
        if adapter is None:
            return {"ok": False, "error": f"{channel} not connected"}
        peer = adapter.peer_for_phone(phone)
        acc = adapter.account_id

    conv_id = f"{channel}:{acc}:{peer}"
    conv = {"conv_id": conv_id, "channel": channel, "account_id": acc, "peer": peer, "phone": phone}

    client_data = await bas.get_client(phone)
    orders = await bas.get_orders(client_data["id"]) if client_data else []

    cfg_prompt = await config.get_value("system_prompt", "")
    system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
    history = await context.load_history(conv_id=conv_id, limit=20)

    messages = [{"role": "system", "content": system_prompt}, *history,
                {"role": "user", "content": text}]

    try:
        reply, _called = await run_openai(messages, phone, conv=conv)
    except Exception as e:
        logger.error(f"[SEND] OpenAI error: {e}")
        return {"ok": False, "error": str(e)}

    await _ensure_handoff(reply, _called, text, phone, conv=conv)
    if not (reply or "").strip():
        reply = "Дякую! Передав ваш запит, менеджер зв'яжеться з вами найближчим часом."

    await context.save_message(conv_id=conv_id, role="user", content=text)
    await context.save_message(conv_id=conv_id, role="assistant", content=reply)

    try:
        if channel == "telegram" and adapter is None:
            m = await _tg_client.send_message(entity, reply)  # legacy fallback
            from src.channels.telegram_adapter import mark_sent
            mark_sent(m)
        else:
            await adapter.send_reply(peer, reply)
        logger.info(f"[SEND] Sent to {phone} via {channel}: {reply[:80]}")
    except Exception as e:
        logger.error(f"[SEND] Failed to send to {phone}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "reply": reply}


async def operator_send(chat_id: str, text: str) -> dict:
    """Send a RAW operator (human) message into a conversation and pause the AI there.
    `chat_id` may be a legacy Telegram chat_id or a full conv_id. Routes through the
    originating channel's adapter; falls back to the legacy Telegram client."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Порожнє повідомлення"}
    conv_id = context.as_conv_id(chat_id)
    channel, account_id, peer = context.parse_conv_id(conv_id)
    from src.channels import registry
    adapter = registry.get(channel, account_id)
    if adapter is not None:
        res = await adapter.send_text(peer, text)
        if not res.ok:
            logger.error(f"[OPERATOR] send to {conv_id} failed: {res.error}")
            return {"ok": False, "error": res.error}
    elif channel == "telegram":
        if _tg_client is None:
            return {"ok": False, "error": "Telegram не підключено"}
        try:
            p = int(peer)
        except (TypeError, ValueError):
            p = peer
        try:
            m = await _tg_client.send_message(p, text)
            from src.channels.telegram_adapter import mark_sent
            mark_sent(m)
        except Exception as e:
            logger.error(f"[OPERATOR] send to {conv_id} failed: {e}")
            return {"ok": False, "error": str(e)}
    else:
        return {"ok": False, "error": f"{channel} не підключено"}
    await context.save_message(conv_id=conv_id, role="assistant", content=text)
    await context.set_chat_ai_paused(conv_id=conv_id, paused=True)  # human took over
    logger.info(f"[OPERATOR] sent to {conv_id}; AI paused for this conversation")
    return {"ok": True}


async def operator_send_file(chat_id: str, file, caption: str = "", filename: str = "",
                             mimetype: str = "") -> dict:
    """Operator sends a FILE into a conversation from the dashboard, pausing the AI."""
    conv_id = context.as_conv_id(chat_id)
    channel, account_id, peer = context.parse_conv_id(conv_id)
    from src.channels import registry
    adapter = registry.get(channel, account_id)
    if adapter is None:
        return {"ok": False, "error": f"{channel} не підключено"}
    res = await adapter.send_file(peer, file, caption=caption, filename=filename, mimetype=mimetype)
    if not res.ok:
        return {"ok": False, "error": res.error}
    await context.save_message(conv_id=conv_id, role="assistant",
                               content=("[файл] " + (filename or caption or "")).strip())
    await context.set_chat_ai_paused(conv_id=conv_id, paused=True)
    return {"ok": True}


async def ai_reply_now(chat_id: str) -> dict:
    """On-demand AI reply: compose and send ONE reply to this conversation right now,
    even when auto-reply is off or the chat is on human-takeover. This is the
    "AI, відповісти" action — the AI answers only when the operator tells it to."""
    conv_id = context.as_conv_id(chat_id)
    channel, account_id, peer = context.parse_conv_id(conv_id)
    from src.channels import registry
    adapter = registry.get(channel, account_id)
    if adapter is None:
        return {"ok": False, "error": f"{channel} не підключено"}

    linked = await context.get_linked_client(conv_id=conv_id)
    phone = (linked or {}).get("phone") or ""
    client_data = None
    if linked and linked.get("client_ref_key"):
        client_data = (await bas.get_client(phone)) if phone else None
        if not client_data:
            client_data = {"id": linked["client_ref_key"], "name": linked.get("name", ""),
                           "phone": phone, "company": "", "city": ""}
    elif phone:
        client_data = await bas.get_client(phone)

    orders = await bas.get_orders(client_data["id"]) if client_data else []
    cfg_prompt = await config.get_value("system_prompt", "")
    system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
    history = await context.load_history(conv_id=conv_id, limit=20)
    if not history:
        return {"ok": False, "error": "Немає повідомлень для відповіді"}

    messages = [{"role": "system", "content": system_prompt}, *history]
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    conv = {"conv_id": conv_id, "channel": channel, "account_id": account_id,
            "peer": peer, "phone": phone}
    try:
        reply, called = await run_openai(messages, phone, conv=conv)
    except Exception as e:
        logger.error(f"[AI-REPLY] OpenAI error for {conv_id}: {e}")
        return {"ok": False, "error": str(e)}

    await _ensure_handoff(reply, called, last_user, phone, conv=conv)
    if not (reply or "").strip():
        reply = "Хвилинку, уточню і повернусь до вас."
    await context.save_message(conv_id=conv_id, role="assistant", content=reply)
    try:
        await adapter.send_reply(peer, reply)
    except Exception as e:
        logger.error(f"[AI-REPLY] send to {conv_id} failed: {e}")
        return {"ok": False, "error": str(e)}
    logger.info(f"[AI-REPLY] operator-triggered reply sent to {conv_id}: {reply[:80]}")
    return {"ok": True, "reply": reply}


OPERATOR_COMMAND_DIRECTIVE = (
    "РЕЖИМ КОМАНДИ ОПЕРАТОРА. Нижче — пряме розпорядження вашого керівника (оператора), "
    "а не повідомлення клієнта. Виконайте його за допомогою доступних інструментів у "
    "контексті ЦІЄЇ розмови з клієнтом.\n"
    "• «Виставити/відправити КП (комерційну пропозицію)» → виклич інструмент create_offer "
    "(позиція, кількість, ціна якщо вказана) — він сам сформує PDF і надішле клієнту.\n"
    "• «Оформити замовлення» → create_order. «Надіслати прайс/паспорт/рахунок» → send_file.\n"
    "• Якщо це лише внутрішня перевірка (наявність, ціна, історія) — поверни відповідь "
    "оператору текстом, клієнту нічого не надсилай.\n"
    "Твоя текстова відповідь повертається ОПЕРАТОРУ як підтвердження виконання; клієнт "
    "отримує лише те, що надсилають інструменти. Стисло підтверди, що зроблено."
)


async def operator_command(chat_id: str, instruction: str) -> dict:
    """Operator drives the agent like an employee: a free-text instruction is executed
    with the full toolset in the context of this conversation (e.g. "выстави КП на
    позицию X, N штук, по Y грн" → create_offer builds the PDF and sends it to the
    client). The agent's text reply is returned to the OPERATOR, not the client."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "Порожня команда"}
    conv_id = context.as_conv_id(chat_id)
    channel, account_id, peer = context.parse_conv_id(conv_id)

    linked = await context.get_linked_client(conv_id=conv_id)
    phone = (linked or {}).get("phone") or ""
    client_data = await bas.get_client(phone) if phone else None
    if not client_data and linked and linked.get("client_ref_key"):
        client_data = {"id": linked["client_ref_key"], "name": linked.get("name", ""),
                       "phone": phone, "company": "", "city": ""}
    orders = await bas.get_orders(client_data["id"]) if client_data else []
    cfg_prompt = await config.get_value("system_prompt", "")
    system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
    history = await context.load_history(conv_id=conv_id, limit=20)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": OPERATOR_COMMAND_DIRECTIVE},
        *history,
        {"role": "user", "content": f"[КОМАНДА ОПЕРАТОРА]: {instruction}"},
    ]
    conv = {"conv_id": conv_id, "channel": channel, "account_id": account_id,
            "peer": peer, "phone": phone,
            "client_name": (client_data or {}).get("name", "") or (linked or {}).get("name", "")}
    try:
        reply, called = await run_openai(messages, phone, conv=conv)
    except Exception as e:
        logger.error(f"[OPERATOR-CMD] OpenAI error for {conv_id}: {e}")
        return {"ok": False, "error": str(e)}

    await config.log_event(
        "operator_command",
        f"Команда оператора: {instruction[:80]}",
        {"conv_id": conv_id, "tools": sorted(called),
         "channel": channel, "account_id": account_id},
    )
    logger.info(f"[OPERATOR-CMD] {conv_id} tools={sorted(called)} :: {instruction[:80]}")
    return {"ok": True, "reply": reply or "Виконано.", "tools": sorted(called)}


async def activate_client(tg_client, start_scheduler: bool = True):
    """Back-compat: wrap an already-authorized Telethon client in the legacy
    TelegramAdapter (account id=1) and register it."""
    from src.channels import manager, registry
    from src.channels.telegram_adapter import TelegramAdapter
    adapter = registry.get("telegram", context.LEGACY_TG_ACCOUNT_ID)
    if adapter is None:
        adapter = TelegramAdapter(context.LEGACY_TG_ACCOUNT_ID, "Telegram (основний)", {}, manager.dispatch)
        adapter.meta = {"legacy_session": "session/svy_agent"}
        registry.register(adapter)
    await adapter._activate(tg_client)
    return tg_client


async def connect_and_register(start_scheduler: bool = True):
    """Back-compat: init DB and start the legacy Telegram account (id=1) through the
    adapter manager. Returns the client, or None if not authorized yet (use QR login)."""
    os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
    await context.init_db()
    from src.channels import manager, registry
    await manager.start_account(context.LEGACY_TG_ACCOUNT_ID)
    adapter = registry.get("telegram", context.LEGACY_TG_ACCOUNT_ID)
    if adapter and getattr(adapter, "client", None):
        try:
            if await adapter.client.is_user_authorized():
                return adapter.client
        except Exception:
            pass
    logger.warning("[TG] session not authorized — use dashboard QR login")
    return None


async def main():
    """Standalone runner — also handles INTERACTIVE first login (SMS code)."""
    os.makedirs(os.path.dirname(os.getenv("DB_PATH", "data/history.db")), exist_ok=True)
    await context.init_db()
    from src.channels import manager, registry
    from src.channels.telegram_adapter import TelegramAdapter

    tg_client = TelegramClient("session/svy_agent", TG_API_ID, TG_API_HASH)
    await tg_client.start(phone=TG_PHONE)
    logger.info("Telegram client started")

    adapter = registry.get("telegram", context.LEGACY_TG_ACCOUNT_ID)
    if adapter is None:
        adapter = TelegramAdapter(context.LEGACY_TG_ACCOUNT_ID, "Telegram (основний)", {}, manager.dispatch)
        adapter.meta = {"legacy_session": "session/svy_agent"}
        registry.register(adapter)
    await adapter._activate(tg_client)

    logger.info("Listening for messages...")
    await tg_client.run_until_disconnected()

    scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
