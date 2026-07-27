# TRC staging deploy — hermes-agent

Deploys this fork's image to the TRC staging host as its own compose project,
`trc-staging-hermes-agent`. This repo owns only hermes-agent; `trc-open-webui`
and `paperclip` deploy themselves the same way, and all three attach to the
shared external `poc-net` bridge.

**Build and deploy are one workflow, one dispatch.** There is no separate
publish step any more — `trc-publish.yml` is gone. The workflow builds the
image on the runner, pushes it to GHCR, and then rolls it out to the staging
host in the same run, so the tag the deploy pulls always exists by the time it
pulls it.

The workflow runs on a **self-hosted runner** (`runs-on: self-hosted`),
because the staging host is internal and unreachable from a GitHub-hosted
runner. The build step runs with no special Docker configuration — it builds
and pushes using the runner's own local daemon. Only the later steps that
actually need to reach the staging host set `DOCKER_HOST: ssh://…` in their
own **step-level** `env:`. That scoping is deliberate and never a job-level
`env:` or a `docker context`:

- A `docker context` is *persistent state* on a self-hosted runner. `docker
  context create` fails with "already exists" on the second run against the
  same runner, and `docker context use` repoints that runner's **default**
  daemon for every later job that lands on it — including unrelated jobs from
  other workflows.
- `docker buildx` binds to whichever daemon is *current*. An active context
  (or a job-level `DOCKER_HOST`) would silently build the image **on the
  deploy host** instead of the runner.
- A step-level `env: DOCKER_HOST: …` cannot leak into the build step, because
  it is set only on the steps that declare it.

Three consequences of the remote-daemon design worth knowing:

- The rendered `.env.staging` file is read by the **local** Compose CLI via
  `--env-file` and is never copied to the server. Its values reach the
  containers as container environment, sent over the Docker API through the
  SSH tunnel — there is no plaintext secret file on the staging host. This is
  still true under the self-hosted runner: the runner is not the deploy host,
  so nothing changes about where the file lands.
- Bind-mount sources (`config.yaml`) are resolved by the **remote** daemon, so
  that path must be absolute and must already exist on the server before
  `compose up` runs. The deploy still `scp`s `config.yaml` into place for this
  reason — it is the one file that genuinely has to reach the host.
- `docker compose pull` sends the **runner's** registry credentials to the
  remote daemon (`X-Registry-Auth`), not the host's — the staging host never
  **stores** a credential of its own. The workflow logs in to `ghcr.io` on the
  runner with the built-in `GITHUB_TOKEN` before pulling; this works whether
  the package is public (as it is today) or private.

**The runner is persistent.** Unlike a GitHub-hosted runner, this machine is
reused by later jobs — including other repos' deploys. That has two
consequences baked into the workflow:

- `~/.ssh/known_hosts` carries entries from other jobs that are not this
  workflow's to delete. The host-key scan writes into `$RUNNER_TEMP` first,
  removes any stale entry for *this* host with `ssh-keygen -R` (both the
  bare-host and `[host]:port` spellings), and only then **appends**
  (`>>`) — it never truncates the real file with a bare `>`.
- Every secret this workflow writes to disk on the runner —
  `~/.ssh/id_rsa`, `.env.staging`, the `hermes.fpr` fingerprint temp file —
  is removed in a final `Clean up secrets on the runner` step, which runs
  with `if: always()` so a failed deploy still leaves the runner clean.

## Host prerequisites

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

The workflow builds and publishes two tags on every dispatch:

- `ghcr.io/nature-technologies/trc-hermes-agent:staging` — a **moving** tag,
  overwritten by every run. This is what the deploy pulls.
- `ghcr.io/nature-technologies/trc-hermes-agent:git-<7-char-sha>` — an
  **immutable** tag naming the exact commit that was built. This is what
  rollback points at.

1. Actions → **TRC staging deploy (hermes-agent)** → Run workflow.
2. Leave `bootstrap` unchecked (default `false`) unless this is the first
   deploy to a brand-new host.
