from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyphony",
        description="Pyphony - Python Symphony Service",
    )
    parser.add_argument(
        "workflow_file",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: WORKFLOW.md)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP server port (overrides server.port in workflow config)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default="logs/pyphony.log",
        help="Path to log file (default: logs/pyphony.log)",
    )
    return parser.parse_args(argv)


def main() -> None:
    from .service import run_service

    args = parse_args()
    run_service(args)
