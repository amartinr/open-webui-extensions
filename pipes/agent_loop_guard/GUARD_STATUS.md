# Design Document: Agent Loop Guard — Current Architecture

**Version:** 3.0  
**Based on:** `agent_loop_guard.py` (post-`_guard_status` cleanup)

---

## 1. Purpose

Prevent AI agents in Open WebUI from entering infinite tool-calling loops by
analysing conversation history **on every iteration** of the middleware's
tool-call loop, escalating through system-message warnings up to **soft-block**
(removing tools from the request body and metadata).

---

## 2. Core Mechanism

The pipe sits between Open WebUI and the LLM gateway. On every tool-call
iteration, the middleware calls `pipe()` with the accumulated message history.
The pipe:

1. **Analyses** the current turn's tool calls (consecutive duplicates, total
   count)
2. **Escalates** through a formula-based ladder: WARNING → FINAL WARNING →
   soft-block
3. **Acts** via two channels that both survive the middleware's shallow-copy
   loop:
   - **System messages** injected into `body["messages"]` — forwarded to the
     gateway, the LLM receives them
   - **In-place tool removal** (`tools[:] = [...]`) on `body["tools"]` — the
     shallow copy shares the same list object, so the change persists
   - **Metadata clearing** (`__metadata__["tools"].pop(name)`) — the same dict
     object as the middleware's `metadata["tools"]`, so removed tools cannot
     be executed

---

## 3. Why Not a Fabricated Tool Pair

Earlier versions (v1.x, v2.0.0) used a dummy tool `_guard_status` with
fabricated assistant+tool pairs injected into the message history. This was
removed because:

| Issue | Detail |
|-------|--------|
| **Stripped before forwarding** | The pair was removed by `clean_messages` to avoid `reasoning_content` validation errors on DeepSeek thinking mode — the LLM never saw it |
| **Did not survive iterations** | `body["messages"]` is a new list each iteration (rebuilt from `form_data`), so the pair was lost between pipe calls |
| **Tool definition misled the agent** | `_guard_status` was in `body["tools"]` but not in `metadata["tools"]` — if the agent called it, it received `"Tool _guard_status not found"`, wasting a turn |
| **State is self-contained** | Every `pipe()` call recalculates state fresh from `_extract_tool_calls_in_turn()` — no cross-iteration memory needed |

The current design uses only the two channels that **do** work reliably:
system messages and in-place tool/metadata mutation.

---

## 4. Escalation Ladder

Each level fires **exactly once** per turn, determined by the formula:

```
final_pos = 2 + (N - 2) * 3 // 5    # ≈60% of the range [2, N)
```

| consecutive | Action | Mechanism |
|:-----------:|:------:|-----------|
| 1 | None (silent) | — |
| 2 | **WARNING** | System message appended: `"{tool} called 2x... Change your approach or summarise."` |
| `final_pos` (if N > 3) | **FINAL WARNING** | System message appended: `"{tool} called {n}x... Final warning."` |
| ≥ N | **SOFT-BLOCK** | Offending tool removed from `body["tools"]` + `__metadata__["tools"]`. System message instructs the agent to summarise. |

For `N=3` there is no FINAL WARNING (WARNING → block directly).

**Priority:** Loop detection wins over runaway. Runaway only fires when
`consecutive < block_threshold` — an agent in a loop gets `blocked_tool`
(e.g. only the looping tool removed, other tools stay available) even if it
also hit the total-turn limit.

---

## 5. Soft-block (Loop & Runaway)

When the guard decides to stop the agent:

1. **In-place tool removal** — `tools[:] = [...]` mutates the shared list
   object so the change survives the middleware's shallow copy
2. **Metadata clearing** — `__metadata__["tools"].pop(name)` prevents the
   middleware from executing the tool even if the LLM emits tool_calls
3. **System message** — `messages.append({"role": "system", ...})` instructs
   the agent to summarise
4. **No early return** — execution falls through to the normal forwarding
   path. The LLM receives all collected tool results + system instruction,
   but with empty tools it **cannot** schedule more tool calls

### Runaway vs Loop block

| Scenario | What is removed | Agent can still use |
|:---------|:---------------|:--------------------|
| **Loop** (consecutive ≥ threshold) | Only the looping tool | Other tools + summarise |
| **Runaway** (total ≥ max, no loop) | All tools used this turn | Unused tools + summarise |

---

## 6. Middleware Integration

The pipe relies on three properties of Open WebUI's `streaming_chat_response_handler`
in `backend/open_webui/utils/middleware.py`:

### 6.1 `pipe()` is called on every tool-call iteration

The middleware's tool-call loop:

```python
while tool_calls and (iterations < CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS):
    # ... execute tools, collect results ...
    new_form_data = {**form_data, ...}
    new_form_data['messages'] = [*form_data['messages'], *tool_messages]
    res = await generate_chat_completion(request, new_form_data, ...)  # → pipe()
```

The model ID retains its pipe prefix, so every iteration routes through `pipe()`.

### 6.2 `body["tools"]` is shared via shallow copy

`new_form_data = {**form_data}` is a **shallow copy**. Lists inside `form_data`
(including `body["tools"]`) are the **same objects** — the copy only copies
references. Mutating the list with **slice assignment** (`tools[:] = [...]`)
modifies the shared object. Reassigning (`body["tools"] = [...]`) creates a
new object that `form_data["tools"]` does not follow.

### 6.3 `__metadata__["tools"]` is the middleware's execution registry

The middleware executes tools by looking them up in `metadata["tools"]` (a
callable dict), **not** `body["tools"]` (the LLM-facing spec list). Removing
tools from `__metadata__["tools"]` prevents execution even if the LLM emits
`tool_calls` (via parsed DSML, etc.).

---

## 7. Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | Credential value (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers with template variable support |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before soft-block. `0` = disabled |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | `4` | Consecutive identical calls before soft-block (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove from the agent's list |

**Validation:** `MAX_TOOL_CALLS_PER_TURN` must be > `MAX_CONSECUTIVE_BEFORE_BLOCK`
when both are enabled.

---

## 8. Custom Headers with Templates

```json
{
  "x-bf-dim-host": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}"
}
```

Supports: `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`,
`{{CHAT_ID}}`, `{{MESSAGE_ID}}`.

---

## 9. UI Events

| State | Notification type | Content |
|-------|:----------------:|---------|
| `warning` | `info` | `"🛡️ Agent Loop Guard: {tool} called {n}x with same args."` |
| `final_warning` | `warning` | `"🛡️ Agent Loop Guard: {tool} called {n}x. Final warning."` |
| `blocked_tool` | `error` | `"🛡️ Agent Loop Guard: {tool} blocked after {n} identical calls."` |
| `runaway` | `error` | `"🛡️ Agent Loop Guard: Tool call limit reached ({n}/{max})."` |
| Counter (non-block) | `status` | `"🛡️ Remaining tool calls: {remaining}/{max}"` |

---

## 10. Risk Analysis

### Mitigated risks

| # | Risk | Mitigation |
|:-:|------|------------|
| R1 | Shallow copy discards reassigned `body["tools"]` | In-place slice assignment `tools[:] = [...]` mutates the shared list |
| R2 | Middleware executes tools from `metadata["tools"]` | Clear `__metadata__["tools"]` alongside `body["tools"]` |
| R3 | `tool_choice: "none"` causes raw DSML leakage | Leave `tool_choice` unset; Bifrost parses DSML into harmless tool_calls against empty metadata |
| R4 | DSML buffer corrupts `reasoning_content` | No buffering in `_stream()` — transparent SSE proxy |
