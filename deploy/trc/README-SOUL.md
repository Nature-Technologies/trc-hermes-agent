# `SOUL.md` — the TRC system prompt, as Hermes loads it

**Do not edit `SOUL.md` here.** It is a byte-for-byte copy of the ```text block in
`trc-backend/integrations/openwebui/SYSTEM_PROMPT.md`, which is where the prompt is
reviewed (it is a security control, and belongs beside the redaction filter).
`scripts/check_soul_matches_backend.py` fails when the two differ; after editing the
backend file, run it with `--write`.

## Why it is a file here at all

Hermes assembles its system prompt as identity → guidance → ephemeral. `SOUL.md` is the
**identity** tier, so it is the first thing the model reads. Open WebUI's model system
prompt arrives as the *ephemeral* tier and is concatenated last, after ~15 K characters of
Hermes persona — which is where the TRC rules sat until H2 (2026-09-19).

## Three ways it silently fails to load — all three happened

Each of these leaves the model with the stock *"You are Hermes Agent … created by Nous
Research"* identity and **no TRC rules at all**, and nothing errors:

1. **Not mounted.** The container reads `$HERMES_HOME/SOUL.md` = `/opt/data/SOUL.md`.
   Uploading the file to the host is not enough; the compose file must bind-mount it
   there (`HERMES_SOUL_PATH`). Until 2026-09-23 only `config.yaml` was mounted.
2. **Shadowed by the image's default.** `docker/stage2-hook.sh` seeds the image's own
   `docker/SOUL.md` into the volume on first boot and never overwrites it. The bind mount
   is what wins over that seeded copy; an upload into the volume would not.
3. **Blocked by Hermes' own injection scanner.** Every context file passes through
   `tools/threat_patterns.py` first, and ANY match replaces the whole file with
   `[BLOCKED: …]`. The first deployed copy tripped it twice: an HTML comment header
   (`html_comment_injection`), and a security rule that quoted an injection phrase in
   order to forbid it (`prompt_injection`). So: no HTML comments, and describe an attack
   rather than writing its words.

`tests/test_trc_soul_loads.py` covers #3 against the real scanner. The deploy's smoke
step covers #1 and #2 by reading the prompt the RUNNING container assembles.
