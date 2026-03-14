"""CLI subcommands: get-issue, update-issue, comment-issue, label-issue, search-issues."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from .config import service_config_from_workflow
from .tracker import LinearClient
from .workflow import load_workflow


# ---------------------------------------------------------------------------
# get-issue
# ---------------------------------------------------------------------------

async def _get_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        result = await tracker.get_issue(identifier=args.identifier)
        comments = await tracker.fetch_issue_comments(result["id"])
        if comments:
            result["comments"] = comments
        print(yaml.dump(result, allow_unicode=True, default_flow_style=False, sort_keys=False))
    finally:
        await tracker.close()


def get_issue(args: argparse.Namespace) -> None:
    asyncio.run(_get_issue(args))


# ---------------------------------------------------------------------------
# update-issue
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# comment-issue
# ---------------------------------------------------------------------------

async def _comment_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        # Resolve issue internal ID from identifier
        issue_data = await tracker.get_issue(identifier=args.identifier)
        issue_id = issue_data["id"]

        comment_id = await tracker.comment_on_issue(
            issue_id=issue_id,
            body=args.body,
            parent_comment_id=getattr(args, "parent_id", None),
        )
        if comment_id:
            print(json.dumps({
                "success": True,
                "comment_id": comment_id,
                "issue": args.identifier,
            }, indent=2))
        else:
            print(json.dumps({"success": False, "error": "Failed to create comment"}, indent=2))
    finally:
        await tracker.close()


def comment_issue(args: argparse.Namespace) -> None:
    asyncio.run(_comment_issue(args))


# ---------------------------------------------------------------------------
# label-issue
# ---------------------------------------------------------------------------

async def _label_issue(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        # Resolve issue internal ID from identifier
        issue_data = await tracker.get_issue(identifier=args.identifier)
        issue_id = issue_data["id"]

        add_labels = args.add or []
        remove_labels = args.remove or []

        success = await tracker.replace_issue_labels(
            issue_id=issue_id,
            remove_labels=remove_labels,
            add_labels=add_labels,
        )
        print(json.dumps({
            "success": success,
            "issue": args.identifier,
            "added": add_labels,
            "removed": remove_labels,
        }, indent=2))
    finally:
        await tracker.close()


def label_issue(args: argparse.Namespace) -> None:
    asyncio.run(_label_issue(args))


# ---------------------------------------------------------------------------
# search-issues
# ---------------------------------------------------------------------------

async def _search_issues(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        states = [s.strip() for s in args.state.split(",")] if args.state else None
        if states:
            issues = await tracker.fetch_issues_by_states(states)
        else:
            # Default: fetch all active + backlog issues
            all_states = list(tracker._active_states) + ["Backlog"]
            issues = await tracker.fetch_issues_by_states(all_states)

        rows = []
        for issue in issues:
            row = {
                "identifier": issue.identifier,
                "title": issue.title,
                "state": issue.state,
                "labels": issue.labels,
            }
            if issue.assignee:
                row["assignee"] = issue.assignee
            rows.append(row)

        print(yaml.dump(rows, allow_unicode=True, default_flow_style=False, sort_keys=False))
    finally:
        await tracker.close()


def search_issues(args: argparse.Namespace) -> None:
    asyncio.run(_search_issues(args))
