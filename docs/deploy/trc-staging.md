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

The workflow runs on a **self-hosted runner** (`runs-on: [self-hosted,
linux]` — labelled, not bare `self-hosted`, so a non-Linux runner registered
later on this label set cannot pick up a job that needs docker/buildx and an
OpenSSH client), because the staging host is internal and unreachable from a
GitHub-hosted runner. The build step runs with no special Docker
configuration — it builds and pushes using the runner's own local daemon.
Only the three later steps that actually need to reach the staging host —
`Verify host preconditions`, `Pull and deploy`, `Smoke test` — set
`DOCKER_HOST: ssh://…` in their own **step-level** `env:`. That scoping is
deliberate and never a workflow-level or job-level `env:`, and never a
`docker context`:

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

**The runner is persistent and SHARED.** Unlike a GitHub-hosted runner, this
machine is reused by later jobs — including `trc-open-webui` and `paperclip`'s
deploys, which can run **concurrently** with this one on the same `$HOME`.
That drove two different fixes, because the two shared files carry different
risk:

- **The private key is a hard collision, so it is structurally isolated.** It
  is written to `$RUNNER_TEMP/id_rsa` — **never** `~/.ssh/id_rsa` — and loaded
  into a per-job `ssh-agent` whose socket is exported via `$GITHUB_ENV` for
  every later step in that job. A shared `~/.ssh/id_rsa` would let one job's
  cleanup (`rm -f ~/.ssh/id_rsa`) delete the key a sibling job is mid-deploy
  with; per-job `$RUNNER_TEMP` isolation makes that impossible, since
  `$RUNNER_TEMP` is scoped to the individual job. The agent authenticates both
  the explicit `ssh`/`scp` calls (which also pass `-i "$RUNNER_TEMP/id_rsa"`
  explicitly, redundantly with the agent, since that costs nothing) and the
  `ssh` the Docker CLI spawns internally for `DOCKER_HOST`. The key file and
  the agent are both removed in the final `Clean up secrets on the runner`
  step, which runs with `if: always()` — including the path where an earlier
  step failed before the agent ever started.
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
- Every other secret this workflow writes to disk on the runner —
  `.env.staging`, the `hermes.fpr` fingerprint temp file — is likewise
  removed in the final cleanup step.

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
is violated — only `known_hosts` is at risk.

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

The workflow builds and publishes two tags on every dispatch, but they play
different roles:

- `ghcr.io/nature-technologies/trc-hermes-agent:staging` — a **moving**
  pointer, overwritten by every run. Pushed for human convenience (e.g.
  browsing the GHCR package), but **nothing in this workflow ever deploys
  it.**
- `ghcr.io/nature-technologies/trc-hermes-agent:git-<7-char-sha>` — an
  **immutable** tag naming the exact commit that was built. **This is what
  gets deployed.** The `Render the environment file` step sets
  `DEPLOY_IMAGE: ${{ steps.tags.outputs.sha }}` in its own `env:` and writes
  that value into `.env.staging` as `HERMES_IMAGE`, so the deploy runs exactly
  what this run built — "what is running" is unambiguous, and never depends on
  `:staging` having been overwritten by a later, unrelated run between build
  and deploy.

1. Actions → **TRC staging deploy (hermes-agent)** → Run workflow.
2. Leave `bootstrap` unchecked (default `false`) unless this is the first
   deploy to a brand-new host.
3. The workflow builds the image from the checked-out ref, pushes both tags,
   and deploys the `:git-<7-char-sha>` one it just pushed.

The `Pull and deploy` step logs the deployed tag by reading it back out of
`.env.staging` (`grep '^HERMES_IMAGE=' .env.staging`) rather than
recomputing it, so the log line can never drift from what is actually
running — including under the rollback override below.

### Rolling back

**There is no digest input.** Because every routine deploy already runs the
immutable tag it just built, rolling back to an *older* build means
re-dispatching the workflow from a branch where the **`Render the environment
file`** step — not `Pull and deploy` — has its `DEPLOY_IMAGE` env line
hardcoded to an older tag instead of the dynamic
`${{ steps.tags.outputs.sha }}` expression:

1. Branch off the current `dev` (name it anything that is not `dev`, e.g.
   `rollback/2026-07-27`).
2. In `.github/workflows/trc-staging-deploy.yml` on that branch, find the
   `Render the environment file` step and change its
   `DEPLOY_IMAGE: ${{ steps.tags.outputs.sha }}` line to a hardcoded
   `DEPLOY_IMAGE: ghcr.io/nature-technologies/trc-hermes-agent:git-<short-sha>`
   — pick the short sha from a previous run's logs or the GHCR package's tag
   list. Commit and push the branch.
3. Actions → **TRC staging deploy (hermes-agent)** → Run workflow, and select
   **that branch** as the workflow ref (the "Use workflow from" selector). The
   deploy is `workflow_dispatch`-only, so it runs the workflow definition from
   whichever ref you pick.
4. Delete the branch once you are done. To roll forward again, dispatch the
   deploy from `dev` as normal, which builds fresh and deploys its own new
   `:git-<sha>`.

Because build and deploy are unified, a rollback dispatch **still rebuilds and
pushes fresh `:staging`/`:git-<newsha>` tags from that branch's source** — but
with `DEPLOY_IMAGE` hardcoded as above, the deploy step itself pulls and runs
the specific **older** tag you named, not the one it just built. A plain
re-dispatch from an old branch, without that edit, is not itself a rollback —
it would build and deploy a fresh image from old source under a new sha.

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
- `DOCKER_HOST` appears only as **step-level** `env:`, never at workflow or
  job level — either would apply to the build step too;
- `DOCKER_HOST` is present, specifically, on each of the `Verify host
  preconditions`, `Pull and deploy` and `Smoke test` steps by name — not just
  "at least one step has it" (which would let it silently go missing from any
  one of the three while the others stay green);
- the step running `docker/build-push-action` has no `DOCKER_HOST` in its own
  `env:` — it must build on the runner, not the deploy host;
- no line writes raw `ssh-keyscan` output directly onto `known_hosts` outside
  `$RUNNER_TEMP`, whether via a bare `>` or piped through `tee` — either
  bypasses the `ssh-keygen -R` stale-entry removal on a file that persists
  across jobs on this runner;
- the private key is never written to `~/.ssh/id_rsa` anywhere in the
  workflow — only under `$RUNNER_TEMP`, so sibling jobs on the same runner
  can never collide over it;
- an `if: always()` cleanup step exists that removes `id_rsa` and
  `.env.staging` and runs `docker logout`;
- `docker volume create` is only reachable from inside a branch whose
  **enclosing** `if`/`elif` condition tests the `bootstrap` input — checked
  with an if/elif/else/fi-aware scan, not merely "does `bootstrap` appear
  earlier in the step", so an unconditional create placed *after* that
  branch's `fi` (i.e. no longer actually gated by anything) is still
  rejected;
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
