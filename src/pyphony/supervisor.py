"""pyphony-sv: Supervisor that auto-updates and restarts pyphony.

Usage:
    pyphony-sv [WORKFLOW_FILES...] [--pull-interval SECONDS] [-- extra args for pyphony run]

    If no workflow files are given, all *.md files in the workflows/ directory
    are used automatically.

Loop:
    1. git pull --rebase
    2. uv run python -m pyphony run <workflow1> <workflow2> ... --exit-on-merge [extra args]
       (single process handles all workflows concurrently)
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
from pathlib import Path

EXIT_CODE_MERGE = 10
DEFAULT_PULL_INTERVAL = 0  # seconds between pull retries when app exits normally
DEFAULT_WORKFLOWS_DIR = "workflows"


def _discover_workflows(directory: str) -> list[str]:
    """Find all *.md workflow files in the given directory."""
    workflows_dir = Path(directory)
    if not workflows_dir.is_dir():
        print(f"[pyphony-sv] workflows directory not found: {directory}")
        return []
    files = sorted(str(p) for p in workflows_dir.glob("*.md"))
    if not files:
        print(f"[pyphony-sv] no workflow files found in {directory}")
    return files


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


def _uv_sync() -> bool:
    """Run uv sync to ensure editable install points to current directory."""
    try:
        result = subprocess.run(
            ["uv", "sync"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True
        else:
            print(f"[pyphony-sv] uv sync failed (rc={result.returncode}): {result.stderr.strip()}")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[pyphony-sv] uv sync error: {exc}")
        return False


def _run_app(workflow_files: list[str], extra_args: list[str]) -> subprocess.Popen:
    """Start a single pyphony process with all workflow files and --exit-on-merge."""
    cmd = [
        sys.executable, "-m", "pyphony",
        "run", *workflow_files,
        "--exit-on-merge",
        *extra_args,
    ]
    print(f"[pyphony-sv] starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def _parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="pyphony-sv",
        description="Pyphony supervisor with auto-update on merge",
    )
    parser.add_argument(
        "workflow_files",
        nargs="*",
        help=(
            "Paths to workflow .md files. "
            f"If omitted, all *.md files in {DEFAULT_WORKFLOWS_DIR}/ are used."
        ),
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

    # Resolve workflow files: explicit args or auto-discover from workflows/
    workflow_files = args.workflow_files
    if not workflow_files:
        workflow_files = _discover_workflows(DEFAULT_WORKFLOWS_DIR)
        if not workflow_files:
            print("[pyphony-sv] no workflows to run, exiting")
            return

    print(f"[pyphony-sv] supervisor started (workflows={workflow_files})")

    while _running:
        # Step 1: pull latest code and ensure deps are in sync
        _git_pull()
        _uv_sync()

        if not _running:
            break

        # Step 2: start a single process with all workflow files
        proc = _run_app(workflow_files, extra)

        # Step 3: wait for the process to exit
        while _running:
            ret = proc.poll()
            if ret is not None:
                break
            time.sleep(0.5)

        if not _running:
            # Terminate process on signal
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break

        if ret == EXIT_CODE_MERGE:
            print("[pyphony-sv] merge detected (exit code 10), pulling and restarting...")
            continue
        elif ret != 0:
            print(f"[pyphony-sv] process exited with code {ret}, restarting in 5s...")
            for _ in range(50):  # 5 seconds in 0.1s increments
                if not _running:
                    break
                time.sleep(0.1)
        else:
            print("[pyphony-sv] process exited cleanly, stopping supervisor")
            break

    print("[pyphony-sv] supervisor stopped")


if __name__ == "__main__":
    main()
