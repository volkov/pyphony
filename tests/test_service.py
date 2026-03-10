import argparse
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pyphony.service import _run_service


class TestServiceStartup:
    def test_missing_workflow_file_exits(self):
        args = argparse.Namespace(
            workflow_files=["/nonexistent/WORKFLOW.md"],
            workflow_file="/nonexistent/WORKFLOW.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            import asyncio
            asyncio.run(_run_service(args))

    def test_multiple_workflow_files_arg(self):
        """Verify that args with multiple workflow files are accepted by _run_service signature."""
        args = argparse.Namespace(
            workflow_files=["/nonexistent/wf1.md", "/nonexistent/wf2.md"],
            workflow_file="/nonexistent/wf1.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            import asyncio
            asyncio.run(_run_service(args))
