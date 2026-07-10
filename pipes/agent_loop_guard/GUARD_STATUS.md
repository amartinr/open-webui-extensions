# Design Document: `_guard_status` — Agent Loop Guard Control System

**Version:** 2.0  
**Author:** Design team (in collaboration with Abel)  
**Based on:** Agent Loop Guard v1.1.0 (amartinr/open-webui-extensions)  
**Middleware source:** Open WebUI `backend/open_webui/utils/middleware.py` (main branch)

---

## 1. Purpose

Replace the current system of injecting system messages and plain text appended to tool results with a **mechanism based on a dummy tool managed by the pipe** for warnings, complemented by direct system messages for soft-blocks. This hybrid allows warnings to travel through the native tool call / tool result channel without contaminating real tool results, while soft-blocks retain the authoritative directiveness of system messages.

**Implementation note:** The original design envisioned a fully unified mechanism. In practice, a hybrid emerged: warnings use `_guard_status` pairs, while soft-blocks (runaway, blocked_tool) use early-return with system messages — matching the tried-and-tested master branch behaviour for the critical stop-the-agent case. See §5.2 and §6.5.

---

## 2. Core Concept

The pipe exposes an additional tool called `_guard_status` that is **always available** in `body["tools"]` alongside the agent's other tools.

**Automatic injection:** At every iteration of the turn, the pipe fabricates and injects a complete pair (assistant with tool_call + tool result) into the message history reflecting the current guard state. The agent did **not** call this tool; the pipe inserts it to keep the agent informed.

Under no circumstances do calls to `_guard_status` count toward the agent's tool budget or toward consecutive duplicate detection.

---

## 3. Viability Analysis — Middleware Integration

### 3.1 The pipe is invoked on every tool‑call iteration

The middleware in `streaming_chat_response_handler` runs a tool‑call loop:

```
while tool_calls and (iterations < CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS):
    execute tools → collect results → convert_output_to_messages()
    new_form_data["messages"] = form_data["messages"] + tool_messages
    res = generate_chat_completion(request, new_form_data, ...)
```

The `model` parameter passed to `generate_chat_completion` retains the original pipe‑prefixed model ID (e.g. `pipe-uuid.deepseek/deepseek-v4-flash`). This means **every iteration of the tool‑call loop routes back through the pipe's `pipe()` method**.

This is the cornerstone of the design: the pipe has the opportunity to inspect, inject, and modify the message history and tool list on every single turn iteration.

### 3.2 The pipe has full control over `body["tools"]` and `body["messages"]`

The `pipe()` method receives the complete `body` dictionary and can modify it freely before forwarding to the gateway:

| Aspect | What the pipe can do |
|---|---|
| `body["tools"]` | Add `_guard_status` to the tool list. Remove specific tools on soft-block. Leave `_guard_status` in place. |
| `body["messages"]` | Scan the full history for existing `_guard_status` pairs. Inject or replace them. Append tool results from `_guard_status`. |
| `body["tool_choice"]` | Reset if it targets a blocked tool. |

### 3.3 Pair replacement is safe

The middleware constructs messages for the next iteration as:

```python
new_form_data['messages'] = [
    *form_data['messages'],          # all prior history
    *convert_output_to_messages(output),  # tool_calls + tool results from this turn
]
```

Since the pipe receives the **complete accumulated history** on every call, it can:

1. Scan backwards through `messages` to find the last `_guard_status` assistant + tool pair.
2. Replace that pair in-place with updated state data.
3. If no pair exists yet, append a new one at the end of the history.

This guarantees at most one `_guard_status` pair in the message history at any time.

### 3.4 Soft‑block mechanics

When the pipe decides to soft‑block:

1. It removes the offending tool(s) from `body["tools"]`.
2. It **keeps** `_guard_status` in `body["tools"]`.
3. It replaces the `_guard_status` pair in `messages` with `status: "blocked_tool"` or `status: "runaway"`.
4. The modified `body` is forwarded to the gateway.

The LLM receives the request with no real tools (or one fewer tool) but still sees `_guard_status` available. It can call it voluntarily to check state, or summarise with the data it already has.

### 3.5 What happens if the LLM calls `_guard_status` voluntarily

The pipe is a custom router that forwards requests directly to the gateway via `httpx` — it does **not** go through Open WebUI's tool execution pipeline. The sequence when the LLM decides to call `_guard_status` on its own initiative is:

```
pipe() → gateway → LLM responds with tool_calls (incl. _guard_status)
→ middleware: _guard_status not found in its tool registry → skip
   (harmless warning in server logs, no crash)
→ sanitize_tool_pairs() in the middleware cleans up the orphaned
   tool_call (no tool result to pair it with)
→ middleware re‑calls pipe() for the next iteration
→ pipe() auto‑injects the _guard_status pair as usual
```

