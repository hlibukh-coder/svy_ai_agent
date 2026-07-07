"""
Channel-agnostic inbound router. This is the AI brain extracted from
index.handle_message: it takes a normalized InboundMessage + the originating
adapter, resolves client identity, runs the OpenAI tool loop, persists history,
and replies THROUGH the originating adapter (no Telethon coupling).
"""
import logging

from src import bas, config, context, files
from src.channels.base import InboundMessage
from src.prompt import build_system_prompt

logger = logging.getLogger(__name__)


async def _store_attachments(msg: InboundMessage) -> list[dict]:
    """Persist inbound files (photo/document/voice) to local storage so the
    operator can open/save them from the dashboard."""
    saved = []
    for att in msg.attachments:
        rec = await files.save_attachment(att)
        if rec:
            saved.append(rec)
        else:
            logger.warning(f"[IN] attachment '{att.filename}' not saved ({msg.conv_id})")
    return saved


async def _save_user_message(msg: InboundMessage, user_text: str, saved: list[dict]) -> None:
    """One inbound → one message row (+ a row per extra attachment), keeping the
    provider message id so the operator can react to it later."""
    await context.save_message(conv_id=msg.conv_id, role="user", content=user_text,
                               external_id=msg.external_id,
                               attachment=saved[0] if saved else None)
    for extra in saved[1:]:
        await context.save_message(conv_id=msg.conv_id, role="user", content="",
                                   attachment=extra)


async def _resolve_and_link(conv_id: str, phone: str, channel: str, account_id: int,
                            peer: str, email: str = "", name_hint: str = "") -> dict | None:
    """Find the BAS client by phone (any channel) or remember the contact, persisting
    the conversation→client link. Mirrors index._resolve_and_link but conv-aware."""
    client_data = await bas.get_client(phone) if phone else None
    if client_data:
        await context.link_client(
            conv_id=conv_id, channel=channel, account_id=account_id, peer=peer,
            phone=phone, email=email or None,
            client_ref_key=client_data.get("id", ""), name=client_data.get("name", ""),
        )
        logger.info(f"[IDENTITY] Auto-linked conv={conv_id} → {client_data.get('name')}")
    else:
        await context.link_client(
            conv_id=conv_id, channel=channel, account_id=account_id, peer=peer,
            phone=phone, email=email or None, client_ref_key="", name=name_hint or "",
        )
        logger.info(f"[IDENTITY] Stored contact for conv={conv_id} (phone={phone} email={email})")
    return client_data


async def route_inbound(msg: InboundMessage, adapter) -> None:
    """Handle one inbound message from any channel."""
    # Lazy import to avoid an import cycle (index imports the channels package).
    from src import index

    if not msg.conv_id:
        msg.conv_id = f"{msg.channel}:{msg.account_id}:{msg.peer}"
    conv_id = msg.conv_id
    user_text = (msg.text or "").strip()
    if not user_text and not msg.attachments:
        return

    # Store inbound files first — they must be kept in EVERY mode (paused/manual too).
    saved_files = await _store_attachments(msg)
    if saved_files:
        # The AI (and the history) must know a file arrived, not just its caption.
        labels = ", ".join(f["filename"] for f in saved_files)
        kind = "фото" if (saved_files[0].get("mimetype") or "").startswith("image/") else "файл"
        marker = f"[{kind}: {labels}]"
        user_text = f"{user_text}\n{marker}".strip() if user_text else marker

    logger.info(f"[IN] conv={conv_id} text={user_text[:80]}")

    # Keep the contact card fresh (name/phone from the channel profile) in EVERY
    # mode — even when the AI stays silent — so dialogs show real names, not IDs.
    try:
        await context.upsert_contact_profile(
            conv_id, name=msg.sender_name or "", phone=msg.sender_phone or "")
    except Exception as e:
        logger.warning(f"[IN] contact upsert failed for {conv_id}: {e}")

    # Master switch: agent paused → record but don't reply.
    if not await config.get_value("agent_enabled", True):
        await _save_user_message(msg, user_text, saved_files)
        logger.info(f"[IN] agent paused — saved but not replying ({conv_id})")
        return

    # Per-conversation human takeover → record but stay silent.
    if await context.is_chat_paused(conv_id=conv_id):
        await _save_user_message(msg, user_text, saved_files)
        logger.info(f"[IN] conv {conv_id} under human control — AI silent")
        return

    # Manual mode: the AI does NOT reply on its own — it answers only when the
    # operator triggers it ("AI, відповісти"). Record the inbound and stay silent.
    if not await config.get_value("auto_reply", True):
        await _save_user_message(msg, user_text, saved_files)
        logger.info(f"[IN] auto-reply off — saved, awaiting operator trigger ({conv_id})")
        return

    # ── identity resolution (channel-aware) ──────────────────────────────────
    linked = await context.get_linked_client(conv_id=conv_id)
    client_data = None
    phone = ""

    if linked:
        phone = linked.get("phone") or ""
        if linked.get("client_ref_key"):
            client_data = await bas.get_client(phone) if phone else None
            if not client_data:
                client_data = {
                    "id": linked["client_ref_key"], "name": linked.get("name", ""),
                    "phone": phone, "company": "", "city": "",
                }
        elif phone:
            client_data = await _resolve_and_link(conv_id, phone, msg.channel,
                                                  msg.account_id, msg.peer)
    else:
        # First contact: prefer the phone/email the channel gave us, else parse the text.
        phone = msg.sender_phone or index._extract_phone(user_text) or ""
        email = msg.sender_email or ""
        if phone or email:
            client_data = await _resolve_and_link(
                conv_id, phone, msg.channel, msg.account_id, msg.peer,
                email=email, name_hint=msg.sender_name,
            )
        elif msg.sender_name:
            # Remember at least the name so the dashboard isn't blank.
            await context.link_client(
                conv_id=conv_id, channel=msg.channel, account_id=msg.account_id,
                peer=msg.peer, name=msg.sender_name,
            )

    # ── orders + prompt + history ────────────────────────────────────────────
    orders = []
    if client_data:
        orders = await bas.get_orders(client_data["id"])

    cfg_prompt = await config.get_value("system_prompt", "")
    system_prompt = build_system_prompt(client_data, orders, base_prompt=cfg_prompt or None)
    history = await context.load_history(conv_id=conv_id, limit=20)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    await _save_user_message(msg, user_text, saved_files)

    conv = {"conv_id": conv_id, "channel": msg.channel,
            "account_id": msg.account_id, "peer": msg.peer, "phone": phone}

    ok = True
    try:
        reply, called = await index.run_openai(messages, phone, conv=conv)
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply, called, ok = "Вибачте, сталася помилка. Спробуйте ще раз.", set(), False

    if ok:
        await index._ensure_handoff(reply, called, user_text, phone, conv=conv)

    if not (reply or "").strip():
        reply = (
            "Дякую! Передав ваш запит, менеджер зв'яжеться з вами найближчим часом."
            if called & {"create_order", "notify_manager"}
            else "Хвилинку, уточню і повернусь до вас."
        )

    # Client may have given a phone mid-chat.
    if not linked and not phone:
        extracted = index._extract_phone(user_text)
        if extracted:
            await _resolve_and_link(conv_id, extracted, msg.channel, msg.account_id, msg.peer)

    await context.save_message(conv_id=conv_id, role="assistant", content=reply)

    logger.info(f"[OUT] conv={conv_id} text={reply[:80]}")
    await adapter.send_reply(msg.peer, reply)
