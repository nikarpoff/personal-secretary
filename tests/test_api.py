import pytest

from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def client():
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("AUTH_TOKEN", TOKEN)
        for name in ("GOOGLE_CREDENTIALS_FILE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            monkeypatch.setenv(name, "")

        from app.core.config import get_settings
        get_settings.cache_clear()
        from app.main import app

        with TestClient(app) as test_client:
            yield test_client
        get_settings.cache_clear()


def test_auth_is_required(client):
    assert client.get("/health").status_code == 200
    assert client.post("/mcp/", json={}).status_code == 401


def test_calendar_off_is_reported(client):
    response = client.get("/v1/events", headers=AUTH)
    assert response.status_code == 503
    assert "GOOGLE_CREDENTIALS_FILE" in response.json()["detail"]
