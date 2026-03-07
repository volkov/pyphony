from pathlib import Path

import pytest

from pyphony.errors import MissingWorkflowFile, WorkflowFrontMatterNotAMap, WorkflowParseError
from pyphony.workflow import load_workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"


class TestLoadWorkflow:
    def test_valid_workflow(self):
        wf = load_workflow(FIXTURES / "valid_workflow.md")
        assert wf.config["tracker"]["kind"] == "linear"
        assert wf.config["polling"]["interval_ms"] == 15000
        assert "{{ issue.identifier }}" in wf.prompt_template

    def test_no_front_matter(self):
        wf = load_workflow(FIXTURES / "no_frontmatter.md")
        assert wf.config == {}
        assert "Just a plain prompt" in wf.prompt_template

    def test_non_map_front_matter(self):
        with pytest.raises(WorkflowFrontMatterNotAMap):
            load_workflow(FIXTURES / "non_map_frontmatter.md")

    def test_missing_file(self):
        with pytest.raises(MissingWorkflowFile):
            load_workflow("/nonexistent/path/WORKFLOW.md")


class TestParseWorkflow:
    def test_empty_front_matter(self):
        wf = parse_workflow("---\n---\nHello")
        assert wf.config == {}
        assert wf.prompt_template == "Hello"

    def test_prompt_trimmed(self):
        wf = parse_workflow("---\nkey: val\n---\n\n  Hello  \n\n")
        assert wf.prompt_template == "Hello"

    def test_no_closing_delimiter(self):
        wf = parse_workflow("---\nkey: val\nNo closing")
        assert wf.config == {}
        assert "---" in wf.prompt_template

    def test_invalid_yaml(self):
        with pytest.raises(WorkflowParseError):
            parse_workflow("---\n: invalid: yaml: [[\n---\nbody")
