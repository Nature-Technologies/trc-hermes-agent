# TRC staging deploy — hermes-agent

Deploys this fork's image to the TRC staging host as its own compose project,
`trc-staging-hermes-agent`. This repo owns only hermes-agent; `trc-open-webui`
and `paperclip` deploy themselves the same way, and all three attach to the
shared external `poc-net` bridge.

**Build and deploy are one workflow, one dispatch.** There is no separate
publish step any more — `trc-publish.yml` is gone.

**The build runs on the deploy host, and there is no registry.** The workflow
runs on a **self-hosted runner** (`runs-on: self-hosted`) because the staging
host is internal and unreachable from a GitHub-hosted runner — but the runner
only orchestrates. `DOCKER_HOST: ssh://…` is set at **job level**, so every
`docker` call in the job, *including the build*, reaches the staging host's
daemon. `docker/build-push-action` runs with `push: false`, putting the image
straight into that host's image store — the same store `docker compose up -d`
reads. Only the source context crosses the network, never an image.

This is the same arrangement `trc-backend`'s deploy uses, and the reason is
caching:

- buildx's default `docker-container` driver starts with an **empty cache on
  every dispatch**, so nothing carries over between runs. The workflow uses
  `driver: docker` instead, which uses the daemon's own layer store.
- The previous design papered over this with `cache-from/to: type=gha`, which
  round-trips multi-GB layers to GitHub's cache service against a 10 GB
  per-repo LRU shared with every other workflow in this repo — so it mostly
  missed, and a cold build of this image is 15-45 min.
- The deploy host's daemon is the only Docker state here that genuinely
  persists, so its layer store is where the cache belongs.

Never a `docker context`, even now that the build is meant to run on the host.
A context is *persistent state* on a self-hosted runner: `docker context
create` fails "already exists" on the second run, and `docker context use`
repoints that runner's **default** daemon for every later job that lands on
it — including the sibling repos' deploys, which expect the local daemon. The
job-level `DOCKER_HOST` is scoped to this job and cannot leak that way.

Consequences of the remote-daemon design worth knowing:

- **No env file is written anywhere.** The `Deploy` step binds the application
  secrets as its own `env:`, and Compose resolves the compose file's `${…}`
  references from that process environment. No secret reaches disk on the
  runner *or* the server. It also means nothing is dotenv-parsed, so a `$`,
  backtick or `#` in a value is taken literally rather than interpolated or
  truncated — see "Secret charset" below.
- Bind-mount sources (`config.yaml`) are resolved by the **remote** daemon, so
  that path must be absolute and must already exist on the server before
  `compose up` runs. The deploy still `scp`s `config.yaml` into place for this
  reason — it is the one file that genuinely has to reach the host.
- **No registry credential is involved at all.** Nothing is pushed and, because
  the compose service sets `pull_policy: never`, nothing is pulled. The
  workflow holds no `packages: write` permission and never logs in to `ghcr.io`.
- The build now consumes CPU, RAM and **disk** on the staging server, alongside
  the running services. `Verify host preconditions` therefore runs **before**
  the build and checks free space, so a full disk fails in seconds rather than
  ~28 minutes in.

**The runner is persistent and SHARED.** Unlike a GitHub-hosted runner, this
machine is reused by later jobs — including `trc-open-webui` and `paperclip`'s
deploys, which can run **concurrently** with this one on the same `$HOME`.
**Three** files in that `$HOME` would otherwise be shared, and they carry
different risk. Two are now isolated per job; only `known_hosts` remains
genuinely shared:

- **The private key is a hard collision, so it is structurally isolated.** It
  is written to `$RUNNER_TEMP/id_rsa` — **never** `~/.ssh/id_rsa` — and loaded
  into a per-job `ssh-agent` whose socket is exported via `$GITHUB_ENV` for
  every later step in that job. A shared `~/.ssh/id_rsa` would let one job's
  cleanup (`rm -f ~/.ssh/id_rsa`) delete the key a sibling job is mid-deploy
  with; per-job `$RUNNER_TEMP` isolation makes that impossible, since
  `$RUNNER_TEMP` is scoped to the individual job. The agent authenticates both
  the explicit `ssh`/`scp` calls (which also pass `-i "$RUNNER_TEMP/id_rsa"`
  explicitly, redundantly with the agent, since that costs nothing) and the
  `ssh` the Docker CLI spawns internally for `DOCKER_HOST`.

  **The key file and the agent outlive the job.** There is no cleanup step —
  it was removed deliberately. `$RUNNER_TEMP` is cleared when that runner
  starts its next job, so the window is bounded, but between deploys the key
  sits on disk and an `ssh-agent` holds it decrypted in memory. If a cleanup
  step is ever reintroduced, the validator requires it be `if: always()` and
  run `ssh-agent -k` — deleting the key *file* does not unload the key.
