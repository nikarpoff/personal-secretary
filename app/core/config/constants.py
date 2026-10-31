# Upstream limits and search heuristics live here, not in env: they are tuned together with the code


class CALENDAR_SETTINGS:
    """Google Calendar API v3."""
    API_URL = "https://www.googleapis.com/calendar/v3"
    SCOPES = ("https://www.googleapis.com/auth/calendar.events", )    # events only: no calendar list, no sharing
    PAGE_SIZE = 250                 # API maximum is 2500, a page this size keeps answers small
    LIST_LIMIT = 500                # events one list request may return to the agent
    TIMEOUT = 20.0                  # seconds per request
    PAST_DAYS = 1                   # the mirror window starts a day back: running events stay in it
    MAX_OVERRIDES = 5               # API limit of reminder overrides per event
    DEFAULT_DURATION_MIN = 60       # event without an end lasts an hour


class REMINDER_SETTINGS:
    """Background loop of the reminders."""
    TICK_SECONDS = 30               # reminder precision; the calendar sync runs on its own interval
    SENT_RETENTION_DAYS = 30        # sent-reminder marks are kept this long, then purged
    MAX_NOTES_SHOWN = 3             # linked notes listed under a reminder


class SEARCH_SETTINGS:
    """Hybrid search over notes and events: FTS5 BM25 + embeddings, fused by reciprocal rank."""
    RRF_K = 60                      # standard RRF constant: damps the weight of the top ranks
    CANDIDATES = 50                 # hits each ranking contributes to the fusion
    SHORT_WORD = 4                  # shorter words are matched whole
    LONG_WORD = 6                   # Russian endings vary, the stem does not: two letters cut from here, one below
    SNIPPET_CHARS = 240             # characters of a body shown in a hit
    EMBEDDING_BATCH = 64            # texts per embeddings request
    EMBEDDING_TIMEOUT = 30.0        # seconds per embeddings request


class TELEGRAM_SETTINGS:
    """Telegram Bot API."""
    API_URL = "https://api.telegram.org"
    TIMEOUT = 15.0                  # seconds per request
    MAX_MESSAGE_CHARS = 4096        # Bot API limit of one message
