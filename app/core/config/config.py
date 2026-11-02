from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # 1. Backend settings
    AUTH_TOKEN: Optional[str] = Field(None, description="Bearer token of the REST API and the MCP endpoint; auth is off when unset")
    TIMEZONE: str = Field("Europe/Moscow", description="IANA timezone of the owner: times without an offset are read in it")

    # 2. Google Calendar
    GOOGLE_CREDENTIALS_FILE: Optional[Path] = Field(None, description="authorized_user (scripts/authorize_google.py) or service_account JSON; calendar tools are off when unset")
    GOOGLE_CALENDAR_ID: str = Field("primary", description="Calendar the assistant works with; a service account needs the owner's e-mail here")
    CALENDAR_SYNC_INTERVAL: int = Field(60, ge=10, description="Seconds between reads of Google Calendar for reminders")
    CALENDAR_SYNC_DAYS: int = Field(30, ge=2, description="Days ahead to fetch from Google Calendar for reminders")
    IMPORTANT_COLOR_ID: str = Field("11", description="Google event color that marks an important event; 11 is Tomato (red)")

    # 3. Telegram reminders
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(None, description="Token from @BotFather, the same bot OpenClaw serves; reminders only go to the log when unset")
    TELEGRAM_CHAT_ID: Optional[int] = Field(None, description="Owner's numeric Telegram user id: reminders are sent there")
    REMINDER_MINUTES: Annotated[List[int], NoDecode] = Field([15], description="Minutes before start for events with default Google notifications, comma-separated")
    IMPORTANT_REMINDER_MINUTES: Annotated[List[int], NoDecode] = Field([1440, 120], description="Extra reminders of important events, comma-separated")

    # 4. Obsidian: only the Secretary subtree is mounted here, never the full vault.
    NOTES_ROOT: Optional[Path] = Field(None, description="Secretary-only bind mount; notes are disabled when unset")
    ASSISTANT_STATE_DIR: Optional[Path] = Field(None, description="Private persistent directory for task delivery marks and conflicts")
    OBSIDIAN_VAULT_NAME: str = Field("Obsidian", min_length=1, max_length=100, description="Same vault/base directory name on WebDAV, Windows and Android")
    NOTE_REMINDER_POLL_SECONDS: int = Field(30, ge=10, le=300, description="Task reminder polling interval, independent of Google Calendar")

    # 5. Logging settings
    LOG_LEVEL: Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"] = Field("INFO", description="Minimum level a message needs to be logged")
    LOG_DIAGNOSE: bool = Field(False, description="Add variable values to logged tracebacks; debug only - request payloads and auth tokens end up in the logs")

    @field_validator("REMINDER_MINUTES", "IMPORTANT_REMINDER_MINUTES", mode="before")
    @classmethod
    def _split_minutes(cls, value: Any) -> Any:
        # "60, 15" from env rather than JSON
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        return value

    @model_validator(mode="after")
    def _validate_related_settings(self) -> Self:
        try:
            ZoneInfo(self.TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"TIMEZONE ({self.TIMEZONE}) is not an IANA timezone, e.g. Europe/Moscow") from None

        if (self.TELEGRAM_BOT_TOKEN is None) != (self.TELEGRAM_CHAT_ID is None):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set together")

        if (self.NOTES_ROOT is None) != (self.ASSISTANT_STATE_DIR is None):
            raise ValueError("NOTES_ROOT and ASSISTANT_STATE_DIR are set together")
        if self.NOTES_ROOT is not None:
            if not self.AUTH_TOKEN or not self.AUTH_TOKEN.strip() or self.AUTH_TOKEN.strip() in {"<change_me>", "change_me"}:
                raise ValueError("Notes require a non-placeholder AUTH_TOKEN")
            if not self.NOTES_ROOT.is_absolute() or not self.ASSISTANT_STATE_DIR.is_absolute():
                raise ValueError("NOTES_ROOT and ASSISTANT_STATE_DIR must be absolute paths")
            notes_path = self.NOTES_ROOT.resolve()
            state_path = self.ASSISTANT_STATE_DIR.resolve()
            if notes_path == state_path or notes_path in state_path.parents or state_path in notes_path.parents:
                raise ValueError("Notes and private state directories must be separate")
        if self.OBSIDIAN_VAULT_NAME.strip() != self.OBSIDIAN_VAULT_NAME or any(
            char in self.OBSIDIAN_VAULT_NAME for char in '/\\\x00\r\n'
        ) or self.OBSIDIAN_VAULT_NAME in {".", ".."}:
            raise ValueError("OBSIDIAN_VAULT_NAME must be a single nonempty directory name")

        minutes = self.REMINDER_MINUTES + self.IMPORTANT_REMINDER_MINUTES
        if any(m < 0 for m in minutes):
            raise ValueError("REMINDER_MINUTES and IMPORTANT_REMINDER_MINUTES must not be negative")
        if minutes and max(minutes) >= self.CALENDAR_SYNC_DAYS * 24 * 60:
            raise ValueError(
                f"CALENDAR_SYNC_DAYS ({self.CALENDAR_SYNC_DAYS}) must cover the earliest reminder ({max(minutes)} min)"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def calendar_enabled(self) -> bool:
        return self.GOOGLE_CREDENTIALS_FILE is not None

    @property
    def notes_enabled(self) -> bool:
        return self.NOTES_ROOT is not None

    # An empty variable keeps the default
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_settings_or_exit() -> Settings:
    """Settings of the app; a bad configuration is logged line by line and stops the process."""
    try:
        return get_settings()
    except ValidationError as e:
        # Messages only: the input values may be secrets
        for error in e.errors():
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error(f"Bad configuration: {name}: {error['msg']}")
        raise SystemExit(1) from None
