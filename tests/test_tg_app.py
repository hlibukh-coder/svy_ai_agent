"""Tests for src.tg_app — baked-in Telegram app creds with env override."""
import importlib

import src.tg_app as tg_app


def _reload():
    return importlib.reload(tg_app)


def test_defaults_when_env_missing(monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    try:
        m = _reload()
        assert m.TG_API_ID == m.DEFAULT_TG_API_ID
        assert m.TG_API_HASH == m.DEFAULT_TG_API_HASH
    finally:
        monkeypatch.undo()
        _reload()


def test_placeholder_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "your_api_id")
    monkeypatch.setenv("TG_API_HASH", "your_api_hash")
    try:
        m = _reload()
        assert m.TG_API_ID == m.DEFAULT_TG_API_ID
        assert m.TG_API_HASH == m.DEFAULT_TG_API_HASH
    finally:
        monkeypatch.undo()
        _reload()


def test_real_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abcdef0123456789")
    try:
        m = _reload()
        assert m.TG_API_ID == 12345
        assert m.TG_API_HASH == "abcdef0123456789"
    finally:
        monkeypatch.undo()
        _reload()
