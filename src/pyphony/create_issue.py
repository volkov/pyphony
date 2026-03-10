"""CLI subcommand: create-issue."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import service_config_from_workflow
from .tracker import LinearClient
from .workflow import load_workflow


async def _create_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        result = await tracker.create_issue(
            title=args.title,
            description=args.description,
        )
        print(json.dumps(result, indent=2))
    finally:
        await tracker.close()


def create_issue(args: argparse.Namespace) -> None:
    asyncio.run(_create_issue(args))
