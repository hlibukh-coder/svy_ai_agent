"""
Account / session registry for ALL channels (Telegram, WhatsApp via WAHA, Email,
Viber). This is the source of truth the dashboard writes to and the channel
adapters read from, so accounts can be added/removed from the UI without code
changes or .env edits.

Stored in the SQLite history.db (DB_PATH) — it always exists, even in USE_MOCK,
so onboarding a channel never depends on PostgreSQL being up.

Secrets (per-channel `credentials` + `session_blob`) are encrypted at rest with
Fernet when ACCOUNTS_SECRET_KEY is set; otherwise stored as plaintext with a
one-time warning (dev/mock convenience, mirrors the project's "degrade
gracefully" style). `cryptography` ships transitively with Telethon.
"""
import json
import logging
import os
import secrets

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "data/history.db")

# The pre-existing single Telegram session is pinned to account id=1 so the legacy
# file session (session/svy_agent) and all old history keep working unchanged.
LEGACY_TG_ACCOUNT_ID = 1
LEGACY_TG_SESSION = "session/svy_agent"

VALID_CHANNELS = ("telegram", "whatsapp", "email", "viber", "elevenlabs")
VALID_STATUS = ("disconnected", "connecting", "authorized", "error")

# Per-channel `credentials` JSON shape (documentation, validated loosely):
#   telegram : {}  (api_id/api_hash come from .env; session in session_blob)
#   whatsapp : {"base_url","api_key","session_name","escalation_peer"?}
#   email    : {"imap_host","imap_port","smtp_host","smtp_port","user","password",
#               "use_ssl","from_name"?}
#   viber    : {"bot_token","sender_name"?,"sender_avatar"?}
#   elevenlabs : {"api_key"?,"agent_id"?,"webhook_secret"}  (post-call webhook ingest)


# ── encryption at rest ────────────────────────────────────────────────────────
_fernet = None
_fernet_loaded = False
_warned_plaintext = False


def _get_fernet():
    global _fernet, _fernet_loaded
    if _fernet_loaded:
        return _fernet
    _fernet_loaded = True
    key = os.getenv("ACCOUNTS_SECRET_KEY", "")
    if not key:
        _fernet = None
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        logger.error(f"[ACCOUNTS] invalid ACCOUNTS_SECRET_KEY ({e}) — falling back to plaintext")
        _fernet = None
    return _fernet


def _enc(obj) -> str | None:
    """Serialize + (optionally) encrypt a credentials/session value for storage."""
    if obj is None:
        return None
    raw = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    f = _get_fernet()
    if f is None:
        global _warned_plaintext
        if not _warned_plaintext:
            logger.warning(
                "[ACCOUNTS] ACCOUNTS_SECRET_KEY not set — account credentials are "
                "stored in PLAINTEXT. Set ACCOUNTS_SECRET_KEY (a Fernet key) in prod."
            )
            _warned_plaintext = True
        return "plain:" + raw
    return "enc:" + f.encrypt(raw.encode("utf-8")).decode("utf-8")


def _dec(blob):
    """Inverse of _enc. Returns a dict if the payload was JSON, else the raw str."""
    if blob is None:
        return None
    s = str(blob)
    if s.startswith("plain:"):
        s = s[len("plain:"):]
    elif s.startswith("enc:"):
        f = _get_fernet()
        if f is None:
            logger.error("[ACCOUNTS] encrypted blob present but no ACCOUNTS_SECRET_KEY")
            return None
        try:
            s = f.decrypt(s[len("enc:"):].encode("utf-8")).decode("utf-8")
        except Exception as e:
            logger.error(f"[ACCOUNTS] decrypt failed: {e}")
            return None
    # Either a JSON object/array or a bare string (e.g. a Telethon StringSession)
    try:
        return json.loads(s)
    except Exception:
        return s


def _meta(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}


