"""
One-shot PostgreSQL bootstrap for a fresh machine.

Run by start.py (and run.bat) before the server starts, so a brand-new device gets
the schema with a single command instead of a manual `psql -f sync/migration.sql`:

    1. read DATABASE_URL / USE_MOCK from .env
    2. create the target database if it doesn't exist yet
    3. apply sync/migration.sql (all tables are CREATE TABLE IF NOT EXISTS — idempotent)

Non-fatal by design: if Postgres is unreachable it prints a hint and exits 0, so the
dashboard still comes up (it works without PG, just with empty business data).
"""
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sync" / "migration.sql"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass


async def _bootstrap() -> None:
    use_mock = os.getenv("USE_MOCK", "true").lower() == "true"
    db_url = os.getenv("DATABASE_URL", "")
    if use_mock or not db_url:
        print("· bootstrap БД: USE_MOCK або без DATABASE_URL — пропускаю (працюю на моках).")
        return

    import asyncpg  # available in the venv

    parts = urlsplit(db_url)
    dbname = parts.path.lstrip("/") or "postgres"
    # Maintenance connection to the default 'postgres' DB to (maybe) CREATE the target.
    # On managed hosts (Neon/Supabase/RDS) the DB already exists and the admin DB may
    # be unreachable — that's fine, we skip creation and apply the schema directly.
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    try:
        admin = await asyncpg.connect(admin_url, timeout=10)
        try:
            exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
            if not exists:
                # asyncpg can't parametrize identifiers; dbname comes from our own .env.
                await admin.execute(f'CREATE DATABASE "{dbname}"')
                print(f"✓ Створив базу даних «{dbname}».")
            else:
                print(f"· База даних «{dbname}» вже існує.")
        finally:
            await admin.close()
    except Exception as e:
        # Can't reach the admin DB — assume the target DB already exists (managed host).
        print(f"· Пропускаю створення БД (адмін-підключення недоступне: {type(e).__name__}); "
              f"вважаю, що «{dbname}» вже існує.")

    if not MIGRATION.exists():
        print(f"⚠ Немає {MIGRATION} — пропускаю застосування схеми.")
        return

    try:
        conn = await asyncpg.connect(db_url, timeout=15)
    except Exception as e:
        print(f"⚠ bootstrap БД: не вдалося підключитись до «{dbname}» ({e}).")
        print(f"  Перевірте DATABASE_URL: {parts.netloc}")
        print("  Дашборд підніметься, але товари/клієнти будуть порожні, поки немає БД.")
        return
    try:
        await conn.execute(MIGRATION.read_text(encoding="utf-8"))
        print("✓ Схему застосовано (sync/migration.sql).")
    finally:
        await conn.close()


def main() -> None:
    _load_env()
    try:
        asyncio.run(_bootstrap())
    except Exception as e:
        # Never block server startup on bootstrap problems.
        print(f"⚠ bootstrap БД: {e} — продовжую без зупинки.")
    sys.exit(0)


if __name__ == "__main__":
    main()