- **`~/.docker/config.json` no longer needs isolating.** The job previously set
  a per-job `DOCKER_CONFIG` so its `docker logout ghcr.io` could not strip the
  GHCR credential out from under a sibling repo's concurrent `docker compose
  pull`. With no registry in this deploy there is no credential to protect and
  no logout to perform, so `DOCKER_CONFIG` was dropped along with the login.
- **`~/.ssh/known_hosts` is still genuinely shared, and that is left as a
  documented operational requirement rather than fixed structurally** — the
  risk is lower (worst case under a race is a redundant rescan, not a hard
  auth failure) and there is no per-job equivalent of `$RUNNER_TEMP` for a
  file every job needs to read. The host-key scan writes into `$RUNNER_TEMP`
  first, removes any stale entry for *this* host with `ssh-keygen -R` (both
  the bare-host and `[host]:port` spellings), and only then **appends**
  (`>>`) to the real file — never a bare `>` or a `tee` without `-a`, either
  of which would truncate entries other jobs rely on. **This means the runner
  that executes this workflow must process one job at a time**, and if more
  than one self-hosted runner is registered on the same machine (e.g. to
  parallelize trc-hermes-agent, trc-open-webui and paperclip deploys), each
  runner must run as a **separate OS user** so they do not share `$HOME` and
  therefore do not share `~/.ssh/known_hosts`.
- No application secret is written to disk at all any more. `.env.staging` is
  gone; the `Deploy` step passes those values to Compose as process
  environment. The only files the run leaves behind are the SSH key above and
  `hermes.fpr`, which is a non-secret 12-hex-character digest.

## Host prerequisites

### One-time host preparation (before the FIRST dispatch)

`bootstrap: true` cannot stand up a host on its own from nothing, and the list
below is what the workflow genuinely cannot do for itself. Run this **once per
host, as root or with sudo, before any dispatch** — including a `bootstrap:
true` one:

```
sudo install -d -o <deploy-user> -g <deploy-user> /srv/trc /srv/trc/staging
```

Both levels are needed, not just the leaf: the deploy's host-prep step runs
`mkdir -p` under `/srv/trc/staging`, which needs write on that directory, and
creating `/srv/trc/staging` itself needs write on `/srv` — neither of which a
non-root deploy user has on a fresh host. The deploy never uses `sudo`, by
design, so this cannot be folded into the workflow.

The deploy creates **only** `poc-net`, idempotently, on every run. Outside
`bootstrap` mode (see below) it creates neither `trc-shared` nor
`trc-staging-hermes-memory`, and fails if either is missing:

- **`trc-shared`** is owned by whoever runs the trc-backend stack. The
  `Verify host preconditions` step fails early if it is absent, before any
  secret is rendered. Create it on the backend side, not here — or dispatch
  with `bootstrap: true` on a genuinely fresh host (below).
- **`trc-staging-hermes-memory` must already exist and already hold Hermes'
  data.** The deploy does not run `docker volume create` on a routine
  deploy: the volume is declared `external: true` precisely so Compose
  refuses to start without it, and pre-creating it would boot Hermes against
  a silently *empty* volume — no sessions, no memories, no cron — with every
  smoke test still passing. See Phase 2 preconditions below.

### The `bootstrap` input

`workflow_dispatch` takes a `bootstrap` boolean input, default `false`. When
`true`, the `Verify host preconditions` step **creates** `trc-shared` and
`trc-staging-hermes-memory` instead of failing when they are missing, and logs
an `::warning::` for each one it creates. Use this **only** when standing up a
genuinely fresh host in one dispatch — it lets that first deploy succeed
without a human pre-creating the network and volume by hand over SSH. A
volume created this way is empty; it is not a substitute for the Phase 2
migration below on a host that is supposed to already have data.

Leave `bootstrap` at its default `false` on every routine deploy. With it
`false`, both checks stay fail-closed exactly as before.

The deploy user (`USERNAME`) also needs:

- **write access to `HOST_DIR`** (`/srv/trc/staging/hermes-agent`, where
  `config.yaml` is copied) and to `/srv/trc/staging/fingerprints`;
- **membership of the `docker` group** (or root) on the host, so the SSH
  session backing `DOCKER_HOST` can reach the daemon socket without `sudo`.

### Runner prerequisites

The runner this workflow executes on must **process one job at a time** —
`~/.ssh/known_hosts` is genuinely shared across jobs (see "The runner is
persistent and SHARED" above), and the non-destructive scan-then-append
pattern assumes no other job is touching that file concurrently. If more than
one self-hosted runner is registered on the same machine — e.g. to let
trc-hermes-agent, trc-open-webui and paperclip deploy in parallel — each
runner **must run as a separate OS user**, so they do not share `$HOME` and
therefore do not share `~/.ssh/known_hosts`. The private key itself does not
have this constraint: it lives under the job-scoped `$RUNNER_TEMP`, so two
jobs on the same runner user cannot collide over it even if this requirement
is violated. Neither does `~/.docker/config.json`: this job no longer logs in
to any registry, so it never writes one. **`known_hosts` is the only genuinely
shared file left**, which is why this requirement is about that file
specifically.

### Phase 2 preconditions

This repo's only stateful dependency is `trc-staging-hermes-memory`. Before the
first deploy, with the retired single-compose stack **stopped**, create the
volume and copy the old project-prefixed volume's contents into it — the old
name is the project-prefixed one Compose generated
(e.g. `trc-docker-paperclip-hermes_hermes-memory`), not `hermes-memory`. Copy
with the stack down so nothing is writing mid-copy. The deploy will refuse to
run until the volume exists (unless dispatched with `bootstrap: true`, which
is for a fresh host with no prior data to migrate).

## Running a deploy

The workflow applies two tags on every dispatch, **both only in the staging
host's local image store** — neither is pushed anywhere:

- `ghcr.io/nature-technologies/trc-hermes-agent:staging` — a **moving**
  pointer, overwritten by every run. Kept as a human-readable "what landed
  last" marker, but **nothing in this workflow ever deploys it.**
- `ghcr.io/nature-technologies/trc-hermes-agent:git-<7-char-sha>` — an
  **immutable** tag naming the exact commit that was built. **This is what
  gets deployed.** The `Deploy` step sets
  `HERMES_IMAGE: ${{ steps.tags.outputs.sha }}` in its own `env:`, so the
  deploy runs exactly what this run built — never whatever `:staging` happens
  to point at.

The `ghcr.io/…` prefix is now just a name. It is a local tag string; no
registry is contacted.

1. Actions → **TRC staging deploy (hermes-agent)** → Run workflow.
2. Leave `bootstrap` unchecked (default `false`) unless this is the first
   deploy to a brand-new host.
3. The workflow builds the image from the checked-out ref directly into the
   staging host's image store, then deploys the `:git-<7-char-sha>` tag.

### Rolling back

> **Rollback is host-local now.** The `:git-<sha>` tags exist **only** in that
> host's image store. A `docker image prune -a` there, or a host rebuild,
> destroys every rollback target, and there is no offsite copy. Check what is
> actually available before planning a rollback:
> `docker image ls 'ghcr.io/nature-technologies/trc-hermes-agent'`.

**There is no digest input.** Because every routine deploy already runs the
immutable tag it just built, rolling back to an *older* build means
re-dispatching from a branch where the **`Deploy`** step has its
`HERMES_IMAGE` env line hardcoded to an older tag instead of the dynamic
`${{ steps.tags.outputs.sha }}` expression:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. In `.github/workflows/trc-staging-deploy.yml` on that branch, find the
   `Deploy` step and change its `HERMES_IMAGE: ${{ steps.tags.outputs.sha }}`
   line to a hardcoded
   `HERMES_IMAGE: ghcr.io/nature-technologies/trc-hermes-agent:git-<short-sha>`
   — pick a short sha that `docker image ls` on the host confirms is still
   present. Commit and push the branch.
3. Actions → **TRC staging deploy (hermes-agent)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick.
4. Delete the branch once you are done. To roll forward again, dispatch from
   `dev` as normal, which builds fresh and deploys its own new `:git-<sha>`.

Because build and deploy are unified, a rollback dispatch **still rebuilds**
from that branch's source and re-tags `:staging` — but with `HERMES_IMAGE`
hardcoded as above, `up -d` starts the specific **older** tag you named. A
plain re-dispatch from an old branch, without that edit, is not a rollback —
it would build and deploy a fresh image from old source under a new sha.

Note that `pull_policy: never` makes a mistyped or pruned tag **fail closed**
with a clear error, rather than reaching out to GHCR and starting a stale image
left over from the retired push-based scheme.

## Required repository secrets (environment: `staging`)

The workflow declares `environment: staging` in lowercase. GitHub matches
environment names **case-insensitively**, so an environment named `Staging`
resolves against it correctly — that is the observed behaviour on this repo,
where the environment is `Staging` and its secrets resolve normally. The
validator still pins the workflow's spelling to lowercase `staging` for
consistency across the three TRC deploys, not because a different case would
break.

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY_DEV` | Deploy user's private key |
| `HOST` | Staging host, used both for `ssh-keyscan` and in every step's `DOCKER_HOST: ssh://${USERNAME}@${HOST}:${SSH_PORT}` |
| `USERNAME` | Deploy user on the host |
| `SSH_PORT` | Optional, defaults to 22. Threaded through every consumer that needs it: the `ssh-keyscan` that seeds `known_hosts`, every step's `DOCKER_HOST`, and the plain `ssh`/`scp` calls -- a mismatch between these would scan the wrong endpoint and then dial a different one |
| `OPENROUTER_API_KEY` | |
| `HERMES_API_KEY` | **Shared value.** Must be identical to `trc-open-webui`'s copy and to Paperclip's third copy in its instance `config.json`. Must be ≥16 characters — below that the gateway refuses to start the API server, so the symptom is connection-refused on :8642, not a 401 |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`, `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Without these the dashboard is unreachable |

