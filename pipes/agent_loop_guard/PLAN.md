# Implementation Plan: Agent Loop Guard

Phased delivery — each phase produces a **working, uploadable pipe** that
is strictly more capable than the last.

## Progress

- [x] Phase 1 — Transparent Manifold Proxy
- [x] Phase 2 — Runaway Protection
- [x] Phase 3 — Loop Detection & Escalation
- [x] Phase 4 — Tool Counter & Injection Options
- [ ] Phase 5 — Polish & Production Readiness
- [ ] Phase 6 — Tool Allowlist / Blocklist

## Phase overview

| Phase | What it does | Status |
|-------|-------------|:------:|
| 1 | Transparent manifold proxy | ✅ Done |
| 2 | + Runaway limit (`MAX_TOOL_CALLS_PER_TURN`) + soft-block | ✅ Done |
| 3 | + Consecutive duplicate detection + escalation + selective tool removal | ✅ Done |
| 4 | + Descending tool counter + `merge_last_tool` injection + shortened messages | ✅ Done |
| 5 | Polish: logging, hardening, tests | ⬜ Pending |
| 6 | + Tool allowlist/blocklist (remove tools by name) | ⬜ Pending |

---

## Phase 1 — Transparent Manifold Proxy  [x]

**Goal**: a pipe that queries the gateway, creates one sub-pipe per model,
and forwards requests transparently. Zero analysis — pure proxy.

### Files

```
pipes/agent_loop_guard/
├── DESIGN.md
├── PLAN.md            ← this file
└── agent_loop_guard.py
```

### Scope

| Concern | Included | Deferred |
|---------|:--------:|:--------:|
| `Valves` with `GATEWAY_BASE_URL` + `GATEWAY_AUTH_VALUE` | ✅ | |
| `pipes()` — dynamic model discovery via `GET /models` | ✅ | |
| Cache with fallback on gateway failure | ✅ | |
| `pipe()` — strip prefix, forward to gateway | ✅ | |
| Streaming proxy (`_stream`) | ✅ | |
| Non-streaming proxy (`_call`) | ✅ | |
| Error handling on gateway calls | ✅ | |
| Frontmatter with metadata + requirements | ✅ | |
| Tool-call analysis | | Phase 2–3 |
| Warnings / escalation / soft-block | | Phase 2–3 |
| Tool counter / injection options | | Phase 4 |

### `pipe()` logic (Phase 1)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. payload = {**body, "model": real_model}
  4. Forward to {GATEWAY_BASE_URL}/chat/completions
  5. Stream or non-stream as requested
```

### Validation

- Upload to Open WebUI → models appear in selector with 🛡️ prefix.
- Select a protected model → chat works exactly like the bare model.
- Gateway down during `pipes()` → cached models still shown.
- Gateway down during `pipe()` → error string returned.
- Stream and non-stream both work.

---

## Phase 2 — Runaway Protection  [x]

**Goal**: add `MAX_TOOL_CALLS_PER_TURN` — soft-block when the agent
makes too many tool calls in one turn. Simplest protection, highest ROI.

### New code

- `MAX_TOOL_CALLS_PER_TURN` valve.
- `_extract_tool_calls_in_turn()` — scan backwards from end of messages
  until the last `role: "user"`, collect all `tool_calls`.
- In `pipe()`: if `len(history) >= MAX_TOOL_CALLS_PER_TURN`, soft-block
  (remove all tools, inject instruction, forward to gateway).

### `pipe()` logic (Phase 2)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. history = _extract_tool_calls_in_turn(messages)
  4. if len(history) >= MAX_TOOL_CALLS_PER_TURN > 0:
       _soft_block(all tools removed, instruction)
  5. Forward to gateway as before
```

### Soft-block strategy

Soft-block removes `tools` from the request body and injects a system
message instructing the LLM to summarise. All tool results already in
messages are preserved. The LLM receives them plus the instruction, but
has no tools available — forced to respond with text.

| Case | What is removed | Behaviour |
|:----:|:---------------:|-----------|
| **Runaway** (total ≥ MAX) | All tools | `body.pop("tools", None)` |
| **Loop** (escalation ≥ max) | Only the looping tool | Filter `body["tools"]` by name |

### Validation

- Set `MAX_TOOL_CALLS_PER_TURN=3`. Run a tool-using agent.
- ≤3 tool calls → normal forward with tools.
- 4th+ tool call → body has `tools` removed, system msg injected,
  forward to gateway. LLM responds with text, no more tool calls.
- Set `MAX_TOOL_CALLS_PER_TURN=0` → feature disabled, no limit.
- With `__event_emitter__`: toast notification + status indicator.
- Without `__event_emitter__`: soft-block still works (no crash).

---

## Phase 3 — Loop Detection & Escalation  [x]

**Goal**: detect consecutive identical tool calls, escalate through
`WARNING` → `FINAL WARNING` → selective tool removal (only the looping
tool is removed, others remain available).

### New code

- `_has_consecutive_duplicates()` — checks if the last N tool calls in
  the turn are identical (same name + same args).
