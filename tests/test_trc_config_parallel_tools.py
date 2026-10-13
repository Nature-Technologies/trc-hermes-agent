"""The TRC deploy runs independent RAGnarok calls concurrently, within the backend's caps.

A comparison ("compare fund X and Y", "brief me on these three companies") issues one
`query` per entity. With RAGnarok marked parallel-safe those calls run at once instead
of queueing, so the fetch phase costs roughly the slowest call rather than the sum.

The two caps match the backend's own query limiter (QUERY_MAX_CONCURRENT=6,
QUERY_MAX_CONCURRENT_PER_USER=3), so Hermes queues a fourth same-user call itself
instead of sending it to be refused with `busy`. Rollback is one line:
`max_concurrent_calls: 1` or `supports_parallel_tool_calls: false`.

Loaded through the real `load_config` against a temp HERMES_HOME, so the assertions are
about the MERGED config Hermes acts on, not the raw file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "trc"


def _merged_config(tmp_path, monkeypatch) -> dict:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shutil.copy(_DEPLOY / "config.yaml", home / "config.yaml")

    from hermes_cli.config import load_config

    return load_config()


def test_ragnarok_calls_run_in_parallel_within_the_backend_caps(tmp_path, monkeypatch):
    ragnarok = _merged_config(tmp_path, monkeypatch)["mcp_servers"]["ragnarok"]

    assert ragnarok["supports_parallel_tool_calls"] is True
    assert ragnarok["max_concurrent_calls"] == 6
    assert ragnarok["max_concurrent_calls_per_user"] == 3
    # Parallelism must not loosen who a call is for.
    assert ragnarok["forward_user_identity"] is True


def test_the_generic_parallel_guidance_block_stays_off(tmp_path, monkeypatch):
    """The TRC rule lives in SOUL.md; Hermes' generic block only adds prompt length."""
    agent = _merged_config(tmp_path, monkeypatch)["agent"]

    assert agent["parallel_tool_call_guidance"] is False


def test_soul_tells_the_model_to_issue_independent_lookups_together():
    soul = (_DEPLOY / "SOUL.md").read_text(encoding="utf-8")

    assert "issue them all in the same turn" in soul
