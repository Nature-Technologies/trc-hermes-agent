# Agentic reports — Hermes companion

**Status:** proposal, 2026-09-19. Fleet design: `trc-orchastrator/docs/DESIGN-agentic-reports.md`.
Backend spec: `trc-backend/docs/superpowers/specs/2026-09-19-agentic-reports-design.md`.
Separate from `FIXES-2026-09-19.md`; depends on H1, H2, H3 from it.

Hermes is where the loop runs. Nothing here adds tools; the backend adds them. This
repo supplies the mode, the budget, the prompt, and the session behaviour.

---

## 1. Report mode as a model route

`platforms.api_server.extra.model_routes` (see `gateway/platforms/api_server.py:1428`):

```yaml
model_routes:
  trc-assistant:            # chat, unchanged behaviour
    model: "<chosen>"
    provider: openrouter
  trc-report:               # report mode
    model: "<chosen, may differ>"
    provider: openrouter
```

Open WebUI exposes both as models; the user picks "Report" or the assistant
switches when the request is a report (decision D-R3: start explicit, merge later).
Per-route overrides needed and not yet supported by `model_routes`: `max_turns`,
prompt section, compression policy. Add a `route_overrides` block read in
`_create_agent` alongside `route`.

## 2. Budget

| setting | chat | report |
|---|---|---|
| `agent.max_turns` | 12 (FIXES H4) | 40 |
| `tool_loop_guardrails.hard_stop_enabled` | true | true |
| `hard_stop_after.same_tool_failure` | 4 | 6 |
| MCP `timeout` per call | 160 s | 160 s; aggregates may need 240 s |
| `gateway_timeout` (idle) | 1800 | 1800 |

Cost target set after the bake-off (fleet §8); enforce with `max_turns`, not hope.

## 3. Prompt

`deploy/trc/SOUL.md` (FIXES H2) gains a second block, selected by route:

- Chat block: current rules, shortened per FIXES B8.
- Report block: plan → infer scope → gather → compute with `calculate` → draft
  with `draft_put` → `render_report` → repair on `rejected` (max 2) → reply with
  title, `report_id`, sources used, assumptions. Hard rules retained: tokens and
  markers verbatim, figures only via tools, no URLs, security rules.

Keep both blocks short. The prompt must not enumerate tools; the MCP schemas do.

## 4. Session and context

- Stored history is load-bearing (FIXES H3). Report loops produce 30–60 tool
  results of up to 35 K chars each; `compression.threshold 0.50` must trigger and
  must not drop the current section's results. Verify on this path with a test
  that replays a 40-call transcript.
- Once a section is in `draft_put`, its gathering results can be collapsed to a
  one-line stub on compaction. Implement as a compaction hint keyed on tool name.
- Follow-up turns ("add a section on X") continue the same session; the draft is
  in the backend, the outline is in history.

## 5. Progress to the user

Existing `turn_status` frames (`gateway/platforms/turn_status.py`) carry interim
lines to Open WebUI. Report mode should emit one line per phase and per section
("Gathering Addepar 2024…", "Drafting section 3/6…", "Rendering…") rather than the
status-hint side-model text; disable `auxiliary.status_hints` on the report route to
avoid two competing status streams and the extra model calls.

## 6. Timeouts outside Hermes

The Open WebUI → Hermes request stays open for the whole loop. Confirm before R3:
Open WebUI's upstream client timeout for `OPENAI_API_BASE_URL`, and any reverse
proxy in front of `open-webui:8080`, allow ≥ 15 minutes with streaming. If not,
switch report mode to Hermes' Responses/background pattern and poll.

## 7. Toolset

Unchanged from FIXES H1: `platform_toolsets.api_server: [ragnarok]`. Report mode
adds nothing native. `execute_code`, `terminal`, `browser_*`, `web_*` stay off; the
backend's `calculate` and `render_*` cover the needs.

## 8. Tests to add

- `tests/gateway/`: route override applies `max_turns` and prompt block per route.
- Replay test: 40-call synthetic report transcript stays under the context window
  with compression on; last section's results intact.
- Status frames: one line per phase, none from status_hints on the report route.

## 9. Sequence

R3 in the fleet phasing: after backend R1–R2 land. Ship route + budget + prompt
block first with the backend's R1 tools; aggregates arrive with R2.
