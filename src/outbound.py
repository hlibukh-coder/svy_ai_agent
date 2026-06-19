"""
Outbound message composer — generates PERSONALIZED first-contact messages via gpt-4o.

Per the product spec, proactive messages must NOT be templates: each one is written
by the AI individually, using the client's name and real purchase history. There is
NO template fallback — if generation fails, the caller skips sending that client.
"""
import logging
import os

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    return _client


OUTBOUND_SYSTEM = """Ты — менеджер компании СВЮ.КЛУБ (крепёж и метизы оптом по Украине).
Ты пишешь клиенту ПЕРВЫМ в мессенджер — это исходящее сообщение, клиент его не ждал.

ПРАВИЛА:
- Пиши на украинском языке.
- Одно короткое живое сообщение от лица человека-менеджера. Никаких шаблонов и канцелярщины.
- Обязательно обращайся к клиенту по имени.
- НЕ выдумывай конкретные товары, артикулы, цены или количества, которых нет в данных ниже.
- Заканчивай мягким вопросом, на который легко ответить «так» и продолжить диалог.
- Не упоминай что ты бот или AI.
- Тон тёплый, уважительный, без напора.
- Ніякого markdown: жодних **, *, -, нумерованих списків. Звичайний живий текст як в SMS.
Верни ТОЛЬКО текст сообщения, без кавычек и пояснений."""


async def compose_message(reason: str, name: str, context: str) -> str:
    """Generate a personalized outbound message. Returns '' on failure (caller skips send)."""
    user = (
        f"Имя клиента: {name}\n"
        f"Повод написать клиенту: {reason}\n"
        f"Что известно о клиенте (используй только это, не придумывай лишнего):\n{context}\n\n"
        f"Напиши одно персональное сообщение."
    )
    try:
        resp = await _openai().chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": OUTBOUND_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.7,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip().strip('"')
        if not text:
            logger.error(f"[OUTBOUND] empty completion for {name} ({reason})")
        return text
    except Exception as e:
        logger.error(f"[OUTBOUND] compose failed for {name} ({reason}): {e}")
        return ""
