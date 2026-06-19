"""
Telegram helpers for OUTBOUND: resolve a phone number to a Telegram entity so we
can message BAS clients who never wrote us first.

A Telethon userbot (real account) CAN initiate chats — we just need to resolve the
phone to a user. If it isn't already a contact, we import it temporarily via
contacts.ImportContactsRequest. Returns None if the number has no Telegram account
or the user's privacy blocks resolution.
"""
import logging
import re

logger = logging.getLogger(__name__)


def extract_ua_phone(raw: str) -> str | None:
    """Pull a clean +380XXXXXXXXX number out of a messy BAS phone field.

    BAS stores phones like "0504442888, 0504442888", "380512580903",
    "4943535Факс:4590386" (junk). Returns normalized +380… or None if no valid
    Ukrainian mobile is found.
    """
    if not raw:
        return None
    for d in re.findall(r"\d+", raw):
        if len(d) == 10 and d.startswith("0"):
            return "+38" + d
        if len(d) == 12 and d.startswith("380"):
            return "+" + d
    # fallback: collapse all digits and re-check
    d = re.sub(r"\D", "", raw)
    if len(d) == 10 and d.startswith("0"):
        return "+38" + d
    if len(d) == 12 and d.startswith("380"):
        return "+" + d
    return None


async def resolve_phone_entity(tg_client, phone: str, name: str = ""):
    """Resolve a phone to a Telegram entity, importing as a contact if needed.

    Returns the entity, or None if the number has no Telegram / can't be reached.
    """
    if not tg_client or not phone:
        return None
    p = phone if phone.startswith("+") else "+" + phone.lstrip("+")

    # 1) Already resolvable (existing contact or cached)
    try:
        return await tg_client.get_entity(p)
    except Exception:
        pass

    # 2) Import as a contact to resolve
    try:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        first = (name or "Client").split()[0][:64]
        res = await tg_client(ImportContactsRequest(
            [InputPhoneContact(client_id=0, phone=p, first_name=first, last_name="")]
        ))
        if res.users:
            return res.users[0]
        logger.info(f"[TG] {p} has no Telegram account")
    except Exception as e:
        logger.error(f"[TG] resolve {p} failed: {e}")
    return None