Host keys are **scanned at deploy time**
(`ssh-keyscan -T 10 -p "$SSH_PORT" -H "$HOST" > "$RUNNER_TEMP/known_hosts"`)
rather than pinned in advance, and then merged into `~/.ssh/known_hosts`
non-destructively — see "The runner is persistent and SHARED" above for why
it is never a bare `>` (or an unadorned `tee`) onto the real file.
Trust-on-first-use has the same trade-off it always did:

- It still protects against a passive attacker who cannot intercept the very
  first connection of a run — `StrictHostKeyChecking` is never disabled, so if
  the host key changes *after* the scan (e.g. mid-run, or on a subsequent run
  against a key that was swapped since the last scan) the connection still
  aborts rather than silently trusting a new key.
- It does **not** protect against an active machine-in-the-middle present at
  the moment `ssh-keyscan` runs, since there is no prior pinned key to compare
  against. This is a deliberate trade against the operational cost of
  maintaining a pinned-key secret in step with any host-key rotation.

### Secret charset

**Secrets may now contain any characters, including `$`, backticks and `#`.**

This used to be guarded. Secrets were rendered into `.env.staging` and passed
to Compose with `--env-file`, which made Compose parse them as **dotenv** — and
dotenv interpolates `$`, treats a backtick as shell-adjacent, and truncates at
a ` #`. Such a value reached the container as a *different* string with nothing
erroring: a mangled `HERMES_API_KEY` looks like a 401, a mangled dashboard
password like a wrong password. The workflow therefore rejected those
characters up front.

