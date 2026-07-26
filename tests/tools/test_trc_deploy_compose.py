"""Runs the TRC staging deploy contract checks under pytest.

The checks themselves live in deploy/trc/validate_compose.py so that CI can
run them without a Python test environment. This wrapper exists so a local
`pytest` run catches a broken compose file too -- the failure it guards
against (a project-prefixed empty volume) produces no error at deploy time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "deploy" / "trc" / "validate_compose.py"


def test_validator_script_exists() -> None:
    assert VALIDATOR.is_file(), f"{VALIDATOR} is missing"


def test_trc_deploy_contract_checks_pass() -> None:
    if subprocess.run(
        ["docker", "version"], capture_output=True, check=False
    ).returncode != 0:
        pytest.skip("docker unavailable; `docker compose config` cannot run")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, (
        f"validate_compose.py failed:\n{proc.stdout}\n{proc.stderr}"
    )
