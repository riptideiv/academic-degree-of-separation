"""Shared-map validation and restoration regressions, without network access."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


@pytest.mark.skipif(NODE is None, reason='Node.js is required for map sharing tests')
def test_shared_map_links_and_restoration():
    result = subprocess.run(
        [NODE, '--test', 'tests/js/graph_links.test.cjs', 'tests/js/shared_map_lifecycle.test.cjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
