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

import re
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
# Service-level membership, which is a different assertion from EXPECTED_NETWORKS
# above. A top-level network that no service joins is silently IGNORED: drop
# `trc-shared` from the hermes-agent service's own `networks:` list and this
# validator, `docker compose config` and `docker compose up` all stay green,
# while the container can no longer resolve app:8000 -- the only symptom is
# every ragnarok MCP call failing at runtime.
EXPECTED_SERVICE_NETWORKS = {"hermes-agent": {"poc-net", "trc-shared"}}

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
    if key.endswith("_DIR") or key.endswith("_PATH"):
        return "/srv/trc/staging/placeholder"
    # 64 chars clears every length guard in the stack, including the gateway's
    # 16-character minimum on HERMES_API_KEY.
    return "x" * 64


def env_example_keys() -> list[str]:
    return [
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]


def service_networks(spec: dict) -> set[str]:
    """The networks a service joins, in either the list or the mapping form.

    `networks: [poc-net, trc-shared]` and
    `networks: {poc-net: null, trc-shared: {aliases: [...]}}` are both valid
    compose and mean the same membership, so both spellings have to be read or
    a reformat could quietly turn the assertion off.
    """
    nets = (spec or {}).get("networks")
    if nets is None:
        return set()
    return set(nets)


def check_env_example_declares_every_reference() -> None:
    """Assert every ${VAR} the compose file references is declared in the example.

    `docker compose config` cannot carry this. For a plain ${VAR} substitution it
    emits a warning and still exits 0, so only the ${VAR:?} spellings would ever
    fail -- an omission from .env.staging.example would silently become a blank
    default. Comparing the two sets directly is what makes it an error.
    """
    referenced = set(
        re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", COMPOSE.read_text(encoding="utf-8"))
    )
    missing = sorted(referenced - set(env_example_keys()))
    check(
        not missing,
        f"compose references {missing} but .env.staging.example does not declare "
        "them -- a plain ${VAR} omission would otherwise default to a blank "
        "string with only a warning from `docker compose config`",
    )


def check_compose_renders() -> None:
    keys = env_example_keys()
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
        "`docker compose config` failed -- rendering the compose file with "
        "placeholder values for every key in .env.staging.example must "
        f"succeed, which exercises the `${{VAR:?}}` guards:\n{proc.stderr.strip()}",
    )


WORKFLOW = HERE.parents[1] / ".github" / "workflows" / "trc-staging-deploy.yml"