- `_escalation_level()` — scans system and tool messages for
  `[GUARD_WARN]` / `[GUARD_FINAL]` markers to deduce current level.
- `_inject()` — inserts guard messages at configured position
  (see Phase 4 for injection options).
- Warning/final-warning message templates (short, concise).

### Guard messages

```
[GUARD_WARN] Tool call repeated. No progress. Summarize or change approach.
[GUARD_FINAL] Still repeating. Stop now and summarize what you have.
```

### Escalation ladder

```
Level 0: Silent monitoring
  │  Consecutive duplicate tool calls detected
  ▼
Level 1: [GUARD_WARN] (tools still available)
  │  Agent ignores → continues looping
  ▼
Level 2: [GUARD_FINAL] (tools still available)
  │  Agent still ignores
  ▼
LOOP SOFT-BLOCK: only the offending tool removed from body["tools"]
  Agent can still use other tools or summarise.
```

### `pipe()` logic (Phase 3)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. history      = _extract_tool_calls_in_turn(messages)
  4. escalation   = _escalation_level(messages)
  5. max_esc      = MAX_WARNINGS_BEFORE_TERMINATE

  6. if len(history) >= MAX_TOOL_CALLS_PER_TURN > 0:
       _soft_block(None)       ← removal all tools (runaway)

  7. loop = _has_consecutive_duplicates(history, MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING)

  8. if loop:
       if escalation >= max_esc → _soft_block(bad_tool)  ← remove only looping tool
       elif escalation == 1     → inject FINAL WARNING (tools still available)
       else                     → inject WARNING (tools still available)

  9. Forward to gateway
```

### Validation

- `threshold=2, max_warnings=2, max_tool_calls=15`
- Turn: `[search("X"), search("X")]` → loop detected, escalate=0 → inject WARNING
- Turn: `[search("X")×3]` → loop detected, escalate=1 → inject FINAL WARNING
- Turn: `[search("X")×4]` → loop detected, escalate=2 ≥ max_warnings → **soft-block** (only `search` removed from tools, forward to gateway)
- Turn: `[search("X"), search("Y")]` → **no** loop (different args).
- Turn: `[search("X"), fetch("X")]` → **no** loop (different tool).
- `MAX_WARNINGS_BEFORE_TERMINATE=0` → soft-block on first loop detection.
- `MAX_WARNINGS_BEFORE_TERMINATE=1` → warn once, then soft-block on next duplicate.

---

## Phase 4 — Tool Counter & Injection Options  [x]

**Goal**: add a descending tool call counter to every tool result so the
agent always knows its remaining budget. Provide configurable injection
positions for guard messages.

### New code

- `SHOW_TOOL_COUNTER` valve (default `True`) — append
  `remaining tool calls: N` to every tool result.
- `_append_tool_counter()` — finds the last tool result in the current
  turn and appends the counter after a `---` separator.
- `INJECTION_POSITION` valve (default `"append_user"`):
  - `"append_user"`: inject guard message as a new `system` message
    before the last `user` message.
  - `"merge_last_tool"`: append guard message to the last tool result
    in the current turn, after a `---` separator and the counter.
- `_escalation_level()` updated to scan both `system` and `tool`
  messages for markers (needed for `merge_last_tool`).

### Tool counter format

```
result of web_search...

---
remaining tool calls: 14
```

### Example with counter + warning (merge_last_tool)

```
result of web_search...

---
remaining tool calls: 12

