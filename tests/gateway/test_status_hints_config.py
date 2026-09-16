"""The `status_hints` auxiliary task is registered everywhere the other tasks are."""

from __future__ import annotations

from hermes_cli.config import DEFAULT_CONFIG


def test_default_config_declares_the_task_with_its_knobs():
    task = DEFAULT_CONFIG["auxiliary"]["status_hints"]
    assert task["enabled"] is True
    assert task["provider"] == "auto"
    assert task["model"] == ""
    assert task["timeout"] == 5
    assert task["max_lines"] == 4
    assert task["cadence_seconds"] == 6
    for key in ("base_url", "api_key", "extra_body", "reasoning_effort"):
        assert key in task  # the shape every other aux task has


def test_the_cli_and_dashboard_registries_list_it():
    from hermes_cli.main import _AUX_TASKS
    from hermes_cli.web_server import _AUX_TASK_SLOTS

    assert any(key == "status_hints" for key, _name, _desc in _AUX_TASKS)
    assert "status_hints" in _AUX_TASK_SLOTS
