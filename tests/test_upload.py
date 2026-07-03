"""Tests for the operator file upload endpoint POST /api/chat/{id}/upload."""
from fastapi.testclient import TestClient

import main
from src import index


def _client():
    # No `with` → lifespan (DB init, Telegram connect) is not triggered.
    return TestClient(main.app)


def test_upload_sends_file_through_operator_send_file(monkeypatch):
    calls = []

    async def fake_send(chat_id, file, caption="", filename="", mimetype=""):
        calls.append({"chat_id": chat_id, "file": file, "caption": caption,
                      "filename": filename, "mimetype": mimetype})
        return {"ok": True}

    monkeypatch.setattr(index, "operator_send_file", fake_send)
    r = _client().post("/api/chat/123/upload",
                       files={"file": ("паспорт.pdf", b"%PDF-1.4 test", "application/pdf")},
                       data={"caption": "Паспорт якості"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls and calls[0]["file"] == b"%PDF-1.4 test"
    assert calls[0]["filename"] == "паспорт.pdf"
    assert calls[0]["mimetype"] == "application/pdf"
    assert calls[0]["caption"] == "Паспорт якості"


def test_upload_guesses_mimetype_from_filename(monkeypatch):
    calls = []

    async def fake_send(chat_id, file, caption="", filename="", mimetype=""):
        calls.append(mimetype)
        return {"ok": True}

    monkeypatch.setattr(index, "operator_send_file", fake_send)
    r = _client().post("/api/chat/123/upload",
                       files={"file": ("photo.jpg", b"\xff\xd8\xff", "application/octet-stream")})
    assert r.status_code == 200
    assert calls == ["image/jpeg"]


def test_upload_empty_file_is_400(monkeypatch):
    async def fake_send(*a, **k):
        raise AssertionError("must not send an empty file")

    monkeypatch.setattr(index, "operator_send_file", fake_send)
    r = _client().post("/api/chat/123/upload", files={"file": ("x.pdf", b"", "application/pdf")})
    assert r.status_code == 400


def test_upload_channel_not_connected_is_409(monkeypatch):
    async def fake_send(*a, **k):
        return {"ok": False, "error": "telegram не підключено"}

    monkeypatch.setattr(index, "operator_send_file", fake_send)
    r = _client().post("/api/chat/123/upload",
                       files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 409
    assert "підключено" in r.json()["detail"]