def _run_script_lines(doc: dict) -> list[str]:
    """Every executable line of every `run:` block, full-line shell comments dropped.

    Assertions about script behaviour cannot match the raw file text: these
    scripts document the rules they follow, so a comment saying "never set -x"
    reads as a violation and a comment saying "serialize on flock" reads as
    compliance. Only executable lines carry either meaning.
    """
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if not script:
                continue
            lines += [
                ln for ln in script.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
    return lines


def _set_words(line: str) -> list[str] | None:
    """The option words of a `set ...` line, or None if the line is not one."""
    match = re.match(r"^\s*set\s+(-.*)$", line)
    return match.group(1).split() if match else None


def _enables_tracing(line: str) -> bool:
    words = _set_words(line)
    if words is None:
        return False
    for index, word in enumerate(words):
        if word == "-o":
            if index + 1 < len(words) and words[index + 1] == "xtrace":
                return True
            continue
        if word.startswith("-") and "x" in word.lstrip("-"):
            return True
    return False


def _step_with_run_containing(doc: dict, needle: str) -> dict | None:
    """The first step whose `run:` script contains `needle`, or None.

    Used to scope an assertion to a single step's own `env:` mapping rather
    than the whole file: `secrets.HOST` appearing ANYWHERE (e.g. in an
    unrelated step like the host-key scan) is not evidence that THIS step's
    invocation actually derives from that secret.
    """
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            script = (step or {}).get("run")
            if script and needle in script:
                return step
    return None


def _strict_mode_chars(line: str) -> set[str]:
    """Short-option letters enabled by a `set ...` line, ignoring `-o name`."""
    words = _set_words(line)
    if words is None:
        return set()
    chars: set[str] = set()
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word == "-o":
            skip = True
            continue
        if word.startswith("-"):
            chars |= set(word.lstrip("-"))
    return chars


def check_deploy_workflow() -> None:
    """Assert the deploy workflow's security and reproducibility invariants.

    These are properties a generic YAML linter cannot know about: that the
    deploy runs against a remote Docker context (never a raw SSH shell) using
    the HOST/USERNAME secrets, that the external volume is verified rather
    than pre-created, that compose is invoked with --env-file so secrets never
    hit a shell command string, that a pull always precedes `up -d`, and the
    ways this workflow could otherwise leak or weaken credentials.
    """
    check(WORKFLOW.is_file(), f"missing {WORKFLOW}")
    if not WORKFLOW.is_file():
        return
    raw = WORKFLOW.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)

    # YAML 1.1 parses the bare key `on` as the boolean True, so read both
    # spellings rather than guessing which one PyYAML lands on.
    triggers = doc.get("on", doc.get(True)) or {}
    check(
        set(triggers) == {"workflow_dispatch"},
        "deploy must be workflow_dispatch only -- auto-deploy was explicitly "
        f"rejected; got triggers {sorted(str(t) for t in triggers)}",
    )
    script_lines = _run_script_lines(doc)

    traced = [ln.strip() for ln in script_lines if _enables_tracing(ln)]
    check(
        not traced,
        f"shell tracing is enabled by {traced} -- tracing prints every secret "
        "into the run log. This catches `set -x`, `set -eux`, `set -xe`, "
        "`set -e -u -x` and `set -o xtrace`, while leaving `set -o pipefail` "
        "alone",
    )
    check(
        any({"e", "u"} <= _strict_mode_chars(ln) for ln in script_lines),
        "no `run:` block enables strict mode -- at least one must set both -e "
        "and -u",
    )
    check(
        "StrictHostKeyChecking=no" not in raw
        and "StrictHostKeyChecking no" not in raw,
        "StrictHostKeyChecking must never be disabled -- host keys are "
        "scanned at deploy time with `ssh-keyscan` (trust-on-first-use) "
        "rather than pinned in a secret, so this is the only thing standing "
        "between a mid-run key change and a silently accepted new key",
    )

    volume_creates = [
        ln.strip()
        for ln in script_lines
        if re.search(r"\bdocker\s+volume\s+create\b", ln)
    ]
    check(
        not volume_creates,
        f"{volume_creates} pre-creates a volume on the host. Compose REFUSES to "
        "start when an external volume is missing, and that fail-closed "
        "behaviour is the entire point of declaring the volume external: "
        "creating it here converts a loud failure into a silently EMPTY volume "
        "and the run goes green -- Hermes boots with no sessions, memories or "
        "cron, and every smoke test still passes. The volume is created and "
        "populated during the Phase 2 migration, never by a deploy. "
        "`docker network create poc-net` is fine and stays: a network carries "
        "no data",
    )

    check(
        any("docker volume inspect trc-staging-hermes-memory" in ln for ln in script_lines),
        "the deploy must verify the external volume with `docker volume inspect` "
        "and refuse to run if it is absent -- Compose fails closed on a missing "
        "external volume, and pre-creating one would boot the service against a "
        "silently empty volume with every smoke test still passing",
    )
    check(
        any("docker context create" in ln for ln in script_lines),
        "the deploy must run against a remote Docker context, not over an SSH "
        "shell -- that is what keeps the rendered .env off the server",
    )
    # Scoped to the step that actually runs `docker context create`, not the
    # whole file: `secrets.HOST` merely appearing somewhere else (e.g. the
    # host-key-scan step) says nothing about where THIS step's host comes
    # from. A file-wide scan would let someone hardcode the host right here
    # while `secrets.HOST` stays referenced in a completely different step.
    context_step = _step_with_run_containing(doc, "docker context create")
    context_step_env = {
        str(k): str(v) for k, v in ((context_step or {}).get("env") or {}).items()
    }
    context_step_env_text = " ".join(context_step_env.values())
    context_step_script = (context_step or {}).get("run") or ""
    check(
        context_step is not None
        and "secrets.HOST" in context_step_env_text
        and "secrets.USERNAME" in context_step_env_text
        and "${HOST}" in context_step_script
        and "${USERNAME}" in context_step_script,
        "the step that runs `docker context create` must itself source HOST "
        "and USERNAME from the HOST and USERNAME secrets (in that step's own "
        "`env:`) and actually reference them in its script -- not a literal "
        "hostname, and not merely a secret referenced somewhere else in the "
        "file",
    )
    check(
        any("--env-file" in ln for ln in script_lines),
        "compose must be invoked with --env-file so secret values are never "
        "interpolated into a shell command string",
    )
    # (line index, character offset within the line) rather than just a line
    # index: two tuples compare lexicographically, so this also gets a
    # same-line `docker compose pull && docker compose up -d` right -- with
    # line index alone, both halves share one index and `min(...) < min(...)`
    # would compare `i < i` and always be False, even though `pull` runs
    # first.
    pull_positions = [
        (i, ln.find(" pull"))
        for i, ln in enumerate(script_lines)
        if "compose" in ln and " pull" in ln
    ]
    up_positions = [
        (i, ln.find("up -d"))
        for i, ln in enumerate(script_lines)
        if "compose" in ln and "up -d" in ln
    ]
    check(
        bool(pull_positions) and bool(up_positions) and min(pull_positions) < min(up_positions),
        "`compose pull` must precede `compose up -d`, or a deploy can silently "
        "run a stale image already present on the host",
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

    check(
        set(services) == set(EXPECTED_SERVICE_NETWORKS),
        f"services must be exactly {sorted(EXPECTED_SERVICE_NETWORKS)}, got "
        f"{sorted(services)} -- every service needs an entry in "
        "EXPECTED_SERVICE_NETWORKS or its network membership goes unasserted",
    )

    for svc, spec in services.items():
        check(
            spec.get("restart") == "unless-stopped",
            f"service {svc!r} must set `restart: unless-stopped` -- there is no "
            "cross-project depends_on, so restart is the only ordering mechanism",
        )
        expected_svc_nets = EXPECTED_SERVICE_NETWORKS.get(svc)
        if expected_svc_nets is not None:
            check(
                service_networks(spec) == expected_svc_nets,
                f"service {svc!r} must join exactly "
                f"{sorted(expected_svc_nets)}, got "
                f"{sorted(service_networks(spec))} -- a top-level network no "
                "service joins is silently ignored, so dropping one from this "
                "list leaves the validator, `docker compose config` and `up` "
                "all green while the container can no longer resolve the peers "
                "it needs",
            )
        for dep in spec.get("depends_on") or {}:
            check(
                dep in services,
                f"service {svc!r} declares depends_on {dep!r}, which is not in "
                "this compose project -- depends_on cannot cross projects",
            )

    check_env_example_declares_every_reference()
    check_compose_renders()
    check_deploy_workflow()
    return report()


if __name__ == "__main__":
    sys.exit(main())
