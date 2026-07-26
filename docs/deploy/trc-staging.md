# TRC staging deploy — hermes-agent

Deploys this fork's image to the TRC staging host as its own compose project,
`trc-staging-hermes-agent`. This repo owns only hermes-agent; `trc-open-webui`
and `paperclip` deploy themselves the same way, and all three attach to the
shared external `poc-net` bridge.

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

## Editing Hermes config

`deploy/trc/config.yaml` is copied to the host on every deploy, so it is
git-managed: **editing it on the server is overwritten by the next deploy.**
Change it here and re-dispatch.

## Checks

`deploy/trc/validate_compose.py` asserts the invariants that make three
independent compose projects add up to one stack — above all `external: true`
on every network and volume. It runs in the **TRC deploy checks** workflow and
under `pytest tests/tools/test_trc_deploy_compose.py`. Read its docstring
before changing the compose file.
