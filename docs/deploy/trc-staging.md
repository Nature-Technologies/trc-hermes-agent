# TRC staging deploy — hermes-agent

Deploys this fork's image to the TRC staging host as its own compose project,
`trc-staging-hermes-agent`. This repo owns only hermes-agent; `trc-open-webui`
and `paperclip` deploy themselves the same way, and all three attach to the
shared external `poc-net` bridge.

The deploy runs `docker compose` **from the GitHub Actions runner**, against
the host's Docker daemon over a remote Docker context
(`docker context create ... --docker "host=ssh://${USERNAME}@${HOST}"`). It
never opens an interactive SSH shell on the host to run compose commands.
Two consequences of that:

- The rendered `.env.staging` file is read by the **local** Compose CLI via
  `--env-file` and is never copied to the server. Its values reach the
  containers as container environment, sent over the Docker API through the
  SSH tunnel — there is no plaintext secret file on the staging host.
- Bind-mount sources (`config.yaml`) are resolved by the **remote** daemon, so
  that path must be absolute and must already exist on the server before
  `compose up` runs. The deploy still `scp`s `config.yaml` into place for this
  reason — it is the one file that genuinely has to reach the host.

## Host prerequisites

The deploy creates **only** `poc-net`, idempotently. It creates neither
`trc-shared` nor `trc-staging-hermes-memory`, and it fails if either is
missing:

- **`trc-shared`** is owned by whoever runs the trc-backend stack. The
  `Verify host preconditions` step fails early if it is absent, before any
  secret is rendered. Create it on the backend side, not here.
- **`trc-staging-hermes-memory` must already exist and already hold Hermes'
  data.** The deploy deliberately does not run `docker volume create`: the
  volume is declared `external: true` precisely so Compose refuses to start
  without it, and pre-creating it would boot Hermes against a silently *empty*
  volume — no sessions, no memories, no cron — with every smoke test still
  passing. See Phase 2 preconditions below.

The deploy user (`USERNAME`) also needs:

- **write access to `HOST_DIR`** (`/srv/trc/staging/hermes-agent`, where
  `config.yaml` is copied) and to `/srv/trc/staging/fingerprints`;
- **membership of the `docker` group** (or root) on the host, so the SSH
  session backing the Docker context can reach the daemon socket without
  `sudo`.

### Phase 2 preconditions

This repo's only stateful dependency is `trc-staging-hermes-memory`. Before the
first deploy, with the retired single-compose stack **stopped**, create the
volume and copy the old project-prefixed volume's contents into it — the old
name is the project-prefixed one Compose generated
(e.g. `trc-docker-paperclip-hermes_hermes-memory`), not `hermes-memory`. Copy
with the stack down so nothing is writing mid-copy. The deploy will refuse to
run until the volume exists.

## Running a deploy

The workflow tracks a **moving tag**, not a digest. `trc-publish.yml` builds
and publishes `ghcr.io/nature-technologies/trc-hermes-agent:dev` (plus an
immutable `sha-<short-sha>` tag) on every push to `dev` (and to
`chore/new-ci`, while that branch carries CI on its own). The deploy workflow's
`env:` block pins which tag it rolls out:

```yaml
env:
  IMAGE_TAG: dev
```

Change that one line to follow a different branch tag; nothing else in the
workflow needs to change.

1. Actions → **TRC staging deploy (hermes-agent)** → Run workflow.
2. The workflow pulls `${IMAGE_REPO}:${IMAGE_TAG}` and deploys it.

**There is no digest input.** To roll back, point `IMAGE_TAG` at a
`sha-<short-sha>` tag for the build you want (visible in the `trc-publish.yml`
run history or in the GHCR package's tag list) and re-run the deploy workflow.
The `Pull and deploy` step still prints the digest that actually landed, so
every run log records what is now running, but a re-dispatch — not a digest
input — is how you go back.

## Required repository secrets (environment: `Staging`)

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY_DEV` | Deploy user's private key |
| `HOST` | Staging host, used both for `ssh-keyscan` and as the Docker context target (`ssh://${USERNAME}@${HOST}`) |
| `USERNAME` | Deploy user on the host |
| `TRC_SSH_PORT` | Optional, defaults to 22 |
| `OPENROUTER_API_KEY` | |
| `HERMES_API_KEY` | **Shared value.** Must be identical to `trc-open-webui`'s copy and to Paperclip's third copy in its instance `config.json`. Must be ≥16 characters — below that the gateway refuses to start the API server, so the symptom is connection-refused on :8642, not a 401 |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`, `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Without these the dashboard is unreachable |

`TRC_SSH_KNOWN_HOSTS` no longer exists as a secret. Host keys are **scanned at
deploy time** (`ssh-keyscan -H "$HOST" > ~/.ssh/known_hosts`) rather than
pinned in advance:

- This still protects against a passive attacker who cannot intercept the very
  first connection of a run — `StrictHostKeyChecking` is never disabled, so if
  the host key changes *after* the scan (e.g. mid-run, or on a subsequent run
  against a key that was swapped since the last scan and cached nowhere) the
  connection still aborts rather than silently trusting a new key.
- It does **not** protect against an active machine-in-the-middle present at
  the moment `ssh-keyscan` runs, since there is no prior pinned key to compare
  against — trust-on-first-use accepts whatever key answers on that first
  connection. This is a deliberate trade against the operational cost of
  maintaining a `TRC_SSH_KNOWN_HOSTS` secret in step with any host-key
  rotation; the previous pinned-key model traded the other way.

Every secret rendered into `.env.staging` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring — a mangled
`HERMES_API_KEY` looks like a 401, a mangled dashboard password like a wrong
password. `openssl rand -hex 32` never produces any of them. `.env.staging`
itself is written on the runner and passed to Compose with `--env-file`; it is
never copied to the host.

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

- the deploy runs against a remote Docker context (`docker context create`),
  never a raw SSH shell — that is what keeps the rendered `.env.staging` off
  the server;
- the context target comes from the `HOST` and `USERNAME` secrets, not a
  literal hostname;
- Compose is invoked with `--env-file`, so secret values are never
  interpolated into a shell command string;
- `compose pull` precedes `compose up -d`, so a deploy can never silently redeploy an image already on the host;
- no `docker volume create` (see Host prerequisites) — Compose must fail
  closed on the missing external volume, not boot against a silently empty
  one.

It runs in the **TRC deploy checks** workflow and under
`pytest tests/tools/test_trc_deploy_compose.py`. Read its docstring before
changing the compose file or the deploy workflow.
