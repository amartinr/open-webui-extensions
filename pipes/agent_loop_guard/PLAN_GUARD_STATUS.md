# Implementation Plan: `_guard_status` — Agent Loop Guard Refactor

**Version:** 1.0  
**Based on:** [GUARD_STATUS.md](./GUARD_STATUS.md) (design document)
**Target:** `agent_loop_guard.py` v1.1.0 → v2.0.0

---

## Overview

Replace the current system of injecting system messages and appending plain
text to tool results with a **unified mechanism based on a dummy tool
`_guard_status`** managed entirely by the pipe.

The design doc (GUARD_STATUS.md) identifies three real risks (R1–R3) and lays
out the full architecture. This plan breaks the implementation into **6 phases**
with clear dependency ordering.

---

## Dependency Graph

```
Phase 1 ───────────────────────────────────────────────┐
  (extraction filter — R1/R2)                          │
      │                                                │
      ▼                                                │
Phase 2                                                │
  (tool registration + definition)                     │
      │                                                │
      ▼                                                │
Phase 3 ──────────────────────────────────┐            │
  (pair fabrication + injection/replace)  │            │
      │                                   │            │
      ▼                                   ▼            ▼
Phase 4 ──────────────────────────────────────────────────┐
  (soft-block hardening — R3 + blocklist protection)      │
      │                                                    │
      ▼                                                    ▼
Phase 5 ──────────────────────────────────────────────────────┐
  (main pipe() flow — wire everything, remove old mechanism)  │
      │                                                        │
      ▼                                                        ▼
Phase 6 ──────────────────────────────────────────────────────────┐
  (cleanup: remove unused methods/valves, update docs, finalise)  │
```

### Dependency rules

| Phase | Depends on | Wait for? |
|:-----:|-----------|:---------:|
| **1** | Nothing | No |
| **2** | Nothing | No — independent of Phase 1 |
| **3** | Phase 2 (needs the tool name/definition) | Yes |
| **4** | Nothing | No — independent, can be done in parallel with Phases 1–3 |
| **5** | Phases 1, 3, 4 | Yes — all three must exist |
| **6** | Phase 5 | Yes — only after old mechanism is gone |

Phases 1, 2, and 4 are **fully independent** and can be implemented in any
order (or in parallel). Phase 5 is the integration point where everything
comes together.

---

## Phase 1 — Safe Extraction (R1/R2) [no new behaviour]

**Goal:** Prevent `_guard_status` pairs from being counted in tool-call
analysis **before** the new mechanism is active. This is a pure defensive
change — it adds filtering logic that has no effect until `_guard_status`
pairs appear in the message history.

Even though the pipe does not yet inject `_guard_status` pairs, the filter
is harmless: `_guard_status` never appears in `function.name` at this
stage, so the filter is a no-op. When Phase 3 starts injecting, the filter
is already in place — no risk of inflating the counter.

| # | Change | File |
|:-:|--------|:----:|
| 1.1 | `_extract_tool_calls_in_turn()`: skip any `tool_calls` entry where `function.name == "_guard_status"` | agent_loop_guard.py |
| 1.2 | `_count_consecutive_duplicates()`: inherently fixed by 1.1 — only real calls reach it | agent_loop_guard.py |

**Risks mitigated:** R1 (inflated total → early runaway), R2 (off-by-one
in remaining-calls counter).

**Validation:**
- No `_guard_status` pairs exist yet → function produces identical output
- No behaviour change observable

---

## Phase 2 — Tool Registration [internal only]

**Goal:** Add the `_guard_status` tool definition to `body["tools"]` on
every `pipe()` call. At this stage no pairs are injected, so the tool is
visible to the LLM but never called and never fabricated.

| # | Change | File |
|:-:|--------|:----:|
| 2.1 | New method `_add_guard_status_tool(body)`: prepend (or append) `_guard_status` to `body["tools"]` | agent_loop_guard.py |
| 2.2 | Call `_add_guard_status_tool(body)` in `pipe()` after tool blocklist but before forwarding | agent_loop_guard.py |

**Tool definition:**
```python
{
    "type": "function",
    "function": {
        "name": "_guard_status",
        "description": "Internal read‑only tool. Returns the current state of the agent loop guard: number of tool calls in the turn, consecutive identical calls, block status, and remaining budget.",
        "parameters": {"type": "object", "properties": {}}
    }
}
```

