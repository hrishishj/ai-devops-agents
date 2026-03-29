"""
Structured logging configuration.

Configures structlog for JSON output in production and
pretty console output in development.

Usage
-----
    from config.logging_config import configure_logging
    configure_logging()          # call once at startup
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys


def configure_logging(log_level: str | None = None) -> None:
    """
    Set up structlog + stdlib logging.

    Parameters
    ----------
    log_level : str, optional
        Override log level. Defaults to LOG_LEVEL env var or "INFO".
    """
    level = log_level or os.getenv("LOG_LEVEL", "INFO").upper()
    is_production = os.getenv("ENVIRONMENT", "development").lower() == "production"

    # ------------------------------------------------------------------ #
    # stdlib logging — picked up by uvicorn and azure SDKs
    # ------------------------------------------------------------------ #
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                    "fmt": "%(asctime)s %(name)s %(levelname)s %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                },
                "console": {
                    "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    "datefmt": "%H:%M:%S",
                },
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "json" if is_production else "console",
                },
            },
            "root": {
                "level": level,
                "handlers": ["stdout"],
            },
            # Silence noisy third-party loggers
            "loggers": {
                "azure": {"level": "WARNING", "propagate": True},
                "urllib3": {"level": "WARNING", "propagate": True},
                "httpx": {"level": "WARNING", "propagate": True},
                "openai": {"level": "WARNING", "propagate": True},
            },
        }
    )

    try:
        import structlog

        shared_processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
        ]

        if is_production:
            processors = shared_processors + [
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ]
        else:
            processors = shared_processors + [
                structlog.dev.ConsoleRenderer(colors=True),
            ]

        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.getLevelName(level)
            ),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    except ImportError:
        # structlog is optional; stdlib logging is still configured above
        pass