**Key insight:** `_guard_status` lives in `body["tools"]` (what the LLM sees) but is **never registered** in Open WebUI's internal tool callable registry (`get_tools()` / `get_builtin_tools()`). When the middleware's `chat_completion_tools_handler` tries to look it up, it logs `Tool "_guard_status" not found` and returns silently — no error, no tool result. The `sanitize_tool_pairs()` function then removes the orphaned assistant message (tool_call without matching tool result).

**Conclusion:** Voluntary calls to `_guard_status` are harmless. The agent loses at most one iteration (the middleware skips the call, the pipe auto-injects normally on the next iteration). No special interception logic is needed — the design does not attempt to intercept or shortcut these calls. This is a deliberate simplification: the added complexity of intercepting voluntary calls is not justified for an edge case that is both improbable (the LLM has no reason to call a tool marked "Internal read‑only" unprompted) and self‑resolving.

**Implementation note:** When `_guard_status` is voluntarily called, the error `"Tool _guard_status not found"` is returned as a tool result with a random UUID `tool_call_id`. This error message survives the `clean_messages` filter (which only strips pairs with `tool_call_id == "guard_status"`). This is harmless — the LLM learns that calling `_guard_status` fails and stops attempting it.

---

## 4. Tool Registration

The tool is added to `body["tools"]` at the start of each `pipe()` call. Its definition is as follows:

| Field | Value |
|---|---|
| **Name** | `_guard_status` |
| **Description** | Internal read‑only tool. Returns the current state of the agent loop guard: number of tool calls in the turn, consecutive identical calls, block status, and remaining budget. |
| **Parameters** | `{}` (none) |

No additional configuration is required on the gateway or in workspace models. The tool is managed entirely by the pipe.

---

## 5. Response Schema — Two Alternative Formats

Two possible formats are under consideration for the `_guard_status` tool result. Both are valid; the final choice depends on testing which the agent processes more effectively.

---

### Option A: Structured fields

The response includes individual fields for each piece of state information, plus a natural language message for clarity.

| Field | Type | Description |
|---|---|---|
| `status` | string | Current state: `"ok"`, `"warning"`, `"final_warning"`, `"blocked_tool"`, `"runaway"` |
| `tool` | string or null | Name of the tool involved in the loop (if applicable) |
| `consecutive` | integer | Number of consecutive identical tool calls detected |
| `total_calls_in_turn` | integer | Total tool calls made in the current turn |
| `max_calls_per_turn` | integer | Maximum configured tools per turn |
| `remaining_calls` | integer | Remaining budget (max - total) |
| `blocked` | boolean | Whether any tool has been blocked |
| `blocked_tools` | list of strings | List of tools blocked in this turn |
| `message` | string | Human‑readable message with instructions or warnings for the agent |

**Examples:**

```json
{
  "status": "ok",
  "tool": null,
  "consecutive": 0,
  "total_calls_in_turn": 3,
  "max_calls_per_turn": 15,
  "remaining_calls": 12,
  "blocked": false,
  "blocked_tools": [],
  "message": "Remaining tool calls: 12/15"
}
```

```json
{
  "status": "warning",
  "tool": "search_web",
  "consecutive": 2,
  "total_calls_in_turn": 4,
  "max_calls_per_turn": 15,
  "remaining_calls": 11,
  "blocked": false,
  "blocked_tools": [],
  "message": "search_web called 2x with same args. Change approach or summarize."
}
```

```json
{
  "status": "blocked_tool",
  "tool": "search_web",
  "consecutive": 4,
  "total_calls_in_turn": 7,
  "max_calls_per_turn": 15,
  "remaining_calls": 8,
  "blocked": true,
  "blocked_tools": ["search_web"],
  "message": "TOOL REMOVED: search_web blocked after 4 identical calls."
}
```

---

### Option B: Minimal fields (status + message)

The response includes only two fields. All state information is conveyed through a single, self‑contained natural language phrase in the `message` field.

| Field | Type | Description |
|---|---|---|
| `status` | string | Current state: `"ok"`, `"warning"`, `"final_warning"`, `"blocked_tool"`, `"runaway"` |
| `message` | string | Single phrase describing the current state: what is happening, remaining budget, and recommended action. Fully self‑contained — the agent needs no other message to understand its situation. |

**Examples:**

```json
{
  "status": "ok",
  "message": "12/15 tool calls remaining."
}
```