def _row_to_dict(row, include_secrets: bool = False) -> dict:
    d = {
        "id": row["id"],
        "channel": row["channel"],
        "label": row["label"],
        "status": row["status"],
        "enabled": bool(row["enabled"]),
        "meta": _meta(row["meta"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_secrets:
        d["credentials"] = _dec(row["credentials"]) or {}
        d["session_blob"] = _dec(row["session_blob"])
    return d


# ── schema ────────────────────────────────────────────────────────────────────
async def init_accounts_table():
    """Create the accounts table + seed the legacy Telegram account (id=1).
    Idempotent; called from context.init_db()."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel      TEXT NOT NULL,
                label        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'disconnected',
                enabled      INTEGER NOT NULL DEFAULT 1,
                credentials  TEXT,
                session_blob TEXT,
                meta         TEXT DEFAULT '{}',
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_accounts_channel ON accounts(channel)")
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE channel='telegram'") as cur:
            n = (await cur.fetchone())[0]
        if n == 0:
            await db.execute(
                "INSERT INTO accounts (id, channel, label, status, enabled, meta) "
                "VALUES (?, 'telegram', ?, 'disconnected', 1, ?)",
                (LEGACY_TG_ACCOUNT_ID, "Telegram (основний)",
                 json.dumps({"legacy_session": LEGACY_TG_SESSION})),
            )
            logger.info("[ACCOUNTS] seeded legacy Telegram account id=1")

        # Seed a default WhatsApp (WAHA) account so WhatsApp works out of the box
        # right after a clone — the operator only has to scan the QR. Points at the
        # local WAHA server (auto-started by the launcher). Gated by WHATSAPP_AUTOSEED
        # (default on); if you delete the account and don't want it re-created, set
        # WHATSAPP_AUTOSEED=false.
        if os.getenv("WHATSAPP_AUTOSEED", "true").lower() == "true":
            async with db.execute(
                "SELECT COUNT(*) FROM accounts WHERE channel='whatsapp'") as cur:
                n_wa = (await cur.fetchone())[0]
            if n_wa == 0:
                creds = {
                    "base_url": os.getenv("WAHA_URL", "http://localhost:3000").rstrip("/"),
                    "session_name": "default",
                    "webhook_secret": secrets.token_urlsafe(16),
                }
                await db.execute(
                    "INSERT INTO accounts (channel, label, status, enabled, credentials, meta) "
                    "VALUES ('whatsapp', ?, 'disconnected', 1, ?, '{}')",
                    ("WhatsApp (WAHA)", _enc(creds)),
                )
                logger.info("[ACCOUNTS] seeded default WhatsApp (WAHA) account")
        await db.commit()
    logger.info("[ACCOUNTS] table ready")


# ── reads ─────────────────────────────────────────────────────────────────────
async def list_accounts(channel: str | None = None, include_secrets: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if channel:
            cur = await db.execute("SELECT * FROM accounts WHERE channel=? ORDER BY id", (channel,))
        else:
            cur = await db.execute("SELECT * FROM accounts ORDER BY id")
        rows = await cur.fetchall()
    return [_row_to_dict(r, include_secrets) for r in rows]


async def get_account(account_id: int, include_secrets: bool = False) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM accounts WHERE id=?", (int(account_id),))
        row = await cur.fetchone()
    return _row_to_dict(row, include_secrets) if row else None


# ── writes ────────────────────────────────────────────────────────────────────
async def add_account(channel: str, label: str, credentials: dict | None = None,
                      meta: dict | None = None, status: str = "disconnected") -> int:
    if channel not in VALID_CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO accounts (channel, label, status, credentials, meta) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, label or channel.title(), status, _enc(credentials or {}),
             json.dumps(meta or {})),
        )
        await db.commit()
        new_id = cur.lastrowid
    logger.info(f"[ACCOUNTS] added {channel} account id={new_id} ({label})")
    return new_id


async def update_account(account_id: int, *, label: str | None = None,
                         credentials: dict | None = None, meta: dict | None = None,
                         enabled: bool | None = None) -> None:
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if credentials is not None:
        sets.append("credentials=?"); params.append(_enc(credentials))
    if enabled is not None:
        sets.append("enabled=?"); params.append(1 if enabled else 0)
    if meta is not None:
        # shallow-merge meta rather than replace
        existing = await get_account(account_id)
        merged = {**(existing["meta"] if existing else {}), **meta}
        sets.append("meta=?"); params.append(json.dumps(merged))
    if not sets:
        return
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(int(account_id))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", params)
        await db.commit()


async def update_status(account_id: int, status: str, error: str | None = None) -> None:
    """Adapters call this on connect / disconnect / failure. Stashes the error into
    meta.last_error for the dashboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        if error is not None:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT meta FROM accounts WHERE id=?", (int(account_id),))
            row = await cur.fetchone()
            meta = _meta(row["meta"]) if row else {}
            meta["last_error"] = error
            await db.execute(
                "UPDATE accounts SET status=?, meta=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, json.dumps(meta), int(account_id)),
            )
        else:
            await db.execute(
                "UPDATE accounts SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, int(account_id)),
            )
        await db.commit()


async def save_session(account_id: int, session_blob) -> None:
    """Persist the channel session (Telethon StringSession / WAHA state / IMAP cursor)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET session_blob=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (_enc(session_blob), int(account_id)),
        )
        await db.commit()


async def set_enabled(account_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (1 if enabled else 0, int(account_id)),
        )
        await db.commit()


async def delete_account(account_id: int) -> bool:
    """Delete an account. Refuses to delete the legacy Telegram account (id=1) —
    callers should disable it instead. Returns True if a row was removed."""
    if int(account_id) == LEGACY_TG_ACCOUNT_ID:
        logger.warning("[ACCOUNTS] refusing to delete legacy account id=1 (disable instead)")
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM accounts WHERE id=?", (int(account_id),))
        await db.commit()
        return cur.rowcount > 0
