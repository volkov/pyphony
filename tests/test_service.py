import argparse
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pyphony.service import _run_service


class TestServiceStartup:
    def test_missing_workflow_file_exits(self):
        args = argparse.Namespace(
            workflow_file="/nonexistent/WORKFLOW.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            import asyncio
            asyncio.run(_run_service(args))
