"""CLI subcommand: prompt-view."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import service_config_from_workflow
from .prompt import render_prompt
from .tracker import LinearClient
from .workflow import load_workflow


async def _prompt_view(args: argparse.Namespace) -> None:
    identifier = args.issue_identifier.upper()

    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        issue = await tracker.fetch_issue_by_identifier(identifier)
        comments = await tracker.fetch_issue_comments(issue.id)

        rendered = render_prompt(
            wf.prompt_template,
            issue,
            attempt=1,
            comments=comments or None,
        )

        print(rendered)
    finally:
        await tracker.close()


def prompt_view(args: argparse.Namespace) -> None:
    asyncio.run(_prompt_view(args))
