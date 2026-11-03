import json

import pytest

from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        # Away from the project root: its .env must not leak into the tests
        monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
        monkeypatch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
        monkeypatch.setenv("AUTH_TOKEN", TOKEN)
        for name in ("GOOGLE_CREDENTIALS_FILE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
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
    assert client.get("/v1/events").status_code == 401
    assert client.get("/v1/events", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.post("/mcp/", json={}).status_code == 401


def test_calendar_off_is_reported(client):
    response = client.get("/v1/events", headers=AUTH)
    assert response.status_code == 503
    assert "GOOGLE_CREDENTIALS_FILE" in response.json()["detail"]


def test_mcp_tools_reach_the_engine(client):
    tools = {tool["name"]: tool for tool in _mcp(client, "tools/list")["tools"]}
    assert set(tools) == {"list_events", "create_event", "update_event", "delete_event"}
    assert tools["delete_event"]["annotations"]["destructiveHint"] is True
    assert tools["list_events"]["annotations"]["readOnlyHint"] is True

    failed = _mcp(client, "tools/call", {"name": "create_event", "arguments": {"title": "Тест", "start": "2030-01-01T10:00"}})
    assert failed["isError"]
    assert "Google Calendar is not configured" in failed["content"][0]["text"]

    invalid = _mcp(client, "tools/call", {"name": "create_event", "arguments": {"title": "", "start": "2030-01-01T10:00"}})
    assert invalid["isError"] and "title" in json.dumps(invalid)


def test_reminder_minutes_are_checked_by_the_contract(client):
    response = client.post("/v1/events", json={"title": "x", "start": "2030-01-01T10:00", "remind_minutes": [-5]}, headers=AUTH)
    assert response.status_code == 422