**Position in `pipe()`:** After tool blocklist filtering, before the
forward payload is built. This ensures `_guard_status` is always present
in the final tool list the LLM sees, even after blocklist or soft-block.

**Validation:**
- `body["tools"]` contains `_guard_status` in the forwarded request
- No `_guard_status` pairs in messages — no injection yet
- LLM could theoretically call it, but the middleware will skip it
  (`tool not found` in its registry, not in body["tools"])

---

## Phase 3 — Pair Fabrication & Injection/Replacement

**Goal:** Build the fabricated assistant + tool pair and manage its
presence in the message history. This is the core of the new mechanism.

| # | Change | File |
|:-:|--------|:----:|
| 3.1 | New method `_build_guard_status_content(state)` → returns tool result dict (status + message) | agent_loop_guard.py |
| 3.2 | New method `_build_guard_status_pair(state)` → returns `(assistant_msg, tool_msg)` | agent_loop_guard.py |
| 3.3 | New method `_inject_or_replace_guard_status(messages, state)`: scan backwards, replace if found, append if not | agent_loop_guard.py |
| 3.4 | New guard message builders that produce the tool result content (not system messages) | agent_loop_guard.py |

### 3.1 — Tool result content format (Option B from GUARD_STATUS.md)

Minimal two-field format for token efficiency:

```python
def _build_guard_status_content(state: dict) -> str:
    """Build the content for the _guard_status tool result.

    state keys:
      - status: str ('ok'|'warning'|'final_warning'|'blocked_tool'|'runaway')
      - tool: str | None  — tool name involved
      - consecutive: int
      - total: int
      - max_calls: int
      - blocked: bool
      - blocked_tools: list[str]
    """
    return json.dumps({
        "status": state["status"],
        "message": _build_guard_status_message(state),
    })
```

### 3.2 — Fabricated message pair

```python
def _build_guard_status_pair(state: dict) -> tuple[dict, dict]:
    content = _build_guard_status_content(state)
    assistant_msg = {
        "role": "assistant",
        "tool_calls": [{
            "id": "guard_status",
            "type": "function",
            "function": {
                "name": "_guard_status",
                "arguments": "{}"
            }
        }]
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": "guard_status",
        "content": content,
    }
    return assistant_msg, tool_msg
```

### 3.3 — Injection/replacement logic

```python
def _inject_or_replace_guard_status(self, messages: list[dict], state: dict) -> None:
    """Scan backwards through messages.

    - If an assistant message with tool_calls[0].id == 'guard_status' is found,
      replace that assistant + its matching tool result in-place.
    - If not found and state['total'] > 0, append a new pair at the end.
    - If total == 0, do nothing (no guard state yet).
    """
    pair = _build_guard_status_pair(state)
    ...
```

**Safety note:** The fixed `tool_call_id = "guard_status"` ensures
`sanitize_tool_pairs()` in the middleware preserves the pair (the tool
message's `tool_call_id` matches the assistant's `tool_calls[0].id`).

### 3.4 — Human-readable message builders

These replace the old `_warning_msg`, `_final_warning_msg`, etc. but
produce text destined for the tool result content, not a system message.

```python
def _build_guard_status_message(state: dict) -> str:
    if state["status"] == "ok":
        return f"{state['remaining_calls']}/{state['max_calls']} tool calls remaining."
    elif state["status"] == "warning":
        return (f"{state['tool']} called {state['consecutive']}x with the same arguments. "
                f"{state['remaining_calls']}/{state['max_calls']} tool calls remaining. "
                f"Change your approach or summarise.")
    elif state["status"] == "final_warning":
        return (f"{state['tool']} called {state['consecutive']}x with the same arguments. "
                f"{state['remaining_calls']}/{state['max_calls']} tool calls remaining. "
                f"This is your final warning. Stop repeating and summarise.")
    elif state["status"] == "blocked_tool":
        return (f"TOOL REMOVED: {state['tool']} blocked after {state['consecutive']} identical calls. "
                f"{state['remaining_calls']}/{state['max_calls']} tool calls remaining. "
                f"Other tools are still available or you may summarise now.")
    elif state["status"] == "runaway":
        return (f"Tool call limit reached: {state['total']}/{state['max_calls']}. "
                f"No more tool calls this turn. Summarise now.")
    return ""
```

