import logging
import os
import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/history.db")

# Conversation identity is a stable composite key across channels/accounts:
#   conv_id = "{channel}:{account_id}:{peer}"
# Legacy Telegram (the single original session) is account id=1, so old bare
# chat_ids map to "telegram:1:<chat_id>" and existing history keeps working.
LEGACY_TG_ACCOUNT_ID = 1
KNOWN_CHANNELS = ("telegram", "whatsapp", "email", "viber", "elevenlabs")


def legacy_conv_id(chat_id) -> str:
    return f"telegram:{LEGACY_TG_ACCOUNT_ID}:{chat_id}"


def _looks_like_conv_id(value: str) -> bool:
    parts = str(value).split(":", 2)
    return len(parts) == 3 and parts[0] in KNOWN_CHANNELS and parts[1].isdigit()


def as_conv_id(value) -> str:
    """Accept either a full conv_id ('channel:account:peer') or a legacy bare
    Telegram chat_id and return a canonical conv_id. This is what makes every
    existing caller (which passes a bare chat_id) keep working unchanged."""
    s = str(value)
    return s if _looks_like_conv_id(s) else legacy_conv_id(s)


def parse_conv_id(conv_id: str) -> tuple[str, int, str]:
    """conv_id -> (channel, account_id, peer). Falls back to telegram/legacy."""
    s = str(conv_id)
    if _looks_like_conv_id(s):
        ch, acc, peer = s.split(":", 2)
        return ch, int(acc), peer
    return "telegram", LEGACY_TG_ACCOUNT_ID, s


async def _column_exists(db, table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    return column in cols


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
        # Kept for rollback / back-compat; reads/writes now go through `contacts`.
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
        # Generalized contact/conversation table (replaces telegram_clients going forward).
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                conv_id        TEXT PRIMARY KEY,
                channel        TEXT NOT NULL,
                account_id     INTEGER NOT NULL,
                peer           TEXT NOT NULL,
                phone          TEXT,
                email          TEXT,
                client_ref_key TEXT,
                name           TEXT,
                linked_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ── additive, idempotent migrations ──────────────────────────────────
        for col, ddl in (
            ("channel",    "ALTER TABLE messages ADD COLUMN channel TEXT"),
            ("account_id", "ALTER TABLE messages ADD COLUMN account_id INTEGER"),
            ("conv_id",    "ALTER TABLE messages ADD COLUMN conv_id TEXT"),
            # provider message id — lets us react to / reference the original message
            ("external_id", "ALTER TABLE messages ADD COLUMN external_id TEXT"),
            # reaction WE put on the client's message (👍 ❤️ …), shown in the dashboard
            ("reaction",   "ALTER TABLE messages ADD COLUMN reaction TEXT"),
            # inbound/outbound file linked to this message (stored under FILES_DIR)
            ("file_path",  "ALTER TABLE messages ADD COLUMN file_path TEXT"),
            ("file_name",  "ALTER TABLE messages ADD COLUMN file_name TEXT"),
            ("file_mime",  "ALTER TABLE messages ADD COLUMN file_mime TEXT"),
        ):
            if not await _column_exists(db, "messages", col):
                await db.execute(ddl)
        # Backfill legacy Telegram rows (channel/account/conv_id derived from old chat_id).
        await db.execute(
            "UPDATE messages SET channel='telegram', account_id=?, "
            "conv_id='telegram:' || ? || ':' || chat_id WHERE conv_id IS NULL",
            (LEGACY_TG_ACCOUNT_ID, LEGACY_TG_ACCOUNT_ID),
        )

        if not await _column_exists(db, "chat_control", "conv_id"):
            await db.execute("ALTER TABLE chat_control ADD COLUMN conv_id TEXT")
        await db.execute(
            "UPDATE chat_control SET conv_id='telegram:' || ? || ':' || chat_id "
            "WHERE conv_id IS NULL",
            (LEGACY_TG_ACCOUNT_ID,),
        )

        # One-time backfill of contacts from the old telegram_clients table.
        await db.execute(
            "INSERT OR IGNORE INTO contacts "
            "(conv_id, channel, account_id, peer, phone, client_ref_key, name) "
            "SELECT 'telegram:' || ? || ':' || chat_id, 'telegram', ?, chat_id, "
            "phone, client_ref_key, name FROM telegram_clients",
            (LEGACY_TG_ACCOUNT_ID, LEGACY_TG_ACCOUNT_ID),
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel)")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_control_conv ON chat_control(conv_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_contacts_client ON contacts(client_ref_key)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")

        # Voice calls (ElevenLabs post-call webhooks) — structured ledger so all the
        # call info (summary, duration, recording, outcome) lives in one place, while
        # the transcript itself is also mirrored into `messages` so calls show up in
        # the dashboard "Діалоги" alongside chats. PK = provider conversation_id → the
        # webhook is idempotent on retries / restarts.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                conversation_id TEXT PRIMARY KEY,
                conv_id         TEXT NOT NULL,
                channel         TEXT NOT NULL DEFAULT 'elevenlabs',
                account_id      INTEGER NOT NULL,
                peer            TEXT,
                phone           TEXT,
                direction       TEXT,
                status          TEXT,
                duration_secs   INTEGER,
                summary         TEXT,
                recording_url   TEXT,
                started_at      DATETIME,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_calls_conv ON calls(conv_id)")
        await db.commit()

    # accounts table + legacy seed (same DB) — lazy import avoids a circular import
    from src import accounts as account_manager
    await account_manager.init_accounts_table()
    logger.info(f"DB initialised at {DB_PATH}")