The `Deploy` step now binds the secrets as its own `env:` and lets Compose
resolve the compose file's `${…}` references from the **process environment**.
Nothing is dotenv-parsed, so values are taken literally and the guard is
unnecessary — it was removed along with the file. This is also why no secret
touches disk on the runner or the server any more.

`HERMES_API_KEY` must still be **≥16 characters**; below that the gateway
refuses to start the API server, which surfaces as connection-refused on
`:8642` rather than a 401. That is no longer asserted before the deploy, so it
shows up in the `Smoke test` step instead.

## Editing Hermes config

`deploy/trc/config.yaml` is copied to `${HOST_DIR}/config.yaml` on every
deploy, so it is git-managed: **editing it on the server is overwritten by the
next deploy.** Change it here and re-dispatch.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — above all `external: true`
on every network and volume, plus each **service's** own `networks:` membership,
since a top-level network no service joins is silently ignored. For the deploy
workflow specifically it also asserts:

- the job requests the **`self-hosted`** runner label (all three `runs-on`
  spellings understood: scalar, list, and the `{group, labels}` mapping) —
  `ubuntu-latest` cannot reach the internal staging host at all;
- `environment` is exactly **`staging`**, lowercase — pinned for consistency
  across the three TRC deploys. GitHub itself matches environment names
  case-insensitively, so this is a house-style assertion, not a correctness
  one;
