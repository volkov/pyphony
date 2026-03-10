from __future__ import annotations

import logging
import os
import sys

import structlog

DEFAULT_LOG_FILE = "logs/pyphony.log"

_INTERNAL_KEYS = frozenset({"_from_structlog", "_logger", "_name"})


def _strip_internal_keys(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    for key in _INTERNAL_KEYS:
        event_dict.pop(key, None)
    return event_dict


def configure_logging(
    level: str = "INFO", log_file: str | None = DEFAULT_LOG_FILE
) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
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

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

    format_processors = [
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        _strip_internal_keys,
    ]

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[*format_processors, structlog.dev.ConsoleRenderer()],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                *format_processors,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
        )
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
