# Upstream limits and reminder timing live here, not in env: they are tuned together with the code


class CALENDAR_SETTINGS:
    """Google Calendar API v3."""
    API_URL = "https://www.googleapis.com/calendar/v3"
    SCOPES = ("https://www.googleapis.com/auth/calendar.events", )    # events only: no calendar list, no sharing
    PAGE_SIZE = 250                 # API maximum is 2500, a page this size keeps answers small
    TIMEOUT = 20.0                  # seconds per request
    PAST_DAYS = 1                   # the reminder window starts a day back: running events stay in it
    MAX_OVERRIDES = 5               # API limit of reminder overrides per event
    DEFAULT_DURATION_MIN = 60       # event without an end lasts an hour


class REMINDER_SETTINGS:
    """Background loop of the reminders."""
    TICK_SECONDS = 30               # reminder precision; the calendar sync runs on its own interval
    SENT_RETENTION_DAYS = 30        # sent-reminder marks are kept this long, then purged
    LATE_MINUTES = 5                # a just started event still gets its reminder: the zero-minute one fires at the start


class TELEGRAM_SETTINGS:
    """Telegram Bot API."""
    API_URL = "https://api.telegram.org"
    TIMEOUT = 15.0                  # seconds per request
    MAX_MESSAGE_CHARS = 4096        # Bot API limit of one message