# ── pause / human takeover ────────────────────────────────────────────────────
async def set_chat_ai_paused(chat_id=None, paused: bool = True, *, conv_id: str | None = None):
    """Pause/resume the AI for a single conversation (human takeover)."""
    cid = conv_id or as_conv_id(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_control (chat_id, conv_id, ai_paused, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(conv_id) DO UPDATE SET "
            "ai_paused = excluded.ai_paused, updated_at = CURRENT_TIMESTAMP",
            (cid, cid, 1 if paused else 0),
        )
        await db.commit()


async def is_chat_paused(chat_id=None, *, conv_id: str | None = None) -> bool:
    """True if a human operator is handling this conversation and the AI should stay silent."""
    cid = conv_id or as_conv_id(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ai_paused FROM chat_control WHERE conv_id = ?", (cid,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


# ── history ───────────────────────────────────────────────────────────────────
async def load_history(chat_id=None, limit: int = 20, *, conv_id: str | None = None) -> list[dict]:
    cid = conv_id or as_conv_id(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, ts
                FROM messages
                WHERE conv_id = ?
                ORDER BY ts DESC
                LIMIT ?
            ) ORDER BY ts ASC
            """,
            (cid, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"role": row[0], "content": row[1]} for row in rows]


async def save_message(chat_id=None, role: str = "", content: str = "", *,
                       conv_id: str | None = None, channel: str | None = None,
                       account_id: int | None = None, peer: str | None = None,
                       external_id: str = "", attachment: dict | None = None) -> int:
    """Persist one message; returns its row id. `attachment` is the dict returned
    by files.save_attachment ({"path","filename","mimetype"})."""
    cid = conv_id or as_conv_id(chat_id)
    ch, acc, pr = parse_conv_id(cid)
    channel = channel or ch
    account_id = account_id if account_id is not None else acc
    peer = peer or pr
    att = attachment or {}
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO messages (chat_id, role, content, channel, account_id, conv_id, "
            " external_id, file_path, file_name, file_mime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (peer, role, content, channel, account_id, cid,
             external_id or None, att.get("path"), att.get("filename"), att.get("mimetype")),
        )
        await db.commit()
        return cur.lastrowid


async def get_message(message_id: int) -> dict | None:
    """One message row by id — used by the reaction and file-download endpoints."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, external_id, reaction, "
            " file_path, file_name, file_mime FROM messages WHERE id = ?",
            (int(message_id),),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_message_reaction(message_id: int, emoji: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE messages SET reaction = ? WHERE id = ?",
                         (emoji or None, int(message_id)))
        await db.commit()


# ── voice calls ───────────────────────────────────────────────────────────────
async def save_call(conversation_id: str, conv_id: str, account_id: int, *,
                    channel: str = "elevenlabs", peer: str = "", phone: str = "",
                    direction: str = "", status: str = "", duration_secs: int = 0,
                    summary: str = "", recording_url: str = "",
                    started_at: str | None = None) -> bool:
    """Insert a call record. Returns True if it was NEW (first time we see this
    provider conversation_id), False if it was already ingested — so the webhook is
    safe to retry and survives restarts without duplicating the transcript."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO calls "
            "(conversation_id, conv_id, channel, account_id, peer, phone, direction, "
            " status, duration_secs, summary, recording_url, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, conv_id, channel, int(account_id), peer, phone, direction,
             status, int(duration_secs or 0), summary, recording_url, started_at),
        )
        await db.commit()
        return cur.rowcount > 0


async def recent_calls(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM calls ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── contact / identity linkage ────────────────────────────────────────────────
async def get_linked_client(chat_id=None, *, conv_id: str | None = None) -> dict | None:
    """Return linked client/contact info for a conversation, or None."""
    cid = conv_id or as_conv_id(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, client_ref_key, name, email, channel, account_id "
            "FROM contacts WHERE conv_id = ?",
            (cid,),
        ) as cursor:
            row = await cursor.fetchone()
    if row:
        return {
            "phone": row[0], "client_ref_key": row[1], "name": row[2],
            "email": row[3], "channel": row[4], "account_id": row[5],
        }
    return None


async def link_client(chat_id=None, phone: str = "", client_ref_key: str = "",
                      name: str = "", *, conv_id: str | None = None,
                      channel: str | None = None, account_id: int | None = None,
                      peer: str | None = None, email: str | None = None):
    """Persist the conversation → client mapping (channel/account aware)."""
    cid = conv_id or as_conv_id(chat_id)
    ch, acc, pr = parse_conv_id(cid)
    channel = channel or ch
    account_id = account_id if account_id is not None else acc
    peer = peer or pr
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO contacts
                (conv_id, channel, account_id, peer, phone, email, client_ref_key, name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                phone = excluded.phone,
                email = COALESCE(excluded.email, contacts.email),
                client_ref_key = excluded.client_ref_key,
                name = excluded.name,
                linked_at = CURRENT_TIMESTAMP
            """,
            (cid, channel, account_id, peer, phone, email or None, client_ref_key, name),
        )
        await db.commit()
    logger.info(f"[IDENTITY] Linked conv={cid} phone={phone} client={client_ref_key}")


async def upsert_contact_profile(conv_id: str, name: str = "", phone: str = "") -> None:
    """Fill/refresh the human profile (name/phone) of a conversation WITHOUT
    touching the BAS link (client_ref_key/email) — safe to call on every inbound.
    Empty values never overwrite existing ones."""
    if not (name or phone):
        return
    ch, acc, pr = parse_conv_id(conv_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO contacts (conv_id, channel, account_id, peer, phone, name)
            VALUES (?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''))
            ON CONFLICT(conv_id) DO UPDATE SET
                name  = COALESCE(NULLIF(excluded.name, ''),  contacts.name),
                phone = COALESCE(NULLIF(excluded.phone, ''), contacts.phone)
            """,
            (conv_id, ch, acc, pr, phone or "", name or ""),
        )
        await db.commit()


async def telegram_convs_without_name(account_id: int) -> list[str]:
    """Conv_ids of this Telegram account's chats that still display as bare IDs
    (no contact name) — candidates for the post-connect name backfill."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT DISTINCT m.conv_id FROM messages m
            LEFT JOIN contacts c ON c.conv_id = m.conv_id
            WHERE m.channel='telegram' AND m.account_id=? AND m.conv_id IS NOT NULL
              AND (c.name IS NULL OR c.name='')
            """,
            (int(account_id),),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_all_chats(limit: int = 100, *, channel: str | None = None,
                        account_id: int | None = None) -> list[dict]:
    """Return all conversations sorted by last message time — used by the dashboard."""
    where, params = [], []
    if channel:
        where.append("m.channel = ?"); params.append(channel)
    if account_id is not None:
        where.append("m.account_id = ?"); params.append(int(account_id))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT m.conv_id,
                   MAX(m.ts) AS last_ts,
                   (SELECT content FROM messages m2
                    WHERE m2.conv_id = m.conv_id ORDER BY ts DESC LIMIT 1) AS last_msg,
                   c.name, c.phone, c.client_ref_key,
                   m.channel, m.account_id, a.label AS account_label
            FROM messages m
            LEFT JOIN contacts c ON c.conv_id = m.conv_id
            LEFT JOIN accounts a ON a.id = m.account_id
            {where_sql}
            GROUP BY m.conv_id
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            # `chat_id` keeps the frontend's identifier key working; it now carries conv_id.
            "chat_id": r[0],
            "conv_id": r[0],
            "last_ts": r[1],
            "last_msg": r[2],
            "name": r[3],
            "phone": r[4],
            "client_ref_key": r[5],
            "channel": r[6] or "telegram",
            "account_id": r[7],
            "account_label": r[8],
        }
        for r in rows
    ]
