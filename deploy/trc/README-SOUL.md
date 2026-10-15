# `SOUL.md` — the TRC system prompt, as Hermes loads it

**Do not edit `SOUL.md` here.** It is a byte-for-byte copy of the ```text block in
`trc-backend/integrations/openwebui/SYSTEM_PROMPT.md`, which is where the prompt is
reviewed (it is a security control, and belongs beside the redaction filter).
`scripts/check_soul_matches_backend.py` fails when the two differ; after editing the
backend file, run it with `--write`.

## Version

**v2, 2026-10-14: the answer-first prompt.** Written to trc-orchestrator spec
`docs/superpowers/specs/2026-10-14-answer-first-agent-design.md` §4 and §4.1. Its exact
text is contract C6, Appendix A of `docs/superpowers/plans/2026-10-14-answer-first-p0-p1.md`.
Against v1 (about 25,000 characters) it routes by the shape of the question, with
`resolve_entity` for names and `rank_and_count` for rankings, counts and rosters. Since
C6 amendment 3 (2026-10-15) `find_relationships` is who knows whom and last contact, and
a resolved record's owners, accounts and files go to `knowledge_graph` by its `ref`; a
script still reaches only the six tools its sentence names. It
speaks in a confident voice, ends each answer with a `trc:followups` block, and has no
skills or subagent sections, because the chat platform has no skill tool and no
`delegate_task` any more (contract C5b).

## Budget

`tests/test_trc_soul_budget.py` enforces each of these:

- At most **6,500 characters**. Another 1,500 are reserved for learned lessons (spec §8).
- `never` (whole word, any case) at most **10** times, kept for the security and token
  rules. Every other rule is a positive instruction with its reason.
- None of the hedge phrases the eval lint counts in answers (`evals/lint.py`, SOFTENERS
  and RETRY_OFFERS): "honestly", "I want to be straight", "unfortunately", "it seems",
  "I wasn't able", "I was not able", "I couldn't", "I could not", "try again".
- It names `resolve_entity` and `rank_and_count`, and no tool that is off the chat wire
  (`list_entities`, `meeting_briefing`, `skill_view`).
- **No line that starts with three backticks**, indented or tagged. A bare fence would
  close the backend doc's ```text block early, and the drift check would compare half a
  prompt; a tagged one would break that doc's rendering.
- **It ends with exactly one newline.** The drift check extracts the block with
  `^```text\n(.*?)^```$` (DOTALL, MULTILINE). What it compares runs from just after the
  opening fence's newline up to the closing fence, so it includes the last prompt line's
  newline and nothing after it. In the backend doc the closing fence goes on the line
  straight after the last prompt line, with no blank line between them.

`tests/test_trc_soul_loads.py` adds four more checks:

- The prompt starts with `RULE ZERO`, as the deploy smoke check also requires.
- It loads whole, with no scanner rewrite and no trim.
- It carries the contracts other components rely on: the three `resolve_entity`
  decisions `pick`, `each` and `ask` (contract C2), and the `trc:followups` tag that the
  backend filter and the Open WebUI fork parse (contract C3).
- Every route in it (`→` followed by a backticked tool name) is in
  `mcp_servers.ragnarok.tools.include`. The one exception is `execute_code`, which is
  not an MCP tool: it must instead come from the `code_execution` toolset in
  `platform_toolsets.api_server`.

`.gitattributes` pins this file to LF (`deploy/trc/SOUL.md text eol=lf`), so a Windows
checkout with `core.autocrlf=true` is LF too, and the raw bytes hash to the same sha256 on
every platform; `test_the_checkout_is_lf_with_no_bom` checks them. Keep the pin: the
sha256 both repos record is of LF text.

## Rollout: a new prompt reaches new chats only

Hermes stores a session's system prompt on its first turn. On every later turn it reuses
that prompt byte for byte, as long as the model and provider are unchanged
(`agent/conversation_loop.py`, `_stored_prompt_matches_runtime`). This keeps the prompt
cache valid, and it means a deploy of a new `SOUL.md` changes **new chats only**.

A chat started before the deploy keeps the old prompt, but its tool list is rebuilt
every turn. So a v1 chat can still be told to call `list_entities`, `skill_view` or
`delegate_task`, which are no longer on the wire. Hermes answers such a call with "Tool … does not exist"
and the list of tools that do, and the model usually corrects itself.

After a prompt deploy, test in a new chat, and ask users to start one. Spec §11.9 leaves
a prompt-version check, which would rebuild stale stored prompts, as a later option.

**Deploy together with the backend.** The backend staging compose sets
`QUERY_DATA_MODE_ENABLED: "true"` (contract C7): `query` returns passages and this
prompt's model writes the answer. SOUL v2 is written for that; SOUL v1 expected `query`
to return a generated `answer`. So publish this Hermes build (C2 + C3) together with the
backend, and have everyone who chatted before the deploy start a new chat: a chat that
began on v1 keeps v1 (the stored-prompt rule above). `"false"` in the backend compose is
the kill switch: every `query` then generates its own answer, as before. Check on the
backend host:

```bash
docker exec app python -c "from app.core.config import get_settings; print(get_settings().query_data_mode_enabled)"
```

## Why it is a file here at all

Hermes assembles its system prompt as identity → guidance → ephemeral. `SOUL.md` is the
**identity** tier, so it is the first thing the model reads. Open WebUI's model system
prompt arrives as the *ephemeral* tier and is concatenated last, after ~15 K characters of
Hermes persona — which is where the TRC rules sat until H2 (2026-09-19).

When delegation returns to chat (P2), subagents will read it too:
`delegation.load_soul: true` in `config.yaml` makes it their identity.

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
