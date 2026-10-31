import httpx

from loguru import logger

from app.core.config import TELEGRAM_SETTINGS as CFG
from app.core.exceptions import UpstreamError, UpstreamTimeoutError, UpstreamUnavailableError

SERVICE = "telegram"


class TelegramClient:
    """Sends messages from the bot to the owner; the bot token never reaches the logs"""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self._chat_id = chat_id
        # The token is a part of every Bot API path
        self._http = httpx.AsyncClient(base_url=f"{CFG.API_URL}/bot{bot_token}", timeout=CFG.TIMEOUT)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send_message(self, text: str) -> None:
        """HTML message to the owner; raises on failure so the caller can retry it."""
        payload = {
            "chat_id": self._chat_id,
            "text": text[:CFG.MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        try:
            response = await self._http.post("/sendMessage", json=payload)
        except httpx.TimeoutException as e:
            raise UpstreamTimeoutError(f"No answer in {CFG.TIMEOUT}s", service=SERVICE) from e
        except httpx.TransportError as e:
            # The exception text is safe: httpx does not put the URL into it
            raise UpstreamUnavailableError(f"Unreachable: {type(e).__name__}", service=SERVICE) from e

        if not response.is_success:
            try:
                reason = response.json().get("description", "")
            except ValueError:
                reason = response.text[:200]
            raise UpstreamError(f"HTTP {response.status_code}: {reason}", service=SERVICE)

        logger.debug("Telegram message sent: {} chars", len(text))
