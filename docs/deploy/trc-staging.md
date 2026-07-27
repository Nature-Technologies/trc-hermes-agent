# TRC staging deploy — hermes-agent

Deploys this fork's image to the TRC staging host as its own compose project,
`trc-staging-hermes-agent`. This repo owns only hermes-agent; `trc-open-webui`
and `paperclip` deploy themselves the same way, and all three attach to the
shared external `poc-net` bridge.

## Host prerequisites

The deploy creates **only** `poc-net`, idempotently. It creates neither
`trc-shared` nor `trc-staging-hermes-memory`, and it fails if either is
missing:

- **`trc-shared`** is owned by whoever runs the trc-backend stack. The
  `Copy deploy artifacts` step fails early if it is absent, before any secret
  is written to the host. Create it on the backend side, not here.
- **`trc-staging-hermes-memory` must already exist and already hold Hermes'
  data.** The deploy deliberately does not run `docker volume create`: the
  volume is declared `external: true` precisely so Compose refuses to start
  without it, and pre-creating it would boot Hermes against a silently *empty*
  volume — no sessions, no memories, no cron — with every smoke test still
  passing. See Phase 2 preconditions below.

The deploy user also needs all of:

- **write access to `/srv/trc`** (the deploy `mkdir -p`s
  `/srv/trc/staging/hermes-agent` and `/srv/trc/staging/fingerprints`);
- **membership of the `docker` group**, so `docker` works without `sudo`;
- **permission to create `/var/lock/trc-deploy.lock`.** `/var/lock` is
  root-owned `0755` on some images, in which case `flock` fails *after* the
  `.env` and the key fingerprint have already been copied to the host. Either
  grant write access to `/var/lock` or pre-create the lock file owned by the
  deploy user.

### Phase 2 preconditions

This repo's only stateful dependency is `trc-staging-hermes-memory`. Before the
first deploy, with the retired single-compose stack **stopped**, create the
volume and copy the old project-prefixed volume's contents into it — the old
name is the project-prefixed one Compose generated
(e.g. `trc-docker-paperclip-hermes_hermes-memory`), not `hermes-memory`. Copy
with the stack down so nothing is writing mid-copy. The deploy will refuse to
run until the volume exists.

## Running a deploy

1. Find the digest to deploy:
   `docker buildx imagetools inspect ghcr.io/nature-technologies/trc-hermes-agent:<tag> --format '{{.Manifest.Digest}}'`
2. Actions → **TRC staging deploy (hermes-agent)** → Run workflow → paste the
   `sha256:...` digest.

Deploys are digest-pinned, never tag-based, so a run is reproducible. **To roll
back, re-dispatch with the previous digest** — the deploy step prints the
currently-running digest before replacing it, so every run log contains its own
rollback target.

## Required repository secrets (environment: `staging`)

| Secret | Notes |
|---|---|
| `TRC_SSH_HOST`, `TRC_SSH_USER`, `TRC_SSH_KEY` | Deploy user and its private key |
| `TRC_SSH_KNOWN_HOSTS` | Pinned host key. The workflow fails if empty — `StrictHostKeyChecking` is never disabled |
| `TRC_SSH_PORT` | Optional, defaults to 22 |
| `OPENROUTER_API_KEY` | |
| `HERMES_API_KEY` | **Shared value.** Must be identical to `trc-open-webui`'s copy and to Paperclip's third copy in its instance `config.json`. Must be ≥16 characters — below that the gateway refuses to start the API server, so the symptom is connection-refused on :8642, not a 401 |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`, `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | Without these the dashboard is unreachable |
| `GHCR_READ_TOKEN` | Optional. Only needed if the GHCR package is private |

Every secret rendered into the host `.env` is charset-guarded: the workflow
rejects any value containing `$`, a backtick or `#`. Compose's `env_file` parser
interpolates the first two and treats `#` as a comment, so such a value would
reach the container as a *different* string with nothing erroring — a mangled
`HERMES_API_KEY` looks like a 401, a mangled dashboard password like a wrong
password. `openssl rand -hex 32` never produces any of them.

## Editing Hermes config

`deploy/trc/config.yaml` is copied to the host on every deploy, so it is
git-managed: **editing it on the server is overwritten by the next deploy.**
Change it here and re-dispatch.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — above all `external: true`
on every network and volume, plus each **service's** own `networks:` membership,
since a top-level network no service joins is silently ignored. It also bans two
things in the deploy workflow that would each fail open silently: any
`docker volume create` (see Host prerequisites), and a `flock -c` string that
does not begin with `set -e` — the `-c` string is a separate shell, so without
it a failed `pull` is ignored and `up -d` redeploys the old image while the run
goes green. It runs in the **TRC deploy checks** workflow and under
`pytest tests/tools/test_trc_deploy_compose.py`. Read its docstring before
changing the compose file or the deploy workflow.
