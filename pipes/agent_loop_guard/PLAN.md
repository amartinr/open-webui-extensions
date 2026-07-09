# Implementation Plan: Agent Loop Guard

Phased delivery — each phase produces a **working, uploadable pipe** that
is strictly more capable than the last.

## Progress

- [x] Phase 1 — Transparent Manifold Proxy
- [x] Phase 2 — Runaway Protection
- [x] Phase 3 — Loop Detection & Escalation
- [x] Phase 4 — Preventive Reminders
- [ ] Phase 5 — Polish & Production Readiness

## Phase overview

| Phase | What it does | Status |
|-------|-------------|:------:|
| 1 | Transparent manifold proxy | ✅ Done |
| 2 | + Runaway limit (`MAX_TOOL_CALLS_PER_TURN`) | ✅ Done |
| 3 | + Consecutive duplicate detection + escalation | ✅ Done |
| 4 | + Preventive reminders | ✅ Done |
| 5 | Polish: logging, hardening, tests | ⬜ Pending |

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
| Warnings / escalation / force-terminate | | Phase 2–3 |
| Preventive reminders | | Phase 4 |

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

**Goal**: add `MAX_TOOL_CALLS_PER_TURN` — force-terminate when the agent
makes too many tool calls in one turn. Simplest protection, highest ROI.

### New code

- `MAX_TOOL_CALLS_PER_TURN` valve (already in `Valves`, just wire up).
- `_extract_tool_calls_in_turn()` — scan backwards from end of messages
  until the last `role: "user"`, collect all `tool_calls`.
- In `pipe()`: if `len(history) >= MAX_TOOL_CALLS_PER_TURN`, return
  a plain string → force-terminate.

### `pipe()` logic (Phase 2)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. history = _extract_tool_calls_in_turn(messages)
  4. if len(history) >= MAX_TOOL_CALLS_PER_TURN > 0:
       return "I've stopped because this turn reached the limit…"
  5. Forward to gateway as before
```

### Soft-block strategy

The pipe cannot prevent tool calls that the LLM schedules in batch (e.g.
5 calls in one response). But once the limit is reached, it can remove
`tools` from the body so the LLM cannot call more — this is **soft-block**.

| Opt | Behaviour | Agent sees it? | UX | Status |
|:---:|-----------|:--------------:|----|:------:|
| **A** | Hardcoded string (force-terminate) | ✅ Yes — learns next turn | Impersonates agent, wastes tool results | ❌ Replaced |
| **B** | Empty string `""` | ❌ No feedback | Blank msg; toast informs user | ❌ Discarded |
| **C** | Raise exception | Depends on OWUI | Unknown | ❌ Discarded |
| **D** | **Soft-block**: remove `tools` from body, inject system msg, forward to gateway | ✅ Yes — sees results + instruction, forced to summarise | All results preserved, LLM responds with text | ✅ **Current** |

**Decision**: soft-block (D). All tool results already in messages are
preserved. The LLM receives them plus a system instruction to summarise,
but has no tools available — forced to respond with text.

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
`WARNING` → `FINAL WARNING` → soft-block (remove tools).

### New code

- `_has_consecutive_duplicates()` — checks if the last N tool calls in
  the turn are identical (same name + same args).
- `_escalation_level()` — scans system messages for `WARNING:` /
  `FINAL WARNING:` to deduce current level.
- `_last_system_contains()` — checks any system message for a substring
  (prevents duplicate injections).
- Warning/final-warning message templates.

### `pipe()` logic (Phase 3)

```
pipe(body)
  1. messages = body["messages"]
  2. real_model = body["model"].split(".", 1)[-1]
  3. history      = _extract_tool_calls_in_turn(messages)
  4. escalation   = _escalation_level(messages)
  5. max_esc      = MAX_WARNINGS_BEFORE_TERMINATE

  6. if len(history) >= MAX_TOOL_CALLS_PER_TURN > 0:
       _soft_block(reason, instruction)  ← remove tools, inject, forward

  7. loop = _has_consecutive_duplicates(history, MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING)

  8. if loop:
       if escalation >= max_esc    → _soft_block(reason, instruction)
       elif escalation == 1        → inject FINAL WARNING (tools still available)
       else                        → inject WARNING (tools still available)

  9. Forward to gateway
```

### Validation

- `threshold=2, max_warnings=2, max_tool_calls=15`
- Turn: `[search("X"), search("X")]` → loop detected, escalate=0 → inject WARNING
- Turn: `[search("X")×3]` → loop detected, escalate=1 → inject FINAL WARNING
- Turn: `[search("X")×4]` → loop detected, escalate=2 ≥ max_warnings → **soft-block** (tools removed, forward to gateway)
- Turn: `[search("X"), search("Y")]` → **no** loop (different args).
- Turn: `[search("X"), fetch("X")]` → **no** loop (different tool).
- `MAX_WARNINGS_BEFORE_TERMINATE=0` → soft-block on first loop.
- WARNING already in messages from earlier turn → escalate to FINAL, don't re-inject.

---

## Phase 4 — Preventive Reminders  [x]

**Goal**: inject a "REMINDER: Periodically evaluate…" message every N
user messages, so the agent self-checks even before looping.

### New code

Already wired in Phase 3 (`_last_system_contains`, `_inject`). Just add the
`if ENABLE_PREVENTIVE_REMINDER and not loop_detected` block in `pipe()`.

### `pipe()` logic (Phase 4)

Same as Phase 3, plus:
```
  8.5. if ENABLE_PREVENTIVE_REMINDER and not loop
         user_count = count of role=="user" in messages
         if user_count > 0 and user_count % REMINDER_INTERVAL == 0
           if not _last_system_contains(messages, "REMINDER:")
             inject REMINDER
```

### Validation

- `INTERVAL=3`: reminder injected on 3rd, 6th, 9th, … user message.
- Loop detected in same turn → reminder is **suppressed** (warning takes priority).
- REMINDER already present from earlier turn → not re-injected.
- `ENABLE_PREVENTIVE_REMINDER=false` → never injected.

---

## Phase 5 — Polish & Production Readiness  [ ]

### Tasks

1. **Logging** — `import logging; log = logging.getLogger(__name__)`.
   Log model discovery, injections, force-terminations at INFO/DEBUG.
2. **Gateway error messages** — return a human-readable string with the
   HTTP status code, not the raw exception.
3. **Model name fallback** — if gateway returns a model without `name`,
   use `id` as display name (already in DESIGN.md code).
4. **Test with multiple gateway providers** — LiteLLM, Bifrost, custom
   OpenAI-compatible proxies.
5. **Test edge cases from DESIGN.md §13** — all 9 cases.

### Optional stretch goals (not in DESIGN.md)

- **`__event_emitter__` status pills** — emit `"status"` events during
  force-termination so the user sees "🛡️ Loop guard: stopped" in the UI.
- **Per-model overrides** — if a manifold per-model valve pattern emerges in
  Open WebUI, allow per-sub-pipe thresholds.

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
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5
   │
   └── Foundation: Valves, pipes(), pipe(), _stream, _call
        │
        └── Phase 2 adds one method (_extract_tool_calls_in_turn)
             │
             └── Phase 3 adds three methods + inject helper
                  │
                  └── Phase 4 reuses all of the above, adds one if-block
                       │
                       └── Phase 5 adds logging, hardening, no new logic
```

Each phase's code is an **additive diff** on the previous phase — never a
rewrite. The Phase 1 file is built from scratch. Phases 2–5 are series of
small, reviewable edits.
