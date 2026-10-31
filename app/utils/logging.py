import logging
import sys

from loguru import logger


# Access-log paths to mute
MUTED_ACCESS_PATHS = ("/health", "/metrics")

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


def configure_logging(level: str = "INFO", diagnose: bool = False) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=LOG_FORMAT,
        backtrace=False,
        diagnose=diagnose,
    )

    logging.getLogger("uvicorn.access").addFilter(
        lambda record: not any(path in record.getMessage() for path in MUTED_ACCESS_PATHS)
    )

    # httpx logs every request URL at INFO, and the Telegram one carries the bot token;
    # the MCP SDK logs every stateless request as a terminated session
    for name in ("httpx", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if diagnose:
        logger.warning(
            "LOG_DIAGNOSE is on: tracebacks will include variable values, "
            "request payloads and auth tokens among them"
        )
