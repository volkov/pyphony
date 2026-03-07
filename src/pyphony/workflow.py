from __future__ import annotations

from pathlib import Path

import yaml

from .errors import MissingWorkflowFile, WorkflowFrontMatterNotAMap, WorkflowParseError
from .models import WorkflowDefinition


def load_workflow(path: str | Path) -> WorkflowDefinition:
    path = Path(path)
    if not path.is_file():
        raise MissingWorkflowFile(f"Workflow file not found: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise MissingWorkflowFile(f"Cannot read workflow file: {e}") from e

    return parse_workflow(text)


def parse_workflow(text: str) -> WorkflowDefinition:
    config: dict = {}
    prompt_body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front_matter_raw = parts[1]
            prompt_body = parts[2]

            try:
                parsed = yaml.safe_load(front_matter_raw)
            except yaml.YAMLError as e:
                raise WorkflowParseError(f"Invalid YAML front matter: {e}") from e

            if parsed is None:
                config = {}
            elif not isinstance(parsed, dict):
                raise WorkflowFrontMatterNotAMap(
                    f"YAML front matter must be a map, got {type(parsed).__name__}"
                )
            else:
                config = parsed

    return WorkflowDefinition(
        config=config,
        prompt_template=prompt_body.strip(),
    )