```json
{
  "status": "warning",
  "message": "search_web called 2x with the same arguments. 11/15 tool calls remaining. Change your approach or summarise."
}
```

```json
{
  "status": "final_warning",
  "message": "search_web called 3x with the same arguments. 10/15 tool calls remaining. This is your final warning. Stop repeating and summarise."
}
```

```json
{
  "status": "blocked_tool",
  "message": "search_web blocked after 4 identical calls. 8/15 tool calls remaining. Other tools are still available or you may summarise now."
}
```

```json
{
  "status": "runaway",
  "message": "Tool call limit reached: 15/15. No more tool calls this turn. Summarise now."
}
```

---

### Comparison

| Aspect | Option A (structured fields) | Option B (minimal) |
|---|---|---|
| **Parsability** | High — each value in its own field | Low — all information embedded in text |
| **Agent comprehension** | Medium — the agent must cross‑reference fields | High — a single sentence says everything |
| **Token cost per message** | Higher (~120–180 tokens) | Lower (~60–100 tokens) |
| **Future extensibility** | Easy — add new fields | Harder — must be incorporated into the message text |
| **Risk of misinterpretation** | Medium — agent may ignore numeric fields | Low — narrative is natural for LLMs |

The final choice between Option A and Option B should be informed by empirical testing with the target model.

---

## 6. Automatic Injection into History

### 6.1 When injection happens

Automatic injection occurs on every iteration of the turn **once at least one real tool call has been made** (`total > 0`). On the very first call of a turn (before any tools run) nothing is injected — there is no guard state to report yet and injecting would add unnecessary tokens to conversations that never use tools.

### 6.2 Content by state

| Detected state | `status` in tool result | Are tools modified? |
|---|---|---|
| No repetition (consecutive < 2) | `"ok"` | No |
| First warning (consecutive == 2) | `"warning"` | No |
| Final warning (consecutive == intermediate threshold) | `"final_warning"` | No |
| Loop block (consecutive >= max) | `"blocked_tool"` | Yes: the offending tool is removed |
| Turn limit reached (total >= max) | `"runaway"` | Yes: all real tools are removed |

### 6.3 History management: replacement, not accumulation

Multiple `_guard_status` pairs are **never accumulated** in the history. On each iteration, the previous pair (assistant + tool) is **replaced** with the new one. This keeps the context lean and ensures there is never more than one `_guard_status` pair in the turn's message history.

Since each message is self‑contained — it includes the full current state — the agent does not need to look at previous iterations to understand where it stands. A single message tells it everything it needs to know.

### 6.4 Position in history

The injected pair is placed **at the end of the message history**, just before forwarding the request to the gateway. This way, the agent processes it as the most recent information available.

### 6.5 Implementation mechanism within the middleware loop

During each invocation of `pipe()` by the middleware's tool‑call loop:

1. The pipe receives `body["messages"]` containing the full conversation history including all prior tool calls and results.
2. The pipe scans backwards through `messages` to find the last `_guard_status` assistant message (identified by `tool_calls[0].function.name == "_guard_status"` and `tool_calls[0].id == "guard_status"`).
3. If found, the pipe **replaces** that assistant message and its corresponding tool result message (identified by `tool_call_id == "guard_status"`) in-place with updated data.
4. If not found and `total > 0`, the pipe **appends** a new assistant + tool pair at the end of `messages`.
5. The modified `body` is forwarded to the gateway.

> **Important:** The `_guard_status` pair is **stripped from the forwarded payload** via `clean_messages` before reaching the gateway. This is necessary because fabricated assistant messages trigger `reasoning_content` validation errors on DeepSeek (thinking mode). The pair remains in `body["messages"]` (modified in-place) for the middleware loop to carry forward between iterations.

**Fabricated message IDs:** both the auto‑injected assistant message and tool result use a fixed, deterministic `tool_call_id` of `"guard_status"`. The assistant message's own `id` field is also set to `"guard_status"`. These fixed IDs make the pair trivially discoverable for the replacement mechanism and ensure `sanitize_tool_pairs()` in the middleware does not strip the pair (the fabricated `tool` message carries a `tool_call_id` that matches the assistant's `tool_calls[0].id`).

---

## 7. Voluntary Agent Calls (Not Implemented)

If the agent decides to invoke `_guard_status` on its own initiative, the middleware silently skips it (see §3.5). The pipe does **not** attempt to intercept, shortcut, or fabricate results for voluntary calls. The agent loses at most one tool‑call iteration; on the next `pipe()` invocation the normal automatic injection restores the guard state. This is an accepted trade‑off: the added implementation complexity of intercepting voluntary calls is not justified for this low‑probability, self‑resolving edge case.

