from __future__ import annotations

import logging
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


class LifecycleFilter(logging.Filter):
    """Only pass log records whose structlog event name is in the whitelist."""

    def filter(self, record: logging.LogRecord) -> bool:
        event = getattr(record, "_event_dict", {}).get("event", "")
        return event in LIFECYCLE_EVENTS


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
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

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.dev.ConsoleRenderer()],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    if log_file:
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[structlog.processors.JSONRenderer()],
        )
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(LifecycleFilter())
        root.addHandler(file_handler)
