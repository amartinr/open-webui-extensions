# HANDOFF — Bifrost/DeepSeek reasoning loss in Open WebUI

> Written for the next agent to pick up this work without redoing the whole
> investigation. Read this before touching anything.

## Context

Deployment: **Open WebUI** (>= 0.11) as an OpenAI-compatible client → **Bifrost**
gateway (`http://bifrost.private/v1`) → **DeepSeek v4 flash/pro**.

Two Open WebUI extensions in this repo are involved:

- `filters/bifrost_reasoning_content_fix` — converts Bifrost's non-standard
  `reasoning` / `reasoning_details` response fields into standard
  `reasoning_content` (stream/outlet), cleans history on the way in (inlet).
- `pipes/agent_loop_guard` — gateway proxy pipe (tool-loop guard + SSE
  forwarding). Workspace models point at its sub-pipes
  (`agent_loop_guard.deepseek/deepseek-v4-flash`).

Reference implementation that WORKS in production: the pi extension
`@amartinr/pi-bifrost-reasoning-fix` (repo at `../pi-bifrost-reasoning-fix`),
which only registers `before_provider_request` and normalizes the provider
payload (rename residue → `reasoning_content`, force `reasoning_content: ""`
on every assistant once tools/tool-call history is in scope). Its
`after_provider_response` is a no-op.

## Symptom (the user's complaint)

"Sometimes the reasoning is lost." Observed both with and without tool calls,
sporadically across turns (not on the first turn). The user reports pi never
loses reasoning with the same model + Bifrost.

## What was done this session (commits on master)

- `5a1b6c1` — ported the pi fix to both extensions:
  - filter `inlet` forces `reasoning_content` (empty) on all assistants when
    `tools` present or history has tool_calls (filter v3.2.0);
  - pipe normalizes every outbound payload the same way (pipe v2.7.0) — the
    pipe is the only hop that also sees tool-call continuations, which bypass
    filter inlets in Open WebUI (`convert_output_to_messages` rebuilds the
    tool-calling assistant without `reasoning_content` and
    `utils/chat.generate_chat_completion` is called directly).
- v2.8–v2.10 / v3.3: added `bf-reasoning:` diagnostic logs (message shape
  summary, response delta counts, payload params).
- `f683c7d` v3.4.0: tried keeping `reasoning_details` in stream deltas so the
  replayed history carries the real reasoning text — **BROKE SSE** (see
  lesson #1). Reverted in `2e07a3e` v3.5.0.
- `5cb758f` v2.11.0: added a `REASONING_EFFORT` valve that upgraded
  `reasoning_effort` low→high for deepseek — **user rejected it** (see lesson
  #2). Reverted in `3b31ef4`; pipe restored byte-identical to v2.10.0
  (`bbd7980`).

## Current state

- `master` = filter **v3.5.0** + pipe **v2.10.0** (forcing fix + diagnostic
  logs, no config overrides).
- `baseline-pre-session` tag = `d8dae1b` (filter v3.1.0 + pipe v2.6.0), the
  exact code the user had deployed before this session.
- Net code delta baseline→master: ONLY the reasoning_content forcing
  (inlet + pipe), residue-detection alignment, one stream edge-case fix, and
  logs. `stream()` behavior is unchanged vs v3.1.0.

## Empirical findings (measured against the live Bifrost endpoint)

All measurements: turn-2 payloads with a prior assistant message, stream=true,
tools in payload, thinking enabled, various efforts. The endpoint is live at
`http://bifrost.private/v1`; the API key is available from the session env
(`BIFROST_*`) — do NOT commit it.

| Config (turn 2, tools + thinking + history) | Reasoning |
|---|---|
| `reasoning_effort: "low"` (the user's Open WebUI model params) | **0/12, 0/8** (never) |
| `reasoning_effort: "high"` (pi sends this; `PI_REASONING_LEVEL=high`) | 5–6/12 |
| `reasoning_effort: "max"` | 7/8 |
| No `tools` in payload | **12/12, 8/8, 8/8** |
| `high` + system prompt demanding step-by-step reasoning | **12/12** |
| Trivial prompt without tools | ~50% (model choice) |

Conclusions:
1. **`tools` in the payload is the main suppressor** of reasoning on turns with
   history (with tools ~30–60%, without tools ~100%).
2. `reasoning_effort: "low"` + `tools` + `thinking: enabled` + history ≈ never
   reasons. Higher effort (high/max) mitigates but does not fully fix it.
3. Turn 1 always reasons (no history) regardless of the above — this is why
   "low always reasons" was true in the user's experience (first turn only).
4. The remaining variance is DeepSeek's own decision, not the pipeline: even
   the exact pi payload (real text + thinking + high) measured ~4/8 on trivial
   follow-ups. pi "always works" mainly because its tasks demand reasoning and
   its system prompt says so, plus `PI_REASONING_LEVEL=high`.
5. The tool-call deterministic case IS fixed: history with `tool_calls` and an
   assistant without `reasoning_content` loses reasoning 100% of the time;
   with `reasoning_content` (even `""`) it reasons ~5/5. This is what the
   forcing (v3.2.0/v2.7.0) fixes and is the part that matches the pi fix.

## Lessons (do not repeat)

1. **Do not leave `reasoning_details` in stream deltas.** Open WebUI's
   `streaming_chat_response_handler` suppresses the frontend
   `response.reasoning_text.delta` event whenever a delta carries
   `reasoning_details` (it sets `data=None` and only saves to DB). Keeping them
   = streaming display breaks. This is why preserving the real reasoning text
   via the stream is not possible without Open WebUI core changes.
2. **Never override the user's `reasoning_effort`.** The user sets it in Open
   WebUI model params and considers it a hard requirement. Any fix must
   respect it. (The v2.11.0 valve was reverted for this reason.)
3. Open WebUI executes function code pasted in Admin → Functions (its DB), not
   the git repo. Deploying = re-pasting the file content + restart if the
   stream() code changed (module cache is per-process).

## Open questions / next steps

- Whether the user keeps the forcing fix (master) or reverts to
  `baseline-pre-session` is undecided. The user leans toward "we added cruft,
  we're at the same point" — be ready to produce a **minimal, no-logs version**
  (only the forcing, no `bf-reasoning:` logs) if asked.
- If "before always reasoned" is confirmed against the baseline, compare
  payloads: the forcing adds `reasoning_content: ""` where the baseline sent
  nothing — this changes the history DeepSeek sees. Whether that affects the
  stochastic behavior has NOT been conclusively isolated.
- The system prompt is the only lever measured to make reasoning consistent
  WITH tools (12/12) — candidate for an opt-in valve (`REASONING_PROMPT`,
  default off, injects a "reason step by step" system instruction for deepseek
  models), but only with explicit user approval.

## Useful commands

```bash
# live A/B against Bifrost (repro of the low-vs-high finding):
# turn-2 payload: [system, user, assistant(reasoning_content:""), user], tools, thinking, stream
# flip reasoning_effort low/high and count reasoning deltas in the SSE
git tag                    # baseline-pre-session
git diff baseline-pre-session master -- filters/ pipes/   # net changes
```

## Testing

- `python3 -m pytest filters/bifrost_reasoning_content_fix/ pipes/agent_loop_guard/tests/`
- 56 tests pass on master. Live endpoint checks: see `../pi-bifrost-reasoning-fix/scripts/verify-fix.mjs` (uses `BIFROST_BASE_URL`/`BIFROST_API_KEY` env vars).
