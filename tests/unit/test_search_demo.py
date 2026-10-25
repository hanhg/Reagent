"""Agent search demo (fixtures/search/scenario.yaml) as a test.

Runs `python -m runtime.api.search_demo` in its own process: the real runtime
app on a free port, the files catalog, no database, no credits. Every act's
PASS/FAIL line must pass.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

pytest.importorskip("langchain_mcp_adapters")
pytest.importorskip("uvicorn")

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_search_demo_scenario():
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "OPENROUTER_API_KEY")}
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, "-m", "runtime.api.search_demo"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    fails = re.findall(r"^  FAIL  (.+?)(?:  \(.*\))?$", p.stdout, re.M)
    assert re.search(r"^\d+/\d+ passed", p.stdout, re.M), p.stdout[-3000:] + p.stderr[-3000:]
    assert not fails and p.returncode == 0, f"failed: {fails}\n{p.stdout}"
    assert len(re.findall(r"^  PASS  ", p.stdout, re.M)) >= 20, p.stdout
