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
        await db.commit()
    logger.info(f"DB initialised at {DB_PATH}")


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