**Validation:**
- Unit-testable: `_build_guard_status_pair` produces valid messages
- Calling `_inject_or_replace_guard_status` on empty messages with `total=0`
  does nothing
- Calling it on messages with `total>0` appends a new pair
- Calling it twice replaces the previous pair (no accumulation)

---

## Phase 4 — Soft-Block Hardening (R3) + Blocklist Protection

**Goal:** Ensure `_guard_status` survives all soft-block and blocklist
operations. This phase is independent of Phases 1–3.

| # | Change | File |
|:-:|--------|:----:|
| 4.1 | In `_soft_block` (runaway case): replace `body.pop("tools", None)` with filter that keeps only `_guard_status` | agent_loop_guard.py |
| 4.2 | In `_soft_block` (loop case): ensure `_guard_status` is never removed (it already only removes the offending tool, but add explicit guard) | agent_loop_guard.py |
| 4.3 | In `_apply_tool_blocklist()`: skip any tool where `function.name == "_guard_status"` | agent_loop_guard.py |

### 4.1 — Runaway soft-block

**Before:**
```python
body.pop("tools", None)
body.pop("tool_choice", None)
```

**After:**
```python
# Keep only _guard_status so the LLM can still read guard state
body["tools"] = [
    t for t in body.get("tools", [])
    if t.get("function", {}).get("name") == "_guard_status"
]
body.pop("tool_choice", None)
```

### 4.2 — Loop soft-block

The loop case already filters by name, so `_guard_status` naturally
survives. Add an explicit comment/assertion for clarity:

```python
# Keep _guard_status (it should never be the offending tool,
# but guard explicitly for safety)
```

### 4.3 — Blocklist protection

```python
# Never remove _guard_status, even if accidentally blocklisted
body["tools"] = [
    t for t in tools
    if t.get("function", {}).get("name") not in blocked
    or t.get("function", {}).get("name") == "_guard_status"
]
```

Also update the `tool_choice` reset to ignore `_guard_status`:
```python
if isinstance(tool_choice, str) and tool_choice in blocked and tool_choice != "_guard_status":
    body.pop("tool_choice", None)
```

**Validation:**
- Runaway soft-block: `_guard_status` remains in `body["tools"]`, all
  real tools removed
- Loop soft-block: offending tool removed, `_guard_status` stays
- `TOOL_BLOCKLIST` containing `_guard_status` → silently ignored
- `_guard_status` never appears in `blocked_tools` list

---

## Phase 5 — Main Flow Integration

**Goal:** Wire everything together in `pipe()` and remove the old
injection mechanism. This is the integration phase.

| # | Change | File |
|:-:|--------|:----:|
| 5.1 | In `pipe()`: after computing `total`, `consecutive`, etc., build state dict and call `_inject_or_replace_guard_status()` | agent_loop_guard.py |
| 5.2 | Remove calls to `_inject()` (old system message injection) | agent_loop_guard.py |
| 5.3 | Remove call to `_append_tool_counter()` | agent_loop_guard.py |
| 5.4 | Remove `INJECTION_POSITION` valve | agent_loop_guard.py |
| 5.5 | Remove `SHOW_TOOL_COUNTER` valve | agent_loop_guard.py |
| 5.6 | Update `__event_emitter__` calls to use `_guard_status` terminology | agent_loop_guard.py |
| 5.7 | Call `_add_guard_status_tool(body)` in the right place in `pipe()` | agent_loop_guard.py |
| 5.8 | Update the `model_validator` to remove INJECTION_POSITION/SHOW_TOOL_COUNTER references (if any) | agent_loop_guard.py |

### 5.1 — Main flow logic in `pipe()`

```python
# --- Build guard state ---------------------------------
blocked_tools_list = []  # populated during soft-block
state = {
    "status": "ok",
    "tool": bad_tool if consecutive >= 2 else None,
    "consecutive": consecutive,
    "total": total,
    "max_calls": self.valves.MAX_TOOL_CALLS_PER_TURN,
    "remaining_calls": max(0, self.valves.MAX_TOOL_CALLS_PER_TURN - total),
    "blocked": bool(blocked_tools_list),
    "blocked_tools": blocked_tools_list,
}

# --- Determine escalation level ------------------------
if consecutive >= 2:
    final_pos = 2 + (block_threshold - 2) * 3 // 5
    if consecutive >= block_threshold:
        state["status"] = "blocked_tool"
        # ... soft-block (set blocked_tools_list) ...
    elif block_threshold > 3 and consecutive == final_pos:
        state["status"] = "final_warning"
    elif consecutive == 2:
        state["status"] = "warning"

if runaway:
    state["status"] = "runaway"
    # ... soft-block ...

# --- Inject/replace guard status pair -----------------
self._inject_or_replace_guard_status(messages, state)

# --- Add _guard_status to tools -----------------------
self._add_guard_status_tool(body)
```

