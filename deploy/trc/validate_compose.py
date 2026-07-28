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


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing shell comment, respecting quotes.

    Assertions about script behaviour cannot match the raw file text: these
    scripts document the rules they follow, so a comment saying "never set -x"
    reads as a violation and a comment saying "serialize on flock" reads as
    compliance. Only executable lines carry either meaning -- and that cuts
    both ways. A "must not appear" check tripping on a comment is merely
    noisy, but a "must appear" check being SATISFIED by a comment is unsafe:
    a comment naming `docker logout` after the real line was deleted would
    let the cleanup-step check stay green while the secret stays on the
    runner.

    A `#` inside single or double quotes is not a comment, so quote state is
    tracked rather than cutting at the first `#`.
    """
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            # Only a comment when it starts a word -- `foo#bar` is not one.
            if index == 0 or line[index - 1].isspace():
                return line[:index]
    return line


def _script_lines(script: str) -> list[str]:
    """Every executable line of a single shell script, comments dropped.

    Both full-line comments and inline trailing comments (see
    _strip_inline_comment) are removed, so every substring-based assertion
    built on this list sees executable text only.
    """
    lines: list[str] = []
    for ln in script.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        stripped = _strip_inline_comment(ln)
        if stripped.strip():
            lines.append(stripped)
    return lines


def _run_script_lines(doc: dict) -> list[str]:
    """Every executable line of every `run:` block in the workflow, comments dropped."""
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            script = (step or {}).get("run")
            if script:
                lines += _script_lines(script)
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


def _step_by_name(doc: dict, name: str) -> dict | None:
    """The first step whose `name:` exactly matches, or None."""
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if (step or {}).get("name") == name:
                return step
    return None


def _step_with_uses_containing(doc: dict, needle: str) -> dict | None:
    """The first step whose `uses:` value contains `needle`, or None.

    Used to scope an assertion to a single step's own `env:` mapping rather
    than the whole file -- e.g. confirming the build step specifically has no
    DOCKER_HOST, not just that DOCKER_HOST appears somewhere unrelated.
    """
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            uses = (step or {}).get("uses")
            if uses and needle in uses:
                return step
    return None


def _step_env_keys(step: dict | None) -> set[str]:
    return set((step or {}).get("env") or {})


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


HEREDOC_RE = re.compile(r"<<-?\s*'?[A-Za-z_][A-Za-z0-9_]*'?\s*$")

# Raw `ssh-keyscan` output must never land directly on the real
# `~/.ssh/known_hosts` -- by `>` (which truncates it) OR by piping through
# `tee` (which truncates too unless `-a` is passed, and even `tee -a` skips
# the `ssh-keygen -R` stale-entry removal the workflow relies on). `~/.ssh`
# persists between jobs on a self-hosted runner, so other entries there are
# not ours to delete or duplicate -- the scan must land in $RUNNER_TEMP first
# and be merged in with `ssh-keygen -R` plus `>>`. A literal `>` inside a
# `>>` still matches this regex, but the workflow never appends raw keyscan
# output directly (it always goes through $RUNNER_TEMP), so that combination
# does not arise here.
KEYSCAN_TRUNCATE_RE = re.compile(r"ssh-keyscan\b.*(?:>|\|\s*tee\b).*known_hosts")


def _writes_default_ssh_key(line: str) -> bool:
    """True when `line` names ~/.ssh/id_rsa, on already comment-stripped input.

    A plain substring test is safe here because _run_script_lines strips
    inline comments at the source (see _strip_inline_comment) -- a comment
    merely naming the path can no longer reach this function. A round-2
    write-indicator heuristic (requiring `>`, `tee`, `install`, etc. on the
    same line) is no longer needed and is strictly weaker: the plain test
    also catches write forms an indicator list would miss, e.g. `dd of=` or
    a heredoc redirected there.
    """
    return "~/.ssh/id_rsa" in line


def _runs_on_labels(job: dict) -> list[str]:
    """The runner labels a job requests, in all three valid spellings.

    `runs-on: self-hosted` (scalar), `runs-on: [self-hosted, linux]` (list) and
    `runs-on: {group: g, labels: [self-hosted, linux]}` (mapping) all mean the
    same thing, so all three have to be read or a reformat could quietly turn
    the assertion off.
    """
    runs_on = (job or {}).get("runs-on")
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            return [labels]
        return [str(x) for x in (labels or [])]
    return [str(x) for x in (runs_on or [])]


def _environment_name(job: dict) -> str | None:
    """A job's deployment-environment name, in either spelling.

    `environment: staging` and `environment: {name: staging, url: ...}` are both
    valid and mean the same thing.
    """
    environment = (job or {}).get("environment")
    if isinstance(environment, dict):
        name = environment.get("name")
        return None if name is None else str(name)
    return None if environment is None else str(environment)


def _docker_host_values(doc: dict) -> list[tuple[str, str]]:
    """Every DOCKER_HOST value in the workflow, with where it was found.

    Keyed on the exact name `DOCKER_HOST`, so sibling variables that merely
    start with `DOCKER_` -- notably the job-level `DOCKER_CONFIG` that isolates
    this job's registry credential and buildx state -- are not collected here
    and cannot trip the DOCKER_HOST assertions.
    """
    found: list[tuple[str, str]] = []
    for key, value in (doc.get("env") or {}).items():
        if key == "DOCKER_HOST":
            found.append(("workflow-level env", str(value)))
    for job_name, job in (doc.get("jobs") or {}).items():
        for key, value in (((job or {}).get("env")) or {}).items():
            if key == "DOCKER_HOST":
                found.append((f"job {job_name!r} env", str(value)))
        for step in (job or {}).get("steps") or []:
            for key, value in (((step or {}).get("env")) or {}).items():
                if key == "DOCKER_HOST":
                    found.append((f"step {(step or {}).get('name')!r} env", str(value)))
    return found


def _docker_exec_is_interactive(line: str) -> bool:
    """True when the `docker exec` on this line passes an -i style flag.

    Flags are the dash-prefixed tokens between `docker exec` and the container
    name, so `-i`, `-it` and `-ti` all count.
    """
    after = line.split("docker exec", 1)[1].split()
    for token in after:
        if not token.startswith("-"):
            break
        if "i" in token.lstrip("-"):
            return True
    return False


def check_deploy_workflow() -> None:
    """Assert the deploy workflow's security and reproducibility invariants.

    These are properties a generic YAML linter cannot know about: that the
    deploy runs against a remote Docker context (never a raw SSH shell) using
    the HOST/USERNAME secrets, that the external volume is verified rather
    than pre-created, that no env file is written so secrets never hit disk,
    that the image is built on the deploy host and never pushed or pulled,
    and the ways this workflow could otherwise leak or weaken credentials.
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

    # The plan's three non-negotiables, none of which was gated before: a
    # mutation test proved `runs-on: ubuntu-latest` + `environment: Staging` +
    # a hardcoded `DOCKER_HOST: ssh://root@10.0.0.9:22` all passed together.
    # The first two fail loudly at runtime; the hardcoded host is the SILENT
    # one -- it would render every application secret and deploy them to
    # whatever machine that literal names.
    for job_name, job in (doc.get("jobs") or {}).items():
        labels = _runs_on_labels(job)
        check(
            "self-hosted" in labels,
            f"job {job_name!r} must request the `self-hosted` runner label "
            f"(got runs-on {labels!r}) -- the staging host is internal and "
            "unreachable from a GitHub-hosted runner, so `ubuntu-latest` "
            "fails at the first `ssh-keyscan` after the secrets have already "
            "been rendered",
        )
        env_name = _environment_name(job)
        check(
            env_name == "staging",
            f"job {job_name!r} must set `environment: staging`, exactly and in "
            f"lowercase (got {env_name!r}) -- GitHub matches environment names "
            "CASE-SENSITIVELY, so `Staging` resolves no secrets at all and "
            "every one of them arrives as the empty string",
        )

    docker_hosts = _docker_host_values(doc)
    check(
        bool(docker_hosts),
        "no DOCKER_HOST is set anywhere -- the deploy would run every "
        "docker/compose call against the runner's own daemon",
    )
    for where, value in docker_hosts:
        check(
            "secrets.HOST" in value and "secrets.USERNAME" in value,
            f"the DOCKER_HOST on {where} is {value!r}, which does not "
            "reference both `secrets.HOST` and `secrets.USERNAME`. Every "
            "DOCKER_HOST must be built from those secrets so a literal host "
            "cannot be substituted: a hardcoded value is the one failure in "
            "this file that is SILENT -- it renders every application secret "
            "and deploys them to whatever machine that literal names, with "
            "the smoke tests passing against it",
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

    # `docker volume create` is banned UNLESS it is reachable only through a
    # branch that tests the `bootstrap` input -- Phase 1c added a bootstrap
    # mode that creates the external volume on a fresh host, loudly. What
    # must never happen is a SILENT, unconditional creation: that converts
    # Compose's fail-closed behaviour on a missing external volume into a
    # silently EMPTY volume, with every smoke test below still passing.
    # Checked per-step (each `run:` is one contiguous script) rather than on
    # the flattened cross-job line list, so "bootstrap" merely appearing
    # somewhere else in the file cannot gate a create in an unrelated step.
    #
    # A depth-tracked if/elif/else/fi scan, not "does 'bootstrap' appear
    # anywhere earlier in the step": that weaker check passes an
    # unconditional create placed AFTER the bootstrap branch's `fi` (the
    # branch closed, so it no longer gates anything below it), which is
    # exactly the fail-open case a reviewer found. Each stack frame tracks
    # whether the CURRENTLY ACTIVE clause of that if-block (the most recent
    # if/elif/else at that depth) tests `bootstrap`; a create only passes
    # while at least one enclosing frame is in its bootstrap-gated clause.
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            script = (step or {}).get("run") or ""
            if not script:
                continue
            step_lines = _script_lines(script)
            stack: list[bool] = []
            for ln in step_lines:
                stripped = ln.strip()
                m = re.match(r"^(if|elif)\b(.*)$", stripped)
                if m:
                    # Case-insensitive: all three repos now read the input
                    # through an `env: BOOTSTRAP:` binding and test
                    # `[ "$BOOTSTRAP" = "true" ]`, rather than splicing
                    # `${{ inputs.bootstrap }}` into the shell text.
                    gated = "bootstrap" in stripped.lower()
                    if m.group(1) == "elif" and stack:
                        stack[-1] = gated
                    else:
                        stack.append(gated)
                    continue
                if re.match(r"^else\b", stripped):
                    if stack:
                        stack[-1] = False
                    continue
                if re.match(r"^fi\b", stripped):
                    if stack:
                        stack.pop()
                    continue
                if not re.search(r"\bdocker\s+volume\s+create\b", ln):
                    continue
                check(
                    any(stack),
                    f"{ln.strip()!r} pre-creates a volume on the host "
                    "reachable OUTSIDE a branch gated on the `bootstrap` "
                    "input (either never inside one, or after that branch's "
                    "`fi` already closed it). Compose REFUSES to start when "
                    "an external volume is missing, and that fail-closed "
                    "behaviour is the entire point of declaring the volume "
                    "external: an unconditional create silently turns a loud "
                    "failure into an EMPTY volume and the run goes green -- "
                    "Hermes boots with no sessions, memories or cron, and "
                    "every smoke test still passes. `docker network create "
                    "poc-net` is fine and stays unconditional: a network "
                    "carries no data",
                )

    check(
        any("docker volume inspect trc-staging-hermes-memory" in ln for ln in script_lines),
        "the deploy must verify the external volume with `docker volume inspect` "
        "and refuse to run if it is absent -- Compose fails closed on a missing "
        "external volume, and pre-creating one would boot the service against a "
        "silently empty volume with every smoke test still passing",
    )
    check(
        not any("docker context create" in ln for ln in script_lines),
        "`docker context create` must appear nowhere -- a context is "
        "persistent state on a self-hosted runner: `create` fails 'already "
        "exists' on the second run, `use` repoints the runner's default "
        "daemon for every later job, and buildx binds to whichever daemon is "
        "current, so an active context would build the image on the deploy "
        "host instead of the runner. Use step-scoped DOCKER_HOST instead",
    )
    # Inverted from the old contract. Secrets used to be rendered into
    # .env.staging and passed with --env-file; they are now bound as the Deploy
    # step's own `env:` and resolved by Compose from the process environment.
    # That is strictly safer -- no secret reaches disk on the runner OR the
    # server -- and it also stops Compose dotenv-parsing the values, which used
    # to interpolate a `$` and truncate at a `#`, silently mangling a password.
    check(
        not any("--env-file" in ln for ln in script_lines),
        "compose must NOT be invoked with --env-file -- secrets are bound as "
        "the Deploy step's `env:` and resolved from the process environment, "
        "so nothing is written to disk and no value is dotenv-parsed (a `$` "
        "would be interpolated and a `#` would truncate the value)",
    )
    check(
        not any(".env.staging" in ln for ln in script_lines),
        "no step may write or read a .env.staging file -- secrets reach "
        "Compose through the Deploy step's `env:`, never through disk",
    )
    # `compose pull` is now forbidden, the exact inverse of the old rule. The
    # image is built straight into the deploy host's image store and pushed
    # nowhere, so a pull can only either fail or -- for a commit deployed under
    # the old push-based scheme -- succeed against a stale tag still sitting in
    # GHCR and start code that is not what this run built.
    check(
        not any("compose" in ln and " pull" in ln for ln in script_lines),
        "`compose pull` must not appear -- the build puts the image directly "
        "in the deploy host's store, so a pull would either fail or resurrect "
        "a stale GHCR tag from the retired push-based scheme",
    )

    # The defect class that has cost this project the most, shipped twice in
    # Phase 1. A `docker exec` fed a heredoc WITHOUT -i gets no stdin, so
    # `python3 -` reads EOF, the body never runs, and the step exits 0 -- a
    # smoke test that silently tests nothing. The inverse bites too: -i on a
    # call that is not heredoc-fed makes it swallow the enclosing script's
    # remaining lines. All three repos are correct today; this keeps them so.
    # `_run_script_lines` drops full-line comments, so prose mentioning
    # `docker exec` cannot trip this.
    for ln in script_lines:
        if "docker exec" not in ln:
            continue
        fed = bool(HEREDOC_RE.search(ln))
        interactive = _docker_exec_is_interactive(ln)
        check(
            fed == interactive,
            f"`docker exec` mismatch on: {ln.strip()!r} -- a heredoc-fed call "
            "MUST pass -i or no stdin reaches the container, the body never "
            "runs and the step exits 0; a call that is NOT heredoc-fed must "
            "NOT pass -i, or it consumes the rest of the enclosing script",
        )

    # INVERTED from the old contract, deliberately. DOCKER_HOST used to be
    # banned above step level so the build would stay on the runner. The build
    # now runs ON THE DEPLOY HOST on purpose: buildx's container driver starts
    # with an empty cache every dispatch, so the host daemon's layer store is
    # the only cache that survives, and a cold build of this image is 15-45
    # min. Job level is what makes every docker call -- including the build --
    # reach that daemon.
    job_level_docker_host = [
        name for name, job in (doc.get("jobs") or {}).items()
        if "DOCKER_HOST" in set(((job or {}).get("env")) or {})
    ]
    check(
        bool(job_level_docker_host),
        "DOCKER_HOST must be set at JOB level -- the build runs on the deploy "
        "host so its daemon's layer cache survives between dispatches, and "
        "only a job-level binding reaches the build step",
    )
    check(
        "DOCKER_HOST" not in set(doc.get("env") or {}),
        "DOCKER_HOST must not be set at WORKFLOW level -- keep it on the job "
        "so its scope is visible next to the steps it governs",
    )
    # No step may override the job-level value. A step-scoped DOCKER_HOST that
    # differs would send just that step to a different daemon -- the silent
    # failure the old step-scoped design was itself trying to prevent.
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            check(
                "DOCKER_HOST" not in _step_env_keys(step),
                f"the {str((step or {}).get('name', '?'))!r} step overrides "
                "DOCKER_HOST in its own `env:` -- it is set once at job level "
                "so every docker call reaches the same daemon; a step-level "
                "override would silently redirect only that step",
            )

    # The build must produce the image in the deploy host's store and push it
    # nowhere. `push: true` would reintroduce the registry round-trip this
    # design removed, and would need a registry credential on the runner again.
    build_step = _step_with_uses_containing(doc, "docker/build-push-action")
    check(
        build_step is not None,
        "no step uses docker/build-push-action -- build and deploy are "
        "unified in this workflow now that trc-publish.yml is deleted, so "
        "the image must be built here",
    )
    if build_step is not None:
        check(
            (build_step.get("with") or {}).get("push") in (False, "false"),
            "the docker/build-push-action step must set `push: false` -- the "
            "image is built straight into the deploy host's image store and "
            "there is no registry in this deploy any more",
        )
        check(
            "platforms" not in (build_step.get("with") or {}),
            "the docker/build-push-action step must not set `platforms` -- "
            "the build is native to the deploy host, and naming an "
            "architecture it does not have silently switches on QEMU "
            "emulation, which is far slower than the cold build this design "
            "exists to avoid",
        )

    keyscan_truncates = [
        ln.strip() for ln in script_lines
        if KEYSCAN_TRUNCATE_RE.search(ln) and "RUNNER_TEMP" not in ln
    ]
    check(
        not keyscan_truncates,
        f"{keyscan_truncates} writes `ssh-keyscan` output directly onto "
        "known_hosts (via `>` or piped through `tee`), which either "
        "TRUNCATES the file or skips the `ssh-keygen -R` stale-entry removal "
        "-- ~/.ssh/known_hosts persists between jobs on a self-hosted "
        "runner, so entries already there are not ours to delete or "
        "duplicate. Scan into $RUNNER_TEMP first, remove any stale entry for "
        "this host with `ssh-keygen -R`, then append (`>>`)",
    )

    # The negative guard above is NEGATIVE ONLY, which makes it much weaker
    # than it reads: deleting both `ssh-keygen -R` lines AND truncating the
    # append leaves nothing for KEYSCAN_TRUNCATE_RE to match, so the file
    # passes with no host-key handling at all. These four positive assertions
    # are what actually require the non-destructive merge to exist, and they
    # are scoped to the SSH-setup step by name rather than to the flattened
    # cross-step line list, so a stray `>>` somewhere else cannot satisfy them.
    ssh_step_name = "Write SSH key and scan the host key"
    ssh_step = _step_by_name(doc, ssh_step_name)
    check(
        ssh_step is not None,
        f"no step named {ssh_step_name!r} -- the deploy must scan the host key "
        "into $RUNNER_TEMP and merge it into ~/.ssh/known_hosts before any "
        "ssh/scp/DOCKER_HOST call",
    )
    if ssh_step is not None:
        ssh_lines = _script_lines(ssh_step.get("run") or "")
        check(
            any("ssh-keyscan" in ln and "RUNNER_TEMP" in ln for ln in ssh_lines),
            f"the {ssh_step_name!r} step must run `ssh-keyscan` into a file "
            "under $RUNNER_TEMP -- the scan has to land in job-scoped scratch "
            "space first so the merge into the shared ~/.ssh/known_hosts can "
            "be non-destructive",
        )
        check(
            any(
                re.search(r"\btest\s+-s\b", ln) and "RUNNER_TEMP" in ln
                for ln in ssh_lines
            ),
            f"the {ssh_step_name!r} step must `test -s` the scanned file under "
            "$RUNNER_TEMP -- `ssh-keyscan` exits 0 even when nothing answered, "
            "so without this the run continues with an EMPTY known_hosts and "
            "fails much later, after the build, on a confusing host-key error",
        )
        keygen_removals = [
            ln for ln in ssh_lines if re.search(r"\bssh-keygen\s+-R\b", ln)
        ]
        bracketed = [ln for ln in keygen_removals if "[" in ln]
        bare = [ln for ln in keygen_removals if "[" not in ln]
        check(
            len(keygen_removals) >= 2 and bool(bracketed) and bool(bare),
            f"the {ssh_step_name!r} step must call `ssh-keygen -R` TWICE, once "
            "for the bare host and once for the `[host]:port` spelling (found "
            f"{len(keygen_removals)}: {len(bare)} bare, {len(bracketed)} "
            "bracketed) -- `ssh-keyscan` writes a bare host for port 22 and "
            "`[host]:port` otherwise, so removing only one spelling leaves a "
            "stale key that makes StrictHostKeyChecking abort the deploy after "
            "a host rebuild",
        )
        check(
            any(
                ">>" in ln and "known_hosts" in ln and "~/.ssh" in ln
                for ln in ssh_lines
            ),
            f"the {ssh_step_name!r} step must APPEND (`>>`) the scanned key "
            "onto ~/.ssh/known_hosts -- without the append the scan never "
            "reaches the file ssh actually reads, and with `>` instead it "
            "would truncate entries sibling jobs on this persistent runner "
            "rely on",
        )

    # The private key must never land in ~/.ssh -- this runner is shared
    # with sibling repos' deploys (trc-open-webui, paperclip), which can run
    # concurrently on the same $HOME. A shared ~/.ssh/id_rsa would let one
    # job's `rm -f ~/.ssh/id_rsa` cleanup delete the key a sibling job is
    # mid-deploy with. It must live only under $RUNNER_TEMP, loaded into a
    # per-job ssh-agent.
    check(
        not any(_writes_default_ssh_key(ln) for ln in script_lines),
        "the private key must never be written to ~/.ssh/id_rsa -- this "
        "runner is shared with sibling jobs and reused across them, so a "
        "shared key file lets one job's cleanup delete the key a sibling is "
        "mid-deploy with. Write it under $RUNNER_TEMP and load it into a "
        "per-job ssh-agent instead",
    )

    # The cleanup step was removed by decision, so this no longer asserts one
    # exists. What it still enforces is the property that made cleanup matter
    # least: application secrets never reach disk at all now -- they are bound
    # as the Deploy step's `env:` rather than rendered into a file. The old
    # rule also named `.env.staging` and `docker logout`, neither of which
    # exists any more.
    #
    # $RUNNER_TEMP/id_rsa and the ssh-agent DO still outlive the job. If a
    # cleanup step is reintroduced, it must be `if: always()` and must run
    # `ssh-agent -k`: deleting the key file does not unload the key, and a
    # leaked agent holds it decrypted in memory on a persistent runner.
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            step_text = "\n".join(_script_lines((step or {}).get("run") or ""))
            if "id_rsa" not in step_text or "rm " not in step_text:
                continue
            check(
                str((step or {}).get("if", "")).strip() == "always()",
                f"the {str((step or {}).get('name', '?'))!r} step removes "
                "id_rsa but is not `if: always()` -- a run that fails earlier "
                "would leave the deploy host's private key on this persistent "
                "runner",
            )
            check(
                "ssh-agent -k" in step_text,
                f"the {str((step or {}).get('name', '?'))!r} step removes the "
                "key file but does not run `ssh-agent -k` -- removing the FILE "
                "does not unload the key, and a leaked agent holds it "
                "decrypted in memory until the machine reboots",
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

        # The deploy builds this image into the host's store and pushes it
        # nowhere, so a missing image is a broken deploy and must fail closed.
        # Compose's default (`missing`) would instead reach out to the registry
        # named in the tag -- which for a commit deployed under the retired
        # push-based scheme SUCCEEDS against a stale GHCR image and silently
        # starts code that is not what the run built.
        check(
            spec.get("pull_policy") == "never",
            f"service {svc!r} must set `pull_policy: never` -- the image is "
            "built locally on the deploy host and pushed nowhere, so without "
            "this Compose would fall back to the registry and could start a "
            "stale tag left over from the old push-based scheme",
        )

    check_env_example_declares_every_reference()
    check_compose_renders()
    check_deploy_workflow()
    return report()


if __name__ == "__main__":
    sys.exit(main())
