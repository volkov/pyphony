from __future__ import annotations

import logging
import os
import sys

import structlog

LIFECYCLE_EVENTS = {
    "dispatch",
    "agent_start",
    "agent_finish",
    "agent_exit",
    "retry_scheduled",
    "retry_issue_not_found",
    "retry_issue_no_longer_eligible",
    "stall_detected",
    "worker_failed",
    "starting_service",
    "shutdown_requested",
    "service_stopped",
}

DEFAULT_LOG_FILE = "logs/lifecycle.log"

_INTERNAL_KEYS = frozenset({"_from_structlog", "_record", "_logger", "_name"})


class LifecycleFilter(logging.Filter):
    """Only pass log records whose structlog event name is in the whitelist."""

    def filter(self, record: logging.LogRecord) -> bool:
        event = getattr(record, "_event_dict", {}).get("event", "")
        return event in LIFECYCLE_EVENTS


def _strip_internal_keys(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    for key in _INTERNAL_KEYS:
        event_dict.pop(key, None)
    return event_dict


def configure_logging(
    level: str = "INFO", log_file: str | None = DEFAULT_LOG_FILE
) -> None:
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Suppress noisy third-party loggers on console
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ExtraAdder(),
            _strip_internal_keys,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ExtraAdder(),
                _strip_internal_keys,
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(LifecycleFilter())
        root.addHandler(file_handler)
