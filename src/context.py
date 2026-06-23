import logging
import os
import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/history.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                ts        DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_clients (
                chat_id         TEXT PRIMARY KEY,
                phone           TEXT,
                client_ref_key  TEXT,
                name            TEXT,
                linked_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_control (
                chat_id     TEXT PRIMARY KEY,
                ai_paused   INTEGER NOT NULL DEFAULT 0,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()
    logger.info(f"DB initialised at {DB_PATH}")


async def set_chat_ai_paused(chat_id: str, paused: bool):
    """Pause/resume the AI for a single chat (human takeover)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_control (chat_id, ai_paused, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "ai_paused = excluded.ai_paused, updated_at = CURRENT_TIMESTAMP",
            (str(chat_id), 1 if paused else 0),
        )
        await db.commit()


async def is_chat_paused(chat_id: str) -> bool:
    """True if a human operator is handling this chat and the AI should stay silent."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ai_paused FROM chat_control WHERE chat_id = ?", (str(chat_id),)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


async def load_history(chat_id: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, ts
                FROM messages
                WHERE chat_id = ?
                ORDER BY ts DESC
                LIMIT ?
            ) ORDER BY ts ASC
            """,
            (str(chat_id), limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"role": row[0], "content": row[1]} for row in rows]


async def save_message(chat_id: str, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (str(chat_id), role, content),
        )
        await db.commit()


async def get_linked_client(chat_id: str) -> dict | None:
    """Return linked client info for a Telegram chat_id, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, client_ref_key, name FROM telegram_clients WHERE chat_id = ?",
            (str(chat_id),),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {"phone": row[0], "client_ref_key": row[1], "name": row[2]}
    return None


async def link_client(chat_id: str, phone: str, client_ref_key: str, name: str):
    """Persist the chat_id → client mapping."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO telegram_clients (chat_id, phone, client_ref_key, name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE
            SET phone = excluded.phone,
                client_ref_key = excluded.client_ref_key,
                name = excluded.name,
                linked_at = CURRENT_TIMESTAMP
            """,
            (str(chat_id), phone, client_ref_key, name),
        )
        await db.commit()
    logger.info(f"[IDENTITY] Linked chat={chat_id} phone={phone} client={client_ref_key}")


async def get_all_chats(limit: int = 100) -> list[dict]:
    """Return all chat_ids sorted by last message time — used by dashboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT m.chat_id,
                   MAX(m.ts) AS last_ts,
                   (SELECT content FROM messages m2
                    WHERE m2.chat_id = m.chat_id ORDER BY ts DESC LIMIT 1) AS last_msg,
                   tc.name, tc.phone, tc.client_ref_key
            FROM messages m
            LEFT JOIN telegram_clients tc ON tc.chat_id = m.chat_id
            GROUP BY m.chat_id
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "chat_id": r[0],
            "last_ts": r[1],
            "last_msg": r[2],
            "name": r[3],
            "phone": r[4],
            "client_ref_key": r[5],
        }
        for r in rows
    ]