[GUARD_WARN] Tool call repeated. No progress. Summarize or change approach.
```

### Soft-block messages (short)

| Type | Message |
|:----:|---------|
| Runaway | `TOOL LIMIT: {total}/{max} used. No more tools this turn. Summarize now.` |
| Loop (tool removed) | `TOOL REMOVED: {tool_name} blocked after repeated identical calls. Other tools still available. Summarize or continue.` |

### Validation

- `SHOW_TOOL_COUNTER=True` → counter appended to every tool result.
- `SHOW_TOOL_COUNTER=False` → no counter.
- `MAX_TOOL_CALLS_PER_TURN=0` → counter not shown (no limit).
- `INJECTION_POSITION=append_user` → warning is a new `system` message.
- `INJECTION_POSITION=merge_last_tool` → warning appended to last tool result.
- Both counter and warning can coexist in the same tool result.

---

## Phase 5 — Polish & Production Readiness  [ ]

### Tasks

1. **Logging** — `import logging; log = logging.getLogger(__name__)`.
   Log model discovery, injections, force-terminations at INFO/DEBUG.
   → ✅ Done: 13 logging calls across the file.
2. **Gateway error messages** — return a human-readable string with the
   HTTP status code, not the raw exception.
   → ✅ Done: httpx.HTTPStatusError / httpx.RequestError handling.
3. **Model name fallback** — if gateway returns a model without `name`,
   use `id` as display name.
   → ✅ Done: `m.get('name', m['id'])` in `pipes()`.
4. **Test with multiple gateway providers** — LiteLLM, Bifrost, custom
   OpenAI-compatible proxies. → ⬜ Pending.
5. **Test edge cases from DESIGN.md §13** — all 9 cases. → ⬜ Pending.
6. **Replace `GATEWAY_HOST_HEADER` + `GATEWAY_HOST_VALUE` with single `GATEWAY_CUSTOM_HEADERS` valve** — the old two-valve pattern was specific to Bifrost's host-routing. The new valve accepts a JSON object so arbitrary headers (host routing, tracing, debug, etc.) can be added without adding a new valve per header. Backward-incompatible: existing installations must migrate their `GATEWAY_HOST_HEADER`/`GATEWAY_HOST_VALUE` into `GATEWAY_CUSTOM_HEADERS` as a JSON object (e.g. `{"x-bf-dim-host": "myhost"}`). → ✅ Done.

### Optional stretch goals (not in DESIGN.md)

- **`__event_emitter__` status pills** — emit `"status"` events during
  force-termination so the user sees "🛡️ Loop guard: stopped" in the UI.
  → ✅ Done: both notification and status events in `_soft_block`.

---

## Phase 6 — Tool Allowlist / Blocklist  [ ]

**Goal**: let the admin configure which tools the agent can see by name.
The pipe filters `body["tools"]` before forwarding, so the LLM never
knows the removed tools exist.

### Motivation

Open WebUI workspace models inherit all available tools. An admin may
want to:

- **Block** a dangerous tool (`terminal_execute`, `delete_file`)
- **Remove** expensive tools (`search_web`, `fetch_url`) from cheap models
- **Allowlist** only a small subset (e.g. `calculator` + `sql_query`)

A Filter cannot do this reliably — the tool list is reconstructed after
filter execution (see Bypass schema below). A Pipe can mutate
`body["tools"]` directly before forwarding, and the change is
**definitive**.

### New valves

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| `TOOL_BLOCKLIST` | str | `""` | Comma-separated tool names to **remove** from the agent's tool list |
| `TOOL_ALLOWLIST` | str | `""` | Comma-separated tool names — **only** these are kept, all others removed |

If both are set, allowlist wins (blocklist is ignored).
If neither is set, all tools pass through unchanged (current behaviour).

### `pipe()` logic (Phase 6)

After analysing tool calls (phases 2-4), before forwarding:

```
if TOOL_ALLOWLIST:
    allowed = {name.strip() for name in TOOL_ALLOWLIST.split(",")}
    body["tools"] = [
        t for t in body.get("tools", [])
        if t.get("function", {}).get("name") in allowed
    ]
elif TOOL_BLOCKLIST:
    blocked = {name.strip() for name in TOOL_BLOCKLIST.split(",")}
    body["tools"] = [
        t for t in body.get("tools", [])
        if t.get("function", {}).get("name") not in blocked
    ]
```

Additionally, if `tool_choice` targets a blocked tool, reset it:

```
# If tool_choice forces a now-removed tool, let the LLM decide
if "tool_choice" in body:
    tc_name = body["tool_choice"]
    if isinstance(tc_name, str) and tc_name not in allowed:
        body.pop("tool_choice", None)
```

### Example

| Allowlist | Blocklist | Result |
|-----------|-----------|--------|
| `""` | `""` | All tools pass through |
| `""` | `"delete_file,terminal_execute"` | Everything except those two |
| `"search_web,calculator"` | `""` | Only search_web and calculator |
| `"search_web"` | `"fetch_url"` | Allowlist wins: only search_web |

### Soft-block interaction

Phase 2's runaway soft-block (`body.pop("tools", None)`) happens **after**
the allowlist/blocklist filter. Phase 3's loop soft-block removes only
the looping tool **after** the allowlist/blocklist filter. Phase 6 runs
only when no soft-block is active.

### Validation

- Blocklist: set `TOOL_BLOCKLIST="search_web"`. Agent cannot call
  `search_web` but can call `fetch_url`, `calculator`, etc.
- Allowlist: set `TOOL_ALLOWLIST="calculator"`. Agent sees only
  `calculator`.
- Both empty: no change, all tools visible.
- `tool_choice` targeting a blocked tool → gracefully reset.
- Loop soft-block still removes only the specific looping tool.
- Runaway soft-block still removes all tools regardless.

---

## File layout on disk

```
pipes/agent_loop_guard/
├── DESIGN.md              # Full design document (reference)
├── PLAN.md                # This file
└── agent_loop_guard.py    # Single-file pipe (all phases)
```

The pipe is a **single Python file** because Open WebUI Functions are
stored as a single source blob in the database. No `__init__.py`, no package.

---

## Implementation order & dependencies

```
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
   │
   └── Foundation: Valves, pipes(), pipe(), _stream, _call
        │
        └── Phase 2 adds one method (_extract_tool_calls_in_turn)
             │
             └── Phase 3 adds _has_consecutive_duplicates, _escalation_level, _inject
                  │
                  └── Phase 4 adds _append_tool_counter, INJECTION_POSITION options
                       │
                       └── Phase 5 adds logging, hardening, no new logic
                            │
                            └── Phase 6 adds allowlist/blocklist filtering
```

Each phase's code is an **additive diff** on the previous phase — never a
rewrite.
