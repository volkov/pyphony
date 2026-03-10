from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyphony",
        description="Pyphony - Python Symphony Service",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Default: run service (also works without subcommand)
    run_parser = subparsers.add_parser("run", help="Run the service")
    _add_common_args(run_parser)

    # List candidates
    list_parser = subparsers.add_parser(
        "list-candidates", help="Fetch and display candidate issues for dispatch"
    )
    _add_common_args(list_parser)

    # Check issue
    check_parser = subparsers.add_parser(
        "check-issue", help="Check why a specific issue is or isn't being dispatched"
    )
    check_parser.add_argument("issue_identifier", help="Issue identifier (e.g. SER-19)")
    _add_common_args(check_parser)

    # For backward compat: if no subcommand, treat positional arg as workflow_file
    parser.add_argument(
        "workflow_file",
        nargs="?",
        default=None,
        help="Path to WORKFLOW.md (default: WORKFLOW.md)",
    )

    args = parser.parse_args(argv)

    # Default workflow_file
    if not hasattr(args, "workflow_file") or args.workflow_file is None:
        args.workflow_file = "WORKFLOW.md"

    return args


def _add_common_args(parser: argparse.ArgumentParser) -> None:
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


def main() -> None:
    args = parse_args()

    if args.command == "list-candidates":
        from .candidates import list_candidates
        list_candidates(args)
    elif args.command == "check-issue":
        from .candidates import check_issue
        check_issue(args)
    else:
        from .service import run_service
        run_service(args)