3. The workflow builds the image from the checked-out ref, pushes both tags,
   and deploys `:staging`.

### Rolling back

**There is no digest input.** Rolling back means re-dispatching the workflow
from a branch whose workflow definition points at the `:git-<short-sha>` tag
of the build you want, instead of building a new one:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. Edit this workflow on that branch so the `Pull and deploy` step deploys
   `${IMAGE}:git-<short-sha>` instead of `${IMAGE}:staging` — pick the short
   sha from a previous run's logs or the GHCR package's tag list. Commit and
   push the branch.
3. Actions → **TRC staging deploy (hermes-agent)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick.
4. Delete the branch once you are done. To roll forward again, dispatch the
   deploy from `dev` as normal, which builds fresh and redeploys `:staging`.

Because build and deploy are unified, a rollback dispatch **also rebuilds and
re-pushes `:staging` from that branch's source** unless you edit the workflow
to skip the build step or point the pull at the `:git-` tag directly, as
above — a plain re-dispatch from an old branch is not itself a rollback.

The `Pull and deploy` step still prints the digest that actually landed, so
every run log records what is now running.

## Required repository secrets (environment: `staging`)

The environment name is lowercase `staging` — GitHub Actions matches
environment names case-sensitively, so a `Staging` environment's secrets will
not resolve here.

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
non-destructively — see "The runner is persistent" above for why it is never
a bare `>` onto the real file. Trust-on-first-use has the same trade-off it
always did:

- It still protects against a passive attacker who cannot intercept the very
  first connection of a run — `StrictHostKeyChecking` is never disabled, so if
  the host key changes *after* the scan (e.g. mid-run, or on a subsequent run
  against a key that was swapped since the last scan) the connection still
  aborts rather than silently trusting a new key.
- It does **not** protect against an active machine-in-the-middle present at
  the moment `ssh-keyscan` runs, since there is no prior pinned key to compare
  against. This is a deliberate trade against the operational cost of
  maintaining a pinned-key secret in step with any host-key rotation.

Every secret rendered into `.env.staging` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring — a mangled
`HERMES_API_KEY` looks like a 401, a mangled dashboard password like a wrong
password. `openssl rand -hex 32` never produces any of them. `.env.staging`
itself is written on the runner and passed to Compose with `--env-file`; it is
never copied to the host, and it is deleted by the final cleanup step even
when an earlier step in the run fails.

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

- `docker context create` appears **nowhere** in the workflow;
- `DOCKER_HOST` appears only as **step-level** `env:`, never at job level —
  a job-level `DOCKER_HOST` would apply to the build step too;
- the step running `docker/build-push-action` has no `DOCKER_HOST` in its own
  `env:` — it must build on the runner, not the deploy host;
- no line redirects `ssh-keyscan` output onto `known_hosts` with a bare `>`
  outside `$RUNNER_TEMP` — that would truncate a file that persists across
  jobs on this runner;
- an `if: always()` cleanup step exists that removes `id_rsa` and
  `.env.staging` and runs `docker logout`;
- `docker volume create` never appears unconditionally — only reachable
  through a branch gated on the `bootstrap` input, and even then only after
  a loud `::warning::`;
- Compose is invoked with `--env-file`, so secret values are never
  interpolated into a shell command string;
- `compose pull` precedes `compose up -d` (same-line `pull && up -d` counts,
  compared by position within the line), so a deploy can never silently
  redeploy an image already on the host;
- every heredoc-fed `docker exec` passes `-i`, and no `docker exec` that is
  *not* heredoc-fed passes `-i` — a heredoc without `-i` gets no stdin, so the
  smoke test's body never runs and the step still exits 0.

It runs in the **TRC deploy checks** workflow and under
`pytest tests/tools/test_trc_deploy_compose.py`. Read its docstring before
changing the compose file or the deploy workflow.
