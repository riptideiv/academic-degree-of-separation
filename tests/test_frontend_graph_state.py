"""Run graph state regressions without browser packages or network requests."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend behavior tests")
def test_graph_state_lifecycle():
    result = subprocess.run(
        [NODE, "--test", "tests/js/graph_state.test.cjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
