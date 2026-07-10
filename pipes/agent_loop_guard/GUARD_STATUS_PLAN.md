# Implementation Plan: `_guard_status` — Agent Loop Guard Refactor

**Version:** 2.0  
**Based on:** [GUARD_STATUS.md](./GUARD_STATUS.md) (design document)
**Target:** `agent_loop_guard.py` v1.1.0 → v2.0.0

---

## Overview

Replace the current system of injecting system messages and appending plain
text to tool results with a **hybrid mechanism based on a dummy tool
`_guard_status` + system messages**, managed entirely by the pipe.  Key
architectural discoveries made during implementation:

1. **`new_form_data = {**form_data, ...}`** is a shallow copy — list mutations
   must be **in-place** (`list[:] = [...]`) to survive across iterations.
2. **`metadata["tools"]`** is the middleware's execution registry — clearing only
   `body["tools"]` is insufficient; `__metadata__["tools"]` must also be purged.
3. **System messages are essential** — the `_guard_status` pair gets stripped
   before forwarding, so warnings/block instructions must also flow via system
   messages for the LLM to receive them.
4. **No early return for soft-block** — both soft-block and normal states now
   fall through to a single, shared forwarding path, eliminating duplication.

---

## Phases

| Phase | Description | Status |
|:-----:|:------------|:------:|
| **1** | Safe extraction — filter `_guard_status` from turn analysis | ✅ Done |
| **2** | Tool registration — `_add_guard_status_tool()` | ✅ Done |
| **3** | Pair fabrication — `_build_guard_status_*()` pure functions | ✅ Done |
| **4** | Blocklist protection — preserve `_guard_status` in `TOOL_BLOCKLIST` | ✅ Done |
| **5** | Main flow integration — full `pipe()` rewired | ✅ Done |
| **6** | Cleanup — dead code removal, docs, version bump | ⬜ Pending |

---

## Phase 1 — Safe Extraction [✅ Done]

**Goal:** Prevent `_guard_status` pairs from being counted in tool-call
analysis.

| # | Change | File |
|:-:|--------|:----:|
| 1.1 | `_extract_tool_calls_in_turn()`: skip any `tool_calls` entry where `function.name == "_guard_status"` | agent_loop_guard.py |
| 1.2 | `_count_consecutive_duplicates()`: inherently fixed by 1.1 | agent_loop_guard.py |

**Risks mitigated:** R1 (inflated total → early runaway), R2 (off-by-one
in remaining-calls counter).

---

## Phase 2 — Tool Registration [✅ Done]

**Goal:** Add the `_guard_status` tool definition to `body["tools"]`.

| # | Change | File |
|:-:|--------|:----:|
| 2.1 | New method `_add_guard_status_tool(body)` | agent_loop_guard.py |
| 2.2 | Call it in `pipe()` after tool blocklist, **guarded by `state["status"] not in ("runaway", "blocked_tool")`** | agent_loop_guard.py |

**Tool definition:** `{"type": "function", "function": {"name": "_guard_status", ...}}`

---

## Phase 3 — Pair Fabrication [✅ Done]

**Goal:** Pure functions for the fabricated assistant + tool pair (Option B:
`status` + `message` fields).

| # | Change | File |
|:-:|--------|:----:|
| 3.1 | `_build_guard_status_message(state)` → human-readable message string | agent_loop_guard.py |
| 3.2 | `_build_guard_status_content(state)` → JSON with `status` + `message` | agent_loop_guard.py |
| 3.3 | `_build_guard_status_pair(state)` → `(assistant_msg, tool_msg)` tuple | agent_loop_guard.py |

---

## Phase 4 — Blocklist Protection [✅ Done]

**Goal:** Protect `_guard_status` from `TOOL_BLOCKLIST`.

| # | Change | File |
|:-:|--------|:----:|
| 4.3 | `_apply_tool_blocklist()`: skip any tool where `function.name == "_guard_status"` | agent_loop_guard.py |

Also protects `tool_choice` reset from targeting `_guard_status`.

---

## Phase 5 — Main Flow Integration [✅ Done]

**Goal:** Wire everything together in `pipe()` and remove the old injection
mechanism.

### Key design decisions reached during this phase

| Decision | Rationale |
|:---------|:----------|
| **Hybrid approach** | Warnings use `_guard_status` pair + system message; soft-blocks use system message + tool removal (no early return) |
| **Loop detection wins over runaway** | `runaway = total >= max and consecutive < threshold` — an agent in a loop gets `blocked_tool` even if also at the turn limit |
| **In-place slice assignment** | `tools[:] = [...]` mutates the shared list object, surviving `{**form_data}` shallow copy |
| **`__metadata__["tools"]` clearing** | The middleware executes tools from `metadata["tools"]`, not `body["tools"]` |
| **No `tool_choice: "none"`** | Bifrost needs to stay in tool-calling mode so it parses DSML; `"none"` would leak raw DSML as text |
| **No DSML buffer in `_stream()`** | Any buffer that merges `reasoning_content` with `content` breaks Open WebUI's collapsible reasoning display |
| **No early return for soft-block** | Both soft-block and normal states fall through to a single forward path, eliminating ~30 lines of duplicated error handling |

### Changes