### 5.5–5.6 — Valve removals

Remove from `Valves` class:
```python
# REMOVED:
# INJECTION_POSITION: Literal["append_user", "merge_last_tool"] = ...
# SHOW_TOOL_COUNTER: bool = ...
```

**Validation:**
- Old system messages no longer appear in the chat
- Old `--- remaining tool calls: N` no longer appended to tool results
- `_guard_status` pair appears at the end of messages with correct state
- Warnings, final warnings, blocks, and runaway all produce correct status
- `_guard_status` is present in `body["tools"]`

---

## Phase 6 — Cleanup & Polish

**Goal:** Remove all dead code, update documentation, and finalise.

| # | Change | File |
|:-:|--------|:----:|
| 6.1 | Remove `_warning_msg()` function | agent_loop_guard.py |
| 6.2 | Remove `_final_warning_msg()` function | agent_loop_guard.py |
| 6.3 | Remove `_runaway_instruction()` function | agent_loop_guard.py |
| 6.4 | Remove `_loop_blocked_tool_instruction()` function | agent_loop_guard.py |
| 6.5 | Remove `_inject()` method | agent_loop_guard.py |
| 6.6 | Remove `_append_tool_counter()` method | agent_loop_guard.py |
| 6.7 | Remove `_merge_injected` instance variable | agent_loop_guard.py |
| 6.8 | Remove `INJECTION_POSITION` and `SHOW_TOOL_COUNTER` from README.md | README.md |
| 6.9 | Update version to 2.0.0 in frontmatter | agent_loop_guard.py |
| 6.10 | Update DESIGN.md or add reference to GUARD_STATUS.md | DESIGN.md (optional) |

---

## Summary of Changes by Risk

| Risk | Mitigation | Phase |
|:----:|-----------|:-----:|
| R1 — `_extract_tool_calls_in_turn` counts `_guard_status` as real call | Skip entries where `function.name == "_guard_status"` | 1 |
| R2 — Counter off by one from injected pair | Same as R1 | 1 |
| R3 — `_soft_block(runaway)` calls `body.pop("tools")`, removing `_guard_status` | Filter instead of pop; keep `_guard_status` | 4 |
| R4 — `TOOL_BLOCKLIST` could remove `_guard_status` | Skip `_guard_status` in blocklist filter | 4 |

---

## Files Modified

| File | Changes |
|:----|:--------|
| `agent_loop_guard.py` | All 6 phases |
| `PLAN_GUARD_STATUS.md` | This file |
| `GUARD_STATUS.md` | May need minor corrections after implementation |
| `README.md` | Remove `INJECTION_POSITION`, `SHOW_TOOL_COUNTER` from valve table (Phase 6) |

No new files are created. The pipe remains a single file.

---

## Edge Cases to Verify

| Case | Expected behaviour |
|:----|:------------------|
| Turn with 0 tool calls | No `_guard_status` pair injected (state.total == 0) |
| First tool call | `_guard_status` pair appears with `status: "ok"` |
| 2 consecutive identical calls | `_guard_status` shows `status: "warning"` |
| Final warning threshold | `_guard_status` shows `status: "final_warning"` |
| Loop block | `_guard_status` shows `status: "blocked_tool"`, tool removed from `body["tools"]`, but `_guard_status` remains |
| Runaway limit | `_guard_status` shows `status: "runaway"`, all real tools removed, `_guard_status` remains |
| LLM voluntarily calls `_guard_status` | Middleware skips (`tool not found`), `sanitize_tool_pairs` cleans the orphaned call. Next `pipe()` invocation reinjects normally. See GUARD_STATUS.md §3.5. |
| `MAX_TOOL_CALLS_PER_TURN = 0` (disabled) | State shows `remaining_calls: 0`, `max_calls: 0`. No runaway block. |
| `TOOL_BLOCKLIST` includes `_guard_status` | Silently ignored — `_guard_status` survives |