- **every** `DOCKER_HOST` value references both `secrets.HOST` and
  `secrets.USERNAME`, so a literal host cannot be substituted. Of the three
  assertions above this is the one that catches a **silent** failure: a
  hardcoded `ssh://root@10.0.0.9:22` renders every application secret and
  deploys them to whatever machine that literal names, with the smoke tests
  passing against it. The other two fail loudly at runtime;
- the `Write SSH key and scan the host key` step **positively** contains the
  whole non-destructive `known_hosts` merge: an `ssh-keyscan` into
  `$RUNNER_TEMP`, a `test -s` on it, `ssh-keygen -R` **twice** (the bare-host
  and `[host]:port` spellings), and an append (`>>`) onto
  `~/.ssh/known_hosts`. The "never truncate" rule below is negative-only, and
  on its own it passes a workflow that has no host-key handling at all;
- `docker context create` appears **nowhere** in the workflow;
- `DOCKER_HOST` is set at **job level** — the build must reach the deploy
  host's daemon, and only a job-level binding covers the build step. This is
  the **inverse** of the old rule, which banned it above step level to keep the
  build on the runner;
- `DOCKER_HOST` is **not** set at workflow level, and **no step overrides it**
  — a step-scoped value that differed would silently send just that step to
  another daemon;
- the `docker/build-push-action` step sets **`push: false`** and does **not**
  set `platforms` — the image goes straight into the deploy host's store, and
  naming an architecture the host does not have silently enables QEMU
  emulation;
- if any step removes `id_rsa`, it must be `if: always()` and must run
  `ssh-agent -k` — deleting the key file does not unload the key. (There is no
  cleanup step today; this fires only if one is reintroduced.);
- no line writes raw `ssh-keyscan` output directly onto `known_hosts` outside
  `$RUNNER_TEMP`, whether via a bare `>` or piped through `tee` — either
  bypasses the `ssh-keygen -R` stale-entry removal on a file that persists
  across jobs on this runner;
- the private key is never written to `~/.ssh/id_rsa` anywhere in the
  workflow — only under `$RUNNER_TEMP`, so sibling jobs on the same runner
  can never collide over it;
- `docker volume create` is only reachable from inside a branch whose
  **enclosing** `if`/`elif` condition tests the `bootstrap` input — checked
  with an if/elif/else/fi-aware scan, not merely "does `bootstrap` appear
  earlier in the step", so an unconditional create placed *after* that
  branch's `fi` (i.e. no longer actually gated by anything) is still
  rejected;
- Compose is **not** invoked with `--env-file`, and no step reads or writes a
  `.env.staging` file — secrets reach Compose through the `Deploy` step's
  `env:`, so nothing hits disk and nothing is dotenv-parsed. Also the inverse
  of the old rule;
- **`compose pull` does not appear.** The build puts the image in the host's
  store, so a pull would either fail or resurrect a stale GHCR tag from the
  retired push-based scheme. Again the inverse of the old rule;
- the compose service sets **`pull_policy: never`**, so a missing or mistyped
  image tag fails closed instead of falling back to the registry;
- every heredoc-fed `docker exec` passes `-i`, and no `docker exec` that is
  *not* heredoc-fed passes `-i` — a heredoc without `-i` gets no stdin, so the
  smoke test's body never runs and the step still exits 0.

It runs in the **TRC deploy checks** workflow and under
`pytest tests/tools/test_trc_deploy_compose.py`. Read its docstring before
changing the compose file or the deploy workflow.
