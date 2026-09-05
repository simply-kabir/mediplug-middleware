import logging
import sys

import structlog

from .config import settings


def configure() -> None:
    """Call once at process startup (gateway main.py and worker __main__.py)."""
    logging.basicConfig(stream=sys.stdout, level=settings.log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if settings.environment == "development"
            else structlog.processors.JSONRenderer(),
        ],
    )


log = structlog.get_logger()
