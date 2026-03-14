from __future__ import annotations

import argparse
import sys

_SUBCOMMANDS = {"run", "list-candidates", "check-issue", "create-issue", "get-issue", "update-issue", "comment-issue", "label-issue", "search-issues", "prompt-view", "work", "open-url", "install-url-scheme"}


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

    # Create issue
    create_parser = subparsers.add_parser(
        "create-issue", help="Create a new issue in Linear (Backlog state)"
    )
    create_parser.add_argument("--title", required=True, help="Issue title")
    create_parser.add_argument("--description", default=None, help="Issue description (markdown)")
    create_parser.add_argument("--state", default=None, help="Initial state (default: Backlog). Use 'Todo' to queue for immediate execution.")
    _add_common_args(create_parser)

    # Get issue
    get_parser = subparsers.add_parser(
        "get-issue", help="Get an existing issue from Linear by identifier"
    )
    get_parser.add_argument("identifier", help="Issue identifier (e.g. SER-27)")
    _add_common_args(get_parser)

    # Update issue
    update_parser = subparsers.add_parser(
        "update-issue", help="Update an existing issue in Linear"
    )
    update_parser.add_argument("identifier", help="Issue identifier (e.g. SER-27)")
    update_parser.add_argument("--title", default=None, help="New issue title")
    update_parser.add_argument("--description", default=None, help="New issue description (markdown)")
    update_parser.add_argument("--state", default=None, help="New issue state (e.g. 'In Progress', 'Done')")
    _add_common_args(update_parser)

    # Comment on issue
    comment_parser = subparsers.add_parser(
        "comment-issue", help="Add a comment to an existing Linear issue"
    )
    comment_parser.add_argument("identifier", help="Issue identifier (e.g. SER-27)")
    comment_parser.add_argument("--body", required=True, help="Comment body (markdown)")
    comment_parser.add_argument("--parent-id", default=None, help="Parent comment ID for threaded replies")
    _add_common_args(comment_parser)

    # Label issue
    label_parser = subparsers.add_parser(
        "label-issue", help="Add or remove labels on a Linear issue"
    )
    label_parser.add_argument("identifier", help="Issue identifier (e.g. SER-27)")
    label_parser.add_argument("--add", action="append", default=None, help="Label to add (can be repeated)")
    label_parser.add_argument("--remove", action="append", default=None, help="Label to remove (can be repeated)")
    _add_common_args(label_parser)

    # Search issues
    search_parser = subparsers.add_parser(
        "search-issues", help="List project issues, optionally filtered by state"
    )
    search_parser.add_argument("--state", default=None, help="Comma-separated states to filter (e.g. 'Backlog,Todo')")
    _add_common_args(search_parser)

    # Prompt view
    prompt_view_parser = subparsers.add_parser(
        "prompt-view", help="Show the rendered prompt for a given issue"
    )
    prompt_view_parser.add_argument("issue_identifier", help="Issue identifier (e.g. SER-42)")
    _add_common_args(prompt_view_parser)

    # Work — interactive agent session
    work_parser = subparsers.add_parser(
        "work", help="Start an interactive Claude session for a Linear issue"
    )
    work_parser.add_argument("issue_identifier", help="Issue identifier (e.g. SER-11)")
    work_parser.add_argument(
        "--main",
        action="store_true",
        default=False,
        help="Work directly in the main repo (~/context) instead of a worktree. "
        "Requires clean working copy on the main branch.",
    )
    _add_common_args(work_parser)

    # open-url — handle pyphony:// URL scheme
    open_url_parser = subparsers.add_parser(
        "open-url", help="Handle a pyphony:// URL (opens iTerm2 tab with work session)"
    )
    open_url_parser.add_argument("url", help="pyphony:// URL to handle")

    # install-url-scheme — register pyphony:// URL scheme on macOS
    subparsers.add_parser(
        "install-url-scheme",
        help="Install macOS app bundle to handle pyphony:// URLs",
    )

    # Backward compat: if first arg is not a known subcommand, insert "run"
    # so that e.g. `pyphony my_workflow.md` or `pyphony --port 8080` works
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in _SUBCOMMANDS:
        argv = ["run"] + argv

    args = parser.parse_args(argv)

    # Default workflow_files
    if not hasattr(args, "workflow_files") or not args.workflow_files:
        args.workflow_files = ["WORKFLOW.md"]

    # Backward compat: expose first workflow as workflow_file for subcommands
    args.workflow_file = args.workflow_files[0]

    return args


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "workflow_files",
        nargs="*",
        default=None,
        help="Paths to WORKFLOW.md files (default: WORKFLOW.md)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP server port (overrides server.port in workflow config)",
    )
    parser.add_argument(
        "--exit-on-merge",
        action="store_true",
        default=False,
        help="Exit with code 10 when an issue is marked Done (for supervisor restart)",
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
    parser.add_argument(
        "--pyphony-slug",
        default=None,
        help="Project slug used for creating issues (bug reports, etc.). "
        "Overrides tracker.project_slug from WORKFLOW.md for issue creation.",
    )


def main() -> None:
    args = parse_args()

    if args.command == "list-candidates":
        from .candidates import list_candidates
        list_candidates(args)
    elif args.command == "check-issue":
        from .candidates import check_issue
        check_issue(args)
    elif args.command == "create-issue":
        from .create_issue import create_issue
        create_issue(args)
    elif args.command == "get-issue":
        from .issue_commands import get_issue
        get_issue(args)
    elif args.command == "update-issue":
        from .issue_commands import update_issue
        update_issue(args)
    elif args.command == "comment-issue":
        from .issue_commands import comment_issue
        comment_issue(args)
    elif args.command == "label-issue":
        from .issue_commands import label_issue
        label_issue(args)
    elif args.command == "search-issues":
        from .issue_commands import search_issues
        search_issues(args)
    elif args.command == "prompt-view":
        from .prompt_view import prompt_view
        prompt_view(args)
    elif args.command == "work":
        from .work import work
        work(args)
    elif args.command == "open-url":
        from .url_handler import handle_url
        handle_url(args.url)
    elif args.command == "install-url-scheme":
        from .url_handler import install_url_scheme
        install_url_scheme()
    else:
        from .service import run_service
        run_service(args)