| # | Change | File |
|:-:|--------|:----:|
| 5.1 | Build `state` dict from `total`, `consecutive`, `remaining_calls` | agent_loop_guard.py |
| 5.2 | `_inject_or_replace_guard_status(messages, state)`: scan backwards, replace or append | agent_loop_guard.py |
| 5.3 | Remove old `_inject()` calls (dead code kept for Phase 6) | agent_loop_guard.py |
| 5.4 | Remove old `_append_tool_counter()` calls (dead code kept for Phase 6) | agent_loop_guard.py |
| 5.5 | Remove `INJECTION_POSITION` valve | agent_loop_guard.py |
| 5.6 | Remove `SHOW_TOOL_COUNTER` valve | agent_loop_guard.py |
| 5.7 | Update `__event_emitter__` calls to use `_guard_status` terminology | agent_loop_guard.py |
| 5.8 | Call `_add_guard_status_tool(body)` guarded by state check | agent_loop_guard.py |
| 5.9 | `model_validator` updated (already clean — no INJECTION/SHOW references) | agent_loop_guard.py |
| 5.10 | **In-place mutation**: all `body["tools"]` assignments → `tools[:] = [...]` | agent_loop_guard.py |
| 5.11 | **Metadata clearing**: remove tools from `__metadata__["tools"]` on soft-block | agent_loop_guard.py |
| 5.12 | **System messages for warnings**: added for `warning`/`final_warning` states | agent_loop_guard.py |
| 5.13 | **Escalation order**: loop detection evaluated before runaway | agent_loop_guard.py |
| 5.14 | **Runaway symmetric with loop**: remove only used tools, keep unused ones | agent_loop_guard.py |
| 5.15 | **Remove early return**: soft-block falls through to shared forward path | agent_loop_guard.py |

### Soft-block flow (both `blocked_tool` and `runaway`)

```
1. Emit __event_emitter__ notifications (error type)
2. Inject system message: _build_guard_status_message(state)
3. [tools already cleared in-place in the escalation ladder above]
4. Fall through to normal path:
   a. _inject_or_replace_guard_status()  → pair added (will be stripped)
   b. _apply_tool_blocklist()            → filters body["tools"]
   c. _add_guard_status_tool()           → SKIPPED (guarded by state check)
   d. clean_messages                     → strips _guard_status pair
   e. Forward to gateway                  → LLM sees: system msg + empty tools
```

---

## Phase 6 — Cleanup & Polish [⬜ Pending]

**Goal:** Remove all dead code, update documentation, and finalise.

| # | Change | File | Priority |
|:-:|--------|:----:|:--------:|
| 6.1 | Remove `_warning_msg()` function | agent_loop_guard.py | Medium |
| 6.2 | Remove `_final_warning_msg()` function | agent_loop_guard.py | Medium |
| 6.3 | Remove `_runaway_instruction()` function | agent_loop_guard.py | Medium |
| 6.4 | Remove `_loop_blocked_tool_instruction()` function | agent_loop_guard.py | Medium |
| 6.5 | Remove `_inject()` method | agent_loop_guard.py | Medium |
| 6.6 | Remove `_append_tool_counter()` method | agent_loop_guard.py | Medium |
| 6.7 | Remove `INJECTION_POSITION` and `SHOW_TOOL_COUNTER` from README.md | README.md | High |
| 6.8 | Update version to `2.0.0` in frontmatter | agent_loop_guard.py | High |
| 6.9 | Update DESIGN.md or add reference to GUARD_STATUS.md | DESIGN.md | Low |

These are purely cosmetic — the code is functionally complete.

---

## Summary of Changes by Risk

| Risk | Mitigation | Phase |
|:----:|-----------|:-----:|
| R1 — `_extract_tool_calls_in_turn` counts `_guard_status` as real call | Skip entries where `function.name == "_guard_status"` | 1 |
| R2 — Counter off by one from injected pair | Same as R1 | 1 |
| R3 — Shallow copy `{**form_data}` loses reassigned `body["tools"]` | Use **in-place slice assignment** `tools[:] = [...]` | 5 |
| R4 — Middleware executes tools from `metadata["tools"]` | Clear `__metadata__["tools"]` alongside `body["tools"]` | 5 |
| R5 — `tool_choice: "none"` causes raw DSML leakage | Leave `tool_choice` unset; let Bifrost parse DSML into harmless `tool_calls` | 5 |
| R6 — DSML buffer corrupts `reasoning_content` | Don't buffer in `_stream()` — leave it as transparent SSE proxy | 5 |
| R7 — `TOOL_BLOCKLIST` could remove `_guard_status` | Skip `_guard_status` in blocklist filter | 4 |

---

## Edge Cases — Current Behaviour

| Case | Behaviour |
|:----|:----------|
| Turn with 0 tool calls | No `_guard_status` pair injected (state.total == 0) |
| First tool call | `_guard_status` pair appears with `status: "ok"` |
| 2 consecutive identical calls | `_guard_status` shows `status: "warning"` + **system message** |
| Final warning threshold | `_guard_status` shows `status: "final_warning"` + **system message** |
| Loop block (consecutive >= threshold) | Tool removed from `body["tools"]` and `metadata["tools"]`. System message injected. |
| Runaway (total >= max, no loop) | All tools used this turn removed. Unused tools stay. System message. |
| Both loop AND total limit reached | **Loop wins** — `blocked_tool` fires instead of `runaway` |
| LLM voluntarily calls `_guard_status` | Middleware: `"Tool _guard_status not found"`. `sanitize_tool_pairs` cleans orphaned call. Next `pipe()` reinjects normally. |
| `MAX_TOOL_CALLS_PER_TURN = 0` (disabled) | State shows `remaining_calls: 0`, `max_calls: 0`. No runaway block. |
| `TOOL_BLOCKLIST` includes `_guard_status` | Silently ignored — `_guard_status` survives |
