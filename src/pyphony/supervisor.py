"""pyphony-sv: Supervisor that auto-updates and restarts pyphony.

Usage:
    pyphony-sv [WORKFLOW.md] [--pull-interval SECONDS] [-- extra args for pyphony run]

Loop:
    1. git pull --rebase
    2. uv run python -m pyphony run WORKFLOW.md --exit-on-merge [extra args]
    3. If process exits with code 10 (merge detected) → restart from step 1
    4. If process exits with 0 or signal → stop
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

EXIT_CODE_MERGE = 10
DEFAULT_PULL_INTERVAL = 0  # seconds between pull retries when app exits normally


def _git_pull() -> bool:
    """Run git pull --rebase. Returns True on success."""
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            stdout = result.stdout.strip()
            print(f"[pyphony-sv] git pull: {stdout}")
            return True
        else:
            print(f"[pyphony-sv] git pull failed (rc={result.returncode}): {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        print("[pyphony-sv] git pull timed out")
        return False
    except FileNotFoundError:
        print("[pyphony-sv] git not found in PATH")
        return False


def _run_app(workflow_file: str, extra_args: list[str]) -> int:
    """Run pyphony with --exit-on-merge. Returns exit code."""
    cmd = [
        sys.executable, "-m", "pyphony",
        "run", workflow_file,
        "--exit-on-merge",
        *extra_args,
    ]
    print(f"[pyphony-sv] starting: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except KeyboardInterrupt:
        return 0


def _parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="pyphony-sv",
        description="Pyphony supervisor with auto-update on merge",
    )
    parser.add_argument(
        "workflow_file",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: WORKFLOW.md)",
    )
    parser.add_argument(
        "--pull-interval",
        type=int,
        default=DEFAULT_PULL_INTERVAL,
        help="Seconds to wait before pulling after normal exit (default: 0)",
    )
    # Everything after -- is passed to pyphony run
    if argv is None:
        argv = sys.argv[1:]

    if "--" in argv:
        idx = argv.index("--")
        own_argv = argv[:idx]
        extra = argv[idx + 1:]
    else:
        own_argv = argv
        extra = []

    args = parser.parse_args(own_argv)
    return args, extra


_running = True


def _handle_signal(signum, frame):
    global _running
    print(f"\n[pyphony-sv] received signal {signum}, shutting down...")
    _running = False


def main() -> None:
    global _running

    args, extra = _parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(f"[pyphony-sv] supervisor started (workflow={args.workflow_file})")

    while _running:
        # Step 1: pull latest code
        _git_pull()

        if not _running:
            break

        # Step 2: run the app
        exit_code = _run_app(args.workflow_file, extra)

        if not _running:
            break

        if exit_code == EXIT_CODE_MERGE:
            print("[pyphony-sv] merge detected (exit code 10), pulling and restarting...")
            continue
        elif exit_code == 0:
            print("[pyphony-sv] app exited cleanly, stopping supervisor")
            break
        else:
            print(f"[pyphony-sv] app exited with code {exit_code}, restarting in 5s...")
            for _ in range(50):  # 5 seconds in 0.1s increments
                if not _running:
                    break
                time.sleep(0.1)

    print("[pyphony-sv] supervisor stopped")


if __name__ == "__main__":
    main()