---

## 8. Behavior During Blocks

### 8.1 Loop block

When the consecutive identical call threshold is exceeded:

1. The pipe removes from `body["tools"]` **only** the tool that is looping.
2. The pipe takes an **early return** — it does NOT continue to `_add_guard_status_tool()`, so `_guard_status` does NOT appear in `body["tools"]` for this request. This matches the original master branch behaviour where no tools were re-added after removal.
3. A **system message** with the block instruction is appended to messages.
4. The `_guard_status` pair in messages is stripped before forwarding (via `clean_messages`).
5. The agent receives the request: it can use other real tools or summarise.

### 8.2 Runaway block (turn limit reached)

When the total tool call limit per turn is exceeded:

1. The pipe removes from `body["tools"]` **all real tools** (`body.pop("tools", None)`).
2. The pipe takes an **early return** — no `_guard_status` tool is added back.
3. A **system message** with the runaway instruction is appended to messages.
4. The agent receives the request with no tools available at all, forcing it to summarise.

> **Implementation note:** The original design specified that `_guard_status` should remain available during soft-blocks so the agent could query guard state. However, during testing with DeepSeek, the presence of any tool in `body["tools"]` led the LLM to attempt further tool calls, preventing it from summarising. The early-return approach (removing all tools + system message) proved more reliable for stopping runaway agents.

---

## 9. Filtering in Turn Analysis

During tool call extraction from the turn (`_extract_tool_calls_in_turn`), the auto‑injected `_guard_status` pair is **excluded** from computation. The assistant message with `tool_calls[0].function.name == "_guard_status"` is skipped entirely — it is not a real tool call, it is a status update injected by the pipe. This applies to:

- **Total tool count per turn** — the auto‑injected pair does not affect the runaway limit.
- **Consecutive duplicate detection** — the pair does not break or contribute to the chain.
- **Descending counter** — the counter in the `_guard_status` response reflects real tool calls only.

---

## 10. Configuration (Valves)

No new valves are added. This functionality supersedes the existing valves related to injection mode:

| Current valve | Impact of the new design |
|---|---|
| `INJECTION_POSITION` | **Removed.** The injection position is fixed: at the end of the history, as an independent message pair. |
| `SHOW_TOOL_COUNTER` | **Removed.** The counter is always included in the `_guard_status` tool result (either as a structured field or embedded in the message). |

All other valves (`MAX_TOOL_CALLS_PER_TURN`, `MAX_CONSECUTIVE_BEFORE_BLOCK`, `TOOL_BLOCKLIST`, etc.) remain unchanged.

---

## 11. User Interface Events

The notification and status events emitted by the pipe (`__event_emitter__`) are kept but adjusted to reflect `_guard_status` terminology:

| State | Notification type | Content |
|---|---|---|
| `warning` | `info` | `"🛡️ Agent Loop Guard: {tool} called {n}x with same args."` |
| `final_warning` | `warning` | `"🛡️ Agent Loop Guard: {tool} called {n}x. Final warning."` |
| `blocked_tool` | `error` | `"🛡️ Agent Loop Guard: {tool} blocked after {n} identical calls."` |
| `runaway` | `error` | `"🛡️ Agent Loop Guard: Tool call limit reached ({n}/{max})."` |
| Counter | `status` | `"🛡️ Remaining tool calls: {remaining}/{max}"` |

---

## 12. Advantages Over the Previous Design

| Aspect | Previous design (system messages + merge_last_tool) | New design (`_guard_status`) |
|---|---|---|
| **Real tool results** | Contaminated with appended plain text | Intact, unmodified |
| **Warning format** | Plain text in system messages or appended to tool results | Structured JSON (Option A) or compact status + message (Option B) |
| **Agent visibility** | Medium (may ignore system messages) | High (tool result is a priority channel) |
| **History accumulation** | Yes, the counter was repeatedly appended | No, the previous pair is replaced |
| **Self‑contained messages** | No — context from previous iterations was needed | Yes — each message contains the full current state |
| **Guard visibility** | Warnings buried in system messages or appended text | `_guard_status` always visible as a tool result in the native tool‑call channel |
| **Transparency** | Warnings visible but in awkward format | Warnings visible in the LLM's native format |
| **Middleware compatibility** | Unaware of middleware structure | Validated against `middleware.py` — pipe is called on every tool‑call iteration |

---

## 13. Middleware Integration Summary

