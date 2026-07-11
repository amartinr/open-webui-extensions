# Implementation Plan: Agent Loop Guard

Phased delivery — each phase produces a **working, uploadable pipe** that
is strictly more capable than the last.

## Progress

- [x] Phase 1 — Transparent Manifold Proxy
- [x] Phase 2 — Runaway Protection
- [x] Phase 3 — Loop Detection & Escalation
- [x] Phase 4 — Tool Counter & Injection Options
- [x] Phase 5 — Polish & Production Readiness
- [x] Phase 6 — Tool Blocklist
- [x] Phase 7 — `_guard_status` cleanup (removed dead code, docs aligned)

## Phase overview

| Phase | What it does | Status |
|-------|-------------|:------:|
| 1 | Transparent manifold proxy | ✅ Done |
| 2 | + Runaway limit (`MAX_TOOL_CALLS_PER_TURN`) + soft-block | ✅ Done |
| 3 | + Consecutive duplicate detection + escalation + selective tool removal | ✅ Done |
| 4 | + Descending tool counter + `merge_last_tool` injection + shortened messages | ✅ Done |
| 5 | Polish: logging, hardening, tests | ✅ Done |
| 6 | + Tool blocklist (remove tools by name) | ✅ Done |
| 7 | + `_guard_status` removal: stripped dead code, docs aligned | ✅ Done |

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

- `_count_consecutive_duplicates()` — counts consecutive identical tool calls
  from the end of history (returns count, name, args).
- Formula-based escalation: `consecutive==2` → WARNING, `consecutive==final_pos`
  → FINAL WARNING, `consecutive >= N` → soft-block.
- `_inject()` — inserts guard messages at configured position
  (see Phase 4 for injection options).
- Warning/final-warning message helpers (`_warning_msg`, `_final_warning_msg`).

### Guard message helpers

```python
def _warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x with same args. Change approach or summarize."

def _final_warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x. Still repeating. Stop now and summarize."
```

### Escalation ladder

Each level fires **exactly once**:

```
consecutive == 2                    → WARNING
consecutive == final_pos (≈60% of N) → FINAL WARNING (if N > 3)
consecutive >= N                     → SOFT-BLOCK
  Only the offending tool removed from body["tools"].
  Agent can still use other tools or summarise.
otherwise                           → silent
```

Where `final_pos = 2 + (N - 2) * 3 // 5`.

### `pipe()` logic (Phase 3)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. history      = _extract_tool_calls_in_turn(messages)
  4. total        = len(history)
  5. consecutive, bad_tool, _ = _count_consecutive_duplicates(history)
  6. N            = MAX_CONSECUTIVE_BEFORE_BLOCK
  7. final_pos    = 2 + (N - 2) * 3 // 5  ← ≈60% of range

  8. if total >= MAX_TOOL_CALLS_PER_TURN > 0:
       _soft_block(None)              ← remove all tools (runaway)

  9. if consecutive >= 2:
       if consecutive >= N:
           _soft_block(bad_tool)       ← remove only looping tool
       elif N > 3 and consecutive == final_pos:
           inject FINAL WARNING (tools still available)
       elif consecutive == 2:
           inject WARNING (tools still available)

 10. Forward to gateway (tools still available, unless soft-blocked)
```

### Validation (with `MAX_CONSECUTIVE_BEFORE_BLOCK=4`)

- Turn: `[search("X"), search("X")]` → consecutive=2, ==2 → inject WARNING
- Turn: `[search("X")×3]` → consecutive=3, final_pos=3 → inject FINAL WARNING
- Turn: `[search("X")×4]` → consecutive=4 ≥ N → **soft-block** (only `search` removed)
- Turn: `[search("X"), search("Y")]` → consecutive=1 (different args) → no loop
- Turn: `[search("X"), fetch("X")]` → consecutive=1 (different tool) → no loop
- `MAX_CONSECUTIVE_BEFORE_BLOCK=3` → consecutive=2 → WARNING, consecutive=3 → soft-block (no FINAL WARNING)

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
- `_inject()` updated to support `merge_last_tool` position (appends guard
  message to the last tool result instead of inserting a new message).

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

---
web_search called 2x with same args. Change approach or summarize.
```

### Soft-block messages (short)

