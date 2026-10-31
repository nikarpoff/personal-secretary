import json

import pytest

from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
        monkeypatch.setenv("AUTH_TOKEN", TOKEN)
        for name in ("GOOGLE_CREDENTIALS_FILE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY"):
            monkeypatch.setenv(name, "")

        from app.core.config import get_settings
        get_settings.cache_clear()
        from app.main import app

        with TestClient(app) as test_client:
            yield test_client
        get_settings.cache_clear()


def _mcp(client: TestClient, method: str, params: dict = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    response = client.post("/mcp/", json=payload, headers=MCP_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_auth_is_required(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/notes").status_code == 401
    assert client.get("/v1/notes", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.post("/mcp/", json={}).status_code == 401


def test_notes_crud_and_search(client):
    created = client.post("/v1/notes", json={"content": "Идея: бот для заметок\nс поиском", "tags": ["#Идеи"]}, headers=AUTH)
    assert created.status_code == 201
    note = created.json()
    assert (note["title"], note["tags"]) == ("Идея: бот для заметок", ["идеи"])

    hits = client.get("/v1/search", params={"q": "заметки"}, headers=AUTH).json()["hits"]
    assert [hit["id"] for hit in hits] == [str(note["id"])]

    assert client.patch(f"/v1/notes/{note['id']}", json={"content": ""}, headers=AUTH).status_code == 422
    assert client.delete(f"/v1/notes/{note['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/v1/notes/{note['id']}", headers=AUTH).json() == {"detail": "Note not found"}


def test_calendar_off_is_reported(client):
    response = client.get("/v1/events", headers=AUTH)
    assert response.status_code == 503
    assert "GOOGLE_CREDENTIALS_FILE" in response.json()["detail"]


def test_mcp_tools_reach_the_engine(client):
    tools = {tool["name"]: tool for tool in _mcp(client, "tools/list")["tools"]}
    assert set(tools) == {
        "list_events", "create_event", "update_event", "delete_event",
        "add_note", "update_note", "delete_note", "list_notes", "search",
    }
    assert tools["delete_event"]["annotations"]["destructiveHint"] is True

    added = _mcp(client, "tools/call", {"name": "add_note", "arguments": {"content": "Пароль от Wi-Fi на даче: в шкафу", "tags": ["дача"]}})
    assert not added["isError"]
    note_id = added["structuredContent"]["id"]

    found = _mcp(client, "tools/call", {"name": "search", "arguments": {"query": "дача wi-fi", "kind": "note"}})
    assert [hit["id"] for hit in found["structuredContent"]["hits"]] == [str(note_id)]

    failed = _mcp(client, "tools/call", {"name": "create_event", "arguments": {"title": "Тест", "start": "2030-01-01T10:00"}})
    assert failed["isError"]
    assert "Google Calendar is not configured" in failed["content"][0]["text"]

    missing = _mcp(client, "tools/call", {"name": "delete_note", "arguments": {"note_id": 999}})
    assert missing["isError"] and "Note not found" in json.dumps(missing)
