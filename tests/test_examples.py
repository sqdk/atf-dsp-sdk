"""Smoke-test that every example script runs offline (memory-backed simulated device).

Guards the README's runnable examples against API drift. Each runs with ATF_DSP_SDK_HW
unset, so `_demo.demo_device()` returns the simulated MATCH M 5.4DSP — no hardware.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ["tune_output", "read_state", "routing", "analyzer_sweep", "apply_pct6"]


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_runs_offline(name: str):
    script = ROOT / "examples" / f"{name}.py"
    assert script.is_file(), script
    env = {k: v for k, v in os.environ.items() if k != "ATF_DSP_SDK_HW"}
    proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"{name} failed:\n{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip(), f"{name} produced no output"
