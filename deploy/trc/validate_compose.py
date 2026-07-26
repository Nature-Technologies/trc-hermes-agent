#!/usr/bin/env python3
"""Contract checks for the TRC staging deploy assets in this directory.

Additive and TRC-only: no upstream tooling reads this file.

These assert the invariants that let three independent compose projects add up
to one stack. The load-bearing one is `external: true` on every network and
volume. Compose prefixes a non-external named volume with the project name, so
declaring `trc-staging-hermes-memory` without `external: true` silently creates
an empty `trc-staging-hermes-agent_trc-staging-hermes-memory` and uses that
instead of the real volume -- Hermes then boots with no sessions, no memories
and no cron, and does not error. Nothing in `compose up` output reveals it,
which is why it is asserted here rather than left to code review.

Run: python deploy/trc/validate_compose.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
COMPOSE = HERE / "docker-compose-staging-trc.yml"
ENV_EXAMPLE = HERE / ".env.staging.example"

EXPECTED_PROJECT = "trc-staging-hermes-agent"
EXPECTED_NETWORKS = {"poc-net", "trc-shared"}
EXPECTED_VOLUMES = {"trc-staging-hermes-memory"}
# Cross-project DNS depends on these exact names: open-webui, in a different
# compose project, reaches this service at http://hermes-agent:8642.
EXPECTED_CONTAINERS = {"hermes-agent"}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def placeholder(key: str) -> str:
    """A stand-in value that satisfies compose's own validation.

    `docker compose config` validates port specifications and image
    references, so a generic filler string would fail for `*_BIND` and
    `*_IMAGE` regardless of whether the file is correct.
    """
    if key.endswith("_IMAGE"):
        return "ghcr.io/nature-technologies/placeholder@sha256:" + "0" * 64
    if key.endswith("_BIND"):
        return "127.0.0.1"
    if key.endswith("_URL"):
        return "http://placeholder.invalid:3100"
    if key.endswith("_DIR"):
        return "/srv/trc/staging/placeholder"
    # 64 chars clears every length guard in the stack, including the gateway's
    # 16-character minimum on HERMES_API_KEY.
    return "x" * 64


def check_compose_renders() -> None:
    keys = [
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    check(bool(keys), "no assignments found in .env.staging.example")
    if not keys:
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".env", delete=False, encoding="utf-8"
    ) as fh:
        for key in keys:
            fh.write(f"{key}={placeholder(key)}\n")
        env_path = fh.name
    proc = subprocess.run(
        [
            "docker", "compose",
            "--env-file", env_path,
            "-f", str(COMPOSE),
            "config", "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    Path(env_path).unlink(missing_ok=True)
    check(
        proc.returncode == 0,
        "`docker compose config` failed -- every variable the compose file "
        f"references must be declared in .env.staging.example:\n{proc.stderr.strip()}",
    )


def report() -> int:
    if failures:
        print(f"{len(failures)} check(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("all TRC deploy checks passed")
    return 0


def main() -> int:
    check(COMPOSE.is_file(), f"missing {COMPOSE}")
    check(ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}")
    if failures:
        return report()

    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    check(
        doc.get("name") == EXPECTED_PROJECT,
        f"top-level `name:` must be {EXPECTED_PROJECT!r}, got {doc.get('name')!r}",
    )

    networks = doc.get("networks") or {}
    check(
        set(networks) == EXPECTED_NETWORKS,
        f"networks must be exactly {sorted(EXPECTED_NETWORKS)}, got {sorted(networks)}",
    )
    for name, spec in networks.items():
        check(
            bool((spec or {}).get("external")),
            f"network {name!r} must declare `external: true` -- no single "
            "compose project owns creation of a shared bridge",
        )

    volumes = doc.get("volumes") or {}
    check(
        set(volumes) == EXPECTED_VOLUMES,
        f"volumes must be exactly {sorted(EXPECTED_VOLUMES)}, got {sorted(volumes)}",
    )
    for name, spec in volumes.items():
        check(
            bool((spec or {}).get("external")),
            f"volume {name!r} must declare `external: true` -- see this "
            "module's docstring for the silent-data-loss failure this prevents",
        )

    services = doc.get("services") or {}
    container_names = {spec.get("container_name") for spec in services.values()}
    check(
        container_names == EXPECTED_CONTAINERS,
        f"container_name set must be exactly {sorted(EXPECTED_CONTAINERS)}, "
        f"got {sorted(n for n in container_names if n)}",
    )

    for svc, spec in services.items():
        check(
            spec.get("restart") == "unless-stopped",
            f"service {svc!r} must set `restart: unless-stopped` -- there is no "
            "cross-project depends_on, so restart is the only ordering mechanism",
        )
        for dep in spec.get("depends_on") or {}:
            check(
                dep in services,
                f"service {svc!r} declares depends_on {dep!r}, which is not in "
                "this compose project -- depends_on cannot cross projects",
            )

    check_compose_renders()
    return report()


if __name__ == "__main__":
    sys.exit(main())
