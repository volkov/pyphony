"""CLI subcommands: list-candidates, check-issue."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import service_config_from_workflow
from .normalization import normalize_state, sort_issues_for_dispatch
from .tracker import LinearClient
from .workflow import load_workflow


ISSUE_BY_IDENTIFIER_QUERY = """
query IssueByIdentifier($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      project { slugId name }
      labels { nodes { name } }
      relations(first: 100) {
        nodes {
          type
          relatedIssue {
            id
            identifier
            title
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""

ALL_STATES_QUERY = """
query AllProjectIssues($projectSlug: String!, $first: Int!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
    }
    first: $first
    after: $after
  ) {
    nodes {
      identifier
      title
      state { name }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


async def _list_candidates(args: argparse.Namespace) -> None:
    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    print(f"Project slug: {config.tracker.project_slug}")
    print(f"Active states: {config.tracker.active_states}")
    print(f"Terminal states: {config.tracker.terminal_states}")
    print()

    tracker = LinearClient(config)
    try:
        # First: show ALL issues in the project regardless of state
        print("=== All issues in project ===")
        data = await tracker._execute(ALL_STATES_QUERY, {
            "projectSlug": config.tracker.project_slug,
            "first": 50,
        })
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            print("  (none)")
        for node in nodes:
            print(f"  {node['identifier']}: {node['title']}  [state: {node['state']['name']}]")
        print()

        # Then: show filtered candidates
        print("=== Candidate issues (filtered by active_states) ===")
        issues = await tracker.fetch_candidate_issues()
        if not issues:
            print("  (none)")
        else:
            sorted_issues = sort_issues_for_dispatch(issues)
            active = {normalize_state(s) for s in config.tracker.active_states}
            terminal = {normalize_state(s) for s in config.tracker.terminal_states}

            for issue in sorted_issues:
                issue_state = normalize_state(issue.state)
                reasons = []
                if issue_state == "todo":
                    for blocker in issue.blocked_by:
                        if blocker.state and normalize_state(blocker.state) not in terminal:
                            reasons.append(
                                f"blocked by {blocker.identifier} (state: {blocker.state})"
                            )

                status = "ELIGIBLE" if not reasons else "BLOCKED"
                print(f"  {issue.identifier}: {issue.title}")
                print(f"    state={issue.state}  priority={issue.priority}  [{status}]")
                if issue.blocked_by:
                    for b in issue.blocked_by:
                        print(f"    blocker: {b.identifier} (state: {b.state})")
                if reasons:
                    for r in reasons:
                        print(f"    reason: {r}")
                print()
    finally:
        await tracker.close()


def list_candidates(args: argparse.Namespace) -> None:
    asyncio.run(_list_candidates(args))


async def _check_issue(args: argparse.Namespace) -> None:
    identifier = args.issue_identifier.upper()

    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    active = {normalize_state(s) for s in config.tracker.active_states}
    terminal = {normalize_state(s) for s in config.tracker.terminal_states}

    tracker = LinearClient(config)
    try:
        # Parse identifier like "SER-19" -> team prefix "SER", number 19
        parts = identifier.rsplit("-", 1)
        if len(parts) != 2:
            print(f"Invalid identifier format: {identifier} (expected e.g. SER-19)")
            return
        team_prefix, number_str = parts
        try:
            number = int(number_str)
        except ValueError:
            print(f"Invalid identifier format: {identifier}")
            return

        data = await tracker._execute(
            ISSUE_BY_IDENTIFIER_QUERY,
            {
                "filter": {
                    "team": {"key": {"eq": team_prefix}},
                    "number": {"eq": number},
                },
                "first": 1,
            },
        )
        nodes = data.get("issues", {}).get("nodes", [])

        if not nodes:
            print(f"Issue {identifier} not found in Linear.")
            return

        issue_node = nodes[0]
        state = issue_node["state"]["name"]
        project = issue_node.get("project")
        project_slug = project.get("slugId") if project else None
        project_name = project.get("name") if project else None

        print(f"=== {issue_node['identifier']}: {issue_node['title']} ===")
        print(f"  State:    {state}")
        print(f"  Priority: {issue_node.get('priority')}")
        print(f"  Project:  {project_name} (slug: {project_slug})")
        print(f"  URL:      {issue_node.get('url')}")
        print()

        # Check: is it in the right project?
        if project_slug != config.tracker.project_slug:
            print(f"  PROBLEM: Issue is in project '{project_slug}', "
                  f"but service watches '{config.tracker.project_slug}'")
            print(f"  FIX: Add this issue to project with slug '{config.tracker.project_slug}'")
            print()
            return

        norm_state = normalize_state(state)

        # Check: is the state active?
        if norm_state in terminal:
            print(f"  VERDICT: Issue is in terminal state '{state}' -> will be IGNORED")
            print()
            return

        if norm_state not in active:
            print(f"  PROBLEM: State '{state}' is not in active_states {config.tracker.active_states}")
            print(f"  FIX: Either move the issue to one of {config.tracker.active_states}, "
                  f"or add '{state}' to tracker.active_states in WORKFLOW.md")
            print()
            return

        # Check: blockers
        blockers = []
        for rel in (issue_node.get("relations", {}) or {}).get("nodes", []):
            if rel.get("type") == "blocks":
                related = rel.get("relatedIssue", {})
                rel_state = (related.get("state") or {}).get("name")
                blockers.append((related.get("identifier"), related.get("title"), rel_state))

        if blockers and norm_state == "todo":
            unresolved = [
                (ident, title, st)
                for ident, title, st in blockers
                if st and normalize_state(st) not in terminal
            ]
            if unresolved:
                print(f"  PROBLEM: Issue has unresolved blockers:")
                for ident, title, st in unresolved:
                    print(f"    - {ident}: {title} [state: {st}]")
                print(f"  FIX: Resolve blocking issues first (move them to a terminal state)")
                print()
                return
            else:
                print(f"  Blockers (all resolved):")
                for ident, title, st in blockers:
                    print(f"    - {ident}: {title} [state: {st}]")

        print(f"  VERDICT: Issue SHOULD be dispatched (state '{state}' is active, no blockers)")
        print()
    finally:
        await tracker.close()


def check_issue(args: argparse.Namespace) -> None:
    asyncio.run(_check_issue(args))