| Concern | Verdict |
|---|---|
| **Is `pipe()` called on each tool‑call iteration?** | ✅ Yes — the middleware's `streaming_chat_response_handler` routes through the pipe model ID on every loop iteration |
| **Can the pipe modify `body["tools"]`?** | ✅ Yes — the pipe can add `_guard_status` and remove tools on soft-block |
| **Can the pipe modify `body["messages"]`?** | ✅ Yes — the pipe can scan, inject, and replace messages in the full history |
| **Is pair replacement safe?** | ✅ Yes — the pipe sees the complete accumulated history on each call |
| **What if the LLM calls `_guard_status` voluntarily?** | ✅ Harmless — the middleware skips it silently (`tool not found`), `sanitize_tool_pairs()` cleans the orphaned call. The pipe auto‑injects normally on the next iteration. No interception needed. |
| **Does `bypass_system_prompt=True` affect routing?** | ❌ No — it only skips system prompt injection, the pipe still receives the call |
| **Does the tool‑call loop support dynamic tool removal?** | ✅ Yes — `body["tools"]` is read fresh from the pipe response on each iteration |

---

## 14. Risk Analysis

### 14.1 Risks analysed and dismissed

| Risk | Why it is not a real concern |
|---|---|
| `sanitize_tool_pairs()` strips the fabricated pair | The fixed `tool_call_id` (`"guard_status"`) matches the assistant's `tool_calls[0].id` — the sanitizer requires this match and therefore **preserves** the pair. |
| `process_messages_with_output()` corrupts the pair | This function only touches messages with an `output` field. The fabricated pair has no `output` field — it is **ignored** entirely. |
| The LLM ignores `_guard_status`, rendering it useless | The escalation ladder is **coercive**, not persuasive. Warnings inform the agent, but soft‑block acts directly on `body["tools"]` — the LLM is forced to stop regardless of whether it understood the warning. `_guard_status` is informative, not critical. |
| Two `_guard_status` pairs accumulate in history | If the LLM calls `_guard_status` voluntarily, `sanitize_tool_pairs()` cleans the orphaned call **before** the next `pipe()` invocation. The pipe never sees a voluntary `_guard_status` tool_call — only its own fabricated pair. |
| `_guard_status` is visible to the end user in the chat UI | The pair is injected into `body["messages"]` (the **input** to the LLM). The gateway's SSE stream (the **output** rendered by the frontend) contains only the LLM's own response — the fabricated pair is never emitted. |

### 14.2 Real risks and mitigations

| # | Risk | Mitigation | Lines |
|---|------|------------|:-----:|
| R1 | `_extract_tool_calls_in_turn()` counts the auto‑injected `_guard_status` pair as a real tool call, inflating `total` and triggering runaway too early | Skip any `tool_calls` entry where `function.name == "_guard_status"` during extraction | ~1 |
| R2 | The remaining‑calls counter in the `_guard_status` response is off by one because the injected pair counts itself in `total` | Same mitigation as R1 — the pair is excluded from `total`, so `remaining = max - total` is accurate | ~0 (same line) |
| R3 | `_soft_block()` in runaway mode calls `body.pop("tools", None)`, which would also remove `_guard_status` | **Resolution:** The early-return approach (see §8.2) avoids re-adding `_guard_status` after `pop`. The agent receives a system message instead of a `_guard_status` pair, and the absence of tools forces it to summarise. This proved more effective than keeping `_guard_status` visible. | ~1 |

---

## 15. Guarding `_guard_status` Against Removal

The `_guard_status` tool is **never removable** by the tool blocklist (`TOOL_BLOCKLIST`). Even if an administrator accidentally adds `_guard_status` to the blocklist, `_apply_tool_blocklist()` explicitly preserves it — the filter skips any tool whose `function.name == "_guard_status"`.

> **Note:** During soft-block (runaway or loop block), `_guard_status` is NOT re-added to `body["tools"]` because the early return skips `_add_guard_status_tool()`. This is a deliberate trade-off: the agent receives a system message instead, and the absence of tools forces summarisation. See §8 for details.

---

## 16. Future Considerations

- **Customizable name:** Could be exposed as a valve so the administrator can choose the tool name (in case of conflicts with a real tool).
- **Language localisation:** The `message` field could support different languages through a valve setting.
- **Format selection:** A valve could be added to toggle between Option A (structured fields) and Option B (minimal) without code changes.
- **State caching:** For extremely long turns, replacing the previous pair avoids context growth; no performance issues are anticipated.
- **Voluntary call handling:** If future models start calling `_guard_status` frequently, the pipe could detect orphaned `_guard_status` tool_calls (from the middleware's skip) and fabricate tool results for them a posteriori in the next `pipe()` iteration. For now this is deferred — see §7.
