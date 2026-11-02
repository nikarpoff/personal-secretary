# Upstream limits and reminder timing constants.


class CALENDAR_SETTINGS:
    """Google Calendar API v3."""
    API_URL = "https://www.googleapis.com/calendar/v3"
    SCOPES = ("https://www.googleapis.com/auth/calendar.events", )    # events only: no calendar list, no sharing
    PAGE_SIZE = 250                 # API maximum is 2500, a page this size keeps answers small
    LIST_LIMIT = 500                # events one list request may return to the agent
    TIMEOUT = 20.0                  # seconds per request
    PAST_DAYS = 1                   # include ongoing events in calendar reads
    MAX_OVERRIDES = 5               # API limit of reminder overrides per event
    DEFAULT_DURATION_MIN = 60       # event without an end lasts an hour


class REMINDER_SETTINGS:
    """Background loop of the reminders."""
    TICK_SECONDS = 30               # reminder precision; the calendar sync runs on its own interval


class TELEGRAM_SETTINGS:
    """Telegram Bot API."""
    API_URL = "https://api.telegram.org"
    TIMEOUT = 15.0                  # seconds per request
    MAX_MESSAGE_CHARS = 4096        # Bot API limit of one message
