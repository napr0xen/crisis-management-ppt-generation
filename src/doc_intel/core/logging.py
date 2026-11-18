"""JSON logs via structlog. Never log document text, prompts, filenames or secrets."""

import logging
import sys

import structlog

FORBIDDEN_KEYS = frozenset(
    {"text", "quote", "prompt", "completion", "content", "filename", "authorization"}
)


def redact_forbidden(_logger, _method, event_dict: dict) -> dict:
    for key in FORBIDDEN_KEYS & event_dict.keys():
        del event_dict[key]
    return event_dict


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", key="ts"),
            redact_forbidden,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )
