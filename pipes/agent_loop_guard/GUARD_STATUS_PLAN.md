# Implementation History: `_guard_status` — Removed

**Status:** Abandoned — the `_guard_status` dummy tool mechanism was
implemented in v2.0.0 and removed shortly after when analysis showed it
provided no value to the agent.

---

## What was `_guard_status`?

A dummy tool registered in `body["tools"]` that the pipe would automatically
"call" by fabricating assistant+tool message pairs into the history on every
turn iteration. The intent was to keep the agent informed of its remaining
tool budget and loop status without contaminating real tool results.

## Why it was removed

| Issue | Detail |
|-------|--------|
| **Fabricated pair never reached the LLM** | Stripped by `clean_messages` before forwarding to avoid `reasoning_content` validation errors on DeepSeek thinking mode |
| **Did not survive between iterations** | `body["messages"]` is rebuilt from `form_data` on every middleware iteration — the pair was lost |
| **Tool definition misled the agent** | Visible in `body["tools"]` but not callable (not in `metadata["tools"]`). Calling it returned `"Tool _guard_status not found"`, wasting a turn |
| **State is self-contained** | Every `pipe()` call recalculates from `_extract_tool_calls_in_turn()` — no cross-iteration memory needed |

## What replaced it

The current architecture uses only mechanisms that **do** survive the
middleware loop:

1. **System messages** — injected into `body["messages"]` at warning,
   final_warning, and soft-block. The LLM receives them via the normal
   forwarding path (no stripping).
2. **In-place tool removal** — `tools[:] = [...]` mutates the shared list
   object, surviving the shallow copy.
3. **Metadata clearing** — `__metadata__["tools"].pop(name)` uses the same
   dict object as the middleware's execution registry.

## Removed functions (Phase 6 Cleanup)

| Function | Purpose |
|----------|---------|
| `_build_guard_status_content()` | Built JSON content for the fabricated tool result |
| `_build_guard_status_pair()` | Built the assistant+tool message pair |
| `_add_guard_status_tool()` | Added `_guard_status` definition to `body["tools"]` |
| `_inject_or_replace_guard_status()` | Managed the pair in the message history |
| `_warning_msg()`, `_final_warning_msg()` | Dead code (replaced by `_build_guard_message()`) |
| `_runaway_instruction()`, `_loop_blocked_tool_instruction()` | Dead code (replaced by `_build_guard_message()`) |
| `_append_tool_counter()` | Dead code from v1 injection position system |
| `_inject()` | Dead code from v1 injection position system |

**Renamed:** `_build_guard_status_message()` → `_build_guard_message()`

**Removed import:** `Literal` from typing (was used by `INJECTION_POSITION` valve)

## Current state

The pipe is v2.0.0 with a clean, minimal architecture:
- System message-based escalation (no fabricated pairs)
- In-place tool/metadata mutation (survives middleware loop)
- No cross-iteration state needed