| Type | Message |
|:----:|---------|
| Runaway | `TOOL LIMIT: {total}/{max} used. No more tools this turn. Summarize now.` |
| Loop (tool removed) | `TOOL REMOVED: {tool_name} blocked after {total} identical calls. Other tools still available. Summarize or continue.` |

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
4. **Tested with Bifrost** (production use). No other gateways planned.
   → ✅ Done.
5. **Test edge cases from DESIGN.md §13** — all 9 cases. → ⬜ Pending.
6. **Replace `GATEWAY_HOST_HEADER` + `GATEWAY_HOST_VALUE` with single `GATEWAY_CUSTOM_HEADERS` valve** — the old two-valve pattern was specific to Bifrost's host-routing. The new valve accepts a JSON object so arbitrary headers (host routing, tracing, debug, etc.) can be added without adding a new valve per header. Backward-incompatible: existing installations must migrate their `GATEWAY_HOST_HEADER`/`GATEWAY_HOST_VALUE` into `GATEWAY_CUSTOM_HEADERS` as a JSON object (e.g. `{"x-bf-dim-host": "myhost"}`). → ✅ Done.

### Optional stretch goals (not in DESIGN.md)

- **`__event_emitter__` status pills** — emit `"status"` events during
  force-termination so the user sees "🛡️ Loop guard: stopped" in the UI.
  → ✅ Done: both notification and status events in `_soft_block`.

---

## Phase 6 — Tool Blocklist  [x]

**Goal**: let the admin remove tools from the agent's tool list by name.
The pipe filters `body["tools"]` before forwarding, so the LLM never
knows the removed tools existed.

### Motivation

Open WebUI workspace models inherit all available tools. An admin may
want to:

- **Block** a dangerous tool (`terminal_execute`, `delete_file`)
- **Remove** expensive tools (`search_web`, `fetch_url`) from cheap models

A Filter cannot do this reliably — the tool list is reconstructed after
filter execution. A Pipe can mutate `body["tools"]` directly before
forwarding, and the change is **definitive**.

### New valve

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| `TOOL_BLOCKLIST` | str | `""` | Comma-separated or newline-separated tool names to **remove** from the agent's tool list |

### Input format

The valve accepts flexible input — commas, newlines, or a mix of both.
All of these produce the same result:

```
delete_file, terminal_execute
```

```
delete_file
terminal_execute
```

```
delete_file, terminal_execute
fetch_url
```

Parsed with `re.split(r"[,\\n\\r]+", raw)` — whitespace around names
is stripped automatically.

### `pipe()` logic (Phase 6)

After analysing tool calls (phases 2-4), before forwarding:

```
blocked = _parse_tool_list(TOOL_BLOCKLIST)

# Warn about names that don't match any available tool
actual_names = {t["function"]["name"] for t in body["tools"]}
unknown = blocked - actual_names
if unknown:
    log.warning("TOOL_BLOCKLIST unknown: %s", sorted(unknown))

body["tools"] = [
    t for t in body["tools"]
    if t["function"]["name"] not in blocked
]

if tool_choice targets a blocked tool → reset tool_choice
```

### Error handling

- If the user writes a tool name that doesn't exist among the available
  tools, it's logged as a **warning** but doesn't break execution.
  The unknown names are simply ignored; the known ones are blocked.
- Matching is **exact** (`==`) — `fetch_url` does not match `smart_fetch_url`.

### Soft-block interaction

The blocklist filter runs **before** soft-block logic. If the runaway
or loop soft-block fires afterwards, it operates on the already-filtered
tool list (removing all tools or just the looping one, respectively).

### Validation

- Set `TOOL_BLOCKLIST="fetch_url"`. Agent cannot call `fetch_url`;
  `smart_fetch_url`, `search_web`, etc. remain available.
- Set `TOOL_BLOCKLIST=""` (empty). All tools pass through unchanged.
- Typo `TOOL_BLOCKLIST="fech_url"` → warning in logs, nothing blocked.
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
             └── Phase 3 adds _count_consecutive_duplicates, formula-based escalation, _inject
                  │
                  └── Phase 4 adds _append_tool_counter, INJECTION_POSITION options
                       │
                       └── Phase 5 adds logging, hardening, no new logic
                            │
                            └── Phase 6 adds tool blocklist filtering
```

Each phase's code is an **additive diff** on the previous phase — never a
rewrite.
