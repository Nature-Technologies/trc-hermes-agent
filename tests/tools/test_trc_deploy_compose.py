"""Runs the TRC staging deploy contract checks under pytest.

The checks themselves live in deploy/trc/validate_compose.py so that CI can
run them without a Python test environment. This wrapper exists so a local
`pytest` run catches a broken compose file too -- the failure it guards
against (a project-prefixed empty volume) produces no error at deploy time.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "deploy" / "trc" / "validate_compose.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "trc-staging-deploy.yml"


def test_validator_script_exists() -> None:
    assert VALIDATOR.is_file(), f"{VALIDATOR} is missing"


def test_trc_deploy_contract_checks_pass() -> None:
    # No docker program at all raises FileNotFoundError rather than failing `version`.
    if shutil.which("docker") is None or subprocess.run(
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


def test_a_missing_docker_program_skips_rather_than_errors(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def no_program(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", no_program)
    with pytest.raises(pytest.skip.Exception):
        test_trc_deploy_contract_checks_pass()


def test_a_failed_isolation_check_stops_the_sandbox() -> None:
    """A sandbox that fails its isolation check must not stay up serving scripts:
    stopped, execute_code answers `unavailable` and `restart: unless-stopped` keeps it
    down."""
    lines = [
        ln.strip() for ln in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "adversarial_check.py" in ln and not ln.strip().startswith("#")
    ]
    assert len(lines) == 1, lines
    check, _, on_failure = lines[0].partition(" || ")
    assert check.startswith("docker exec -u 10000 "), "the check must run as the gateway's uid"
    assert check.endswith("adversarial_check.py --quick")
    assert on_failure.startswith("{ docker stop hermes-sandbox;")
    assert on_failure.endswith("exit 1; }")
