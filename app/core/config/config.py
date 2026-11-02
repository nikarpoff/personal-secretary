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

    # 4. Logging settings
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
