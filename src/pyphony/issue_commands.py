"""CLI subcommands: get-issue, update-issue."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import service_config_from_workflow
from .tracker import LinearClient
from .workflow import load_workflow


async def _get_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        result = await tracker.get_issue(identifier=args.identifier)
        print(json.dumps(result, indent=2))
    finally:
        await tracker.close()


def get_issue(args: argparse.Namespace) -> None:
    asyncio.run(_get_issue(args))


async def _update_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        result = await tracker.update_issue(
            identifier=args.identifier,
            title=args.title,
            description=args.description,
            state=args.state,
        )
        print(json.dumps(result, indent=2))
    finally:
        await tracker.close()


def update_issue(args: argparse.Namespace) -> None:
    asyncio.run(_update_issue(args))
