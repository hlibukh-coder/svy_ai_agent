"""Tests for src/context.py — SQLite dialog history."""
import os
import tempfile
import pytest
import src.context as ctx_module


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    db_file = str(tmp_path / "test_history.db")
    ctx_module.DB_PATH = db_file
    return db_file


@pytest.mark.asyncio
async def test_init_db_creates_table(tmp_db):
    await ctx_module.init_db()
    import aiosqlite
    async with aiosqlite.connect(tmp_db) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_save_and_load_message(tmp_db):
    await ctx_module.init_db()
    await ctx_module.save_message("chat_1", "user", "Привіт!")
    history = await ctx_module.load_history("chat_1")
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Привіт!"


@pytest.mark.asyncio
async def test_load_history_empty(tmp_db):
    await ctx_module.init_db()
    history = await ctx_module.load_history("nonexistent_chat")
    assert history == []


@pytest.mark.asyncio
async def test_load_history_order(tmp_db):
    await ctx_module.init_db()
    await ctx_module.save_message("chat_2", "user", "Перше")
    await ctx_module.save_message("chat_2", "assistant", "Відповідь")
    await ctx_module.save_message("chat_2", "user", "Друге")
    history = await ctx_module.load_history("chat_2")
    assert len(history) == 3
    assert history[0]["content"] == "Перше"
    assert history[1]["role"] == "assistant"
    assert history[2]["content"] == "Друге"


@pytest.mark.asyncio
async def test_load_history_limit(tmp_db):
    await ctx_module.init_db()
    for i in range(25):
        await ctx_module.save_message("chat_3", "user", f"msg {i}")
    history = await ctx_module.load_history("chat_3", limit=20)
    assert len(history) == 20


@pytest.mark.asyncio
async def test_chat_isolation(tmp_db):
    await ctx_module.init_db()
    await ctx_module.save_message("chat_A", "user", "Повідомлення A")
    await ctx_module.save_message("chat_B", "user", "Повідомлення B")
    history_a = await ctx_module.load_history("chat_A")
    history_b = await ctx_module.load_history("chat_B")
    assert len(history_a) == 1
    assert history_a[0]["content"] == "Повідомлення A"
    assert len(history_b) == 1
    assert history_b[0]["content"] == "Повідомлення B"


@pytest.mark.asyncio
async def test_save_multiple_roles(tmp_db):
    await ctx_module.init_db()
    await ctx_module.save_message("chat_4", "user", "Запит")
    await ctx_module.save_message("chat_4", "assistant", "Відповідь менеджера")
    history = await ctx_module.load_history("chat_4")
    roles = [m["role"] for m in history]
    assert "user" in roles
    assert "assistant" in roles
