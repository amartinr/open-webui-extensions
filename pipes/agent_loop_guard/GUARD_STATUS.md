# Design Document: `_guard_status` — Agent Loop Guard Control System

**Version:** 2.0  
**Author:** Design team (in collaboration with Abel)  
**Based on:** Agent Loop Guard v1.1.0 (amartinr/open-webui-extensions)  
**Middleware source:** Open WebUI `backend/open_webui/utils/middleware.py` (main branch)

---

## 1. Purpose

Replace the current system of injecting system messages and plain text appended to tool results with a **unified mechanism based on a dummy tool managed entirely by the pipe**. This allows the agent to receive all control information (warnings, counters, blocks) through the native tool call / tool result channel, without contaminating real tool results.

---

## 2. Core Concept

The pipe exposes an additional tool called `_guard_status` that is **always available** in `body["tools"]` alongside the agent's other tools.

The pipe handles two scenarios:

- **Automatic injection:** At every iteration of the turn, the pipe fabricates and injects a complete pair (assistant with tool_call + tool result) into the message history reflecting the current guard state. The agent did **not** call this tool; the pipe inserts it to keep the agent informed.

- **Voluntary calls:** If the agent chooses to invoke `_guard_status` on its own initiative, the pipe intercepts the call and responds with real current state data.

Under no circumstances do calls to `_guard_status` (automatic or voluntary) count toward the agent's tool budget or toward consecutive duplicate detection.

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

### 3.5 Voluntary calls are intercepted by the pipe

If the LLM includes `_guard_status` in its `tool_calls`, the middleware's `chat_completion_tools_handler` or the tool‑call loop within `streaming_chat_response_handler` will attempt to execute it. Since `_guard_status` is in `tools`, the tool lookup will find it. The pipe must therefore **also** register a callable for `_guard_status` in the tools dictionary, or handle the interception before the middleware executes tools.

The simplest approach is to let the pipe handle `_guard_status` **before** forwarding to the gateway: if the pipe detects a tool_call to `_guard_status` in `extract_tool_calls_in_turn`, it removes it from the list and fabricates the result directly, never reaching the middleware's execution path.

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

Automatic injection occurs **at every iteration of the turn**, regardless of whether there is a warning or not. This ensures the agent always has visibility of the guard state.

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
2. The pipe scans backwards through `messages` to find the last `_guard_status` assistant message (identified by `tool_calls[0].function.name == "_guard_status"`).
3. If found, the pipe **replaces** that assistant message and its corresponding tool result message in-place with updated data.
4. If not found, the pipe **appends** a new assistant + tool pair at the end of `messages`.
5. The modified `body` is forwarded to the gateway.

---

## 7. Voluntary Agent Calls

If the agent decides to invoke `_guard_status` on its own initiative, the pipe detects it during tool call analysis and **does not forward the call to the gateway**. Instead, the pipe fabricates a tool result directly with real current state data and inserts it into the history.

### 7.1 How interception works

When the pipe calls `_extract_tool_calls_in_turn(messages)`, it scans the assistant messages for tool calls. Any call to `_guard_status` is:

1. Identified by name (`"_guard_status"`).
2. Excluded from the consecutive duplicate count and total tool call budget.
3. Responded to immediately by the pipe: it generates a tool result with current state and injects it into `messages`.
4. Removed from the list of tool calls that will be forwarded to the middleware for execution.

This means the middleware's tool‑call loop never sees `_guard_status` as a tool to execute — it is entirely handled within the pipe.

### 7.2 Differences from automatic injection

| Aspect | Automatic injection | Voluntary call |
|---|---|---|
| **Who initiates** | The pipe | The agent (LLM) |
| **Counts toward budget?** | No | No |
| **Counts toward consecutive detection?** | No | No |
| **Tool_call origin** | Fabricated by the pipe | Generated by the LLM |
| **Response** | Current state + possible warning | Current state only |
| **Accumulation?** | No — replaces previous pair | No — replaces previous pair |

---

## 8. Behavior During Blocks

### 8.1 Loop block

When the consecutive identical call threshold is exceeded:

1. The pipe removes from `body["tools"]` **only** the tool that is looping.
2. The `_guard_status` tool **remains** in `body["tools"]`.
3. The previous `_guard_status` pair is **replaced** with a new one carrying `status: "blocked_tool"` and a clear message.
4. The agent receives the request: it can use other real tools, query `_guard_status`, or summarise with the data it already has.

### 8.2 Runaway block (turn limit reached)

When the total tool call limit per turn is exceeded:

1. The pipe removes from `body["tools"]` **all real tools**.
2. The `_guard_status` tool **remains** in `body["tools"]`.
3. The previous `_guard_status` pair is **replaced** with a new one carrying `status: "runaway"` and a clear message.
4. The agent receives the request with no real tools: it can only query `_guard_status` or summarise.

---

## 9. Filtering in Turn Analysis

During tool call extraction from the turn (`_extract_tool_calls_in_turn`), any invocation of `_guard_status` (automatic or voluntary) is **excluded** from computation. This applies to:

- **Total tool count per turn** — does not affect the runaway limit.
- **Consecutive duplicate detection** — does not break or contribute to the chain.
- **Descending counter** — does not deduct from the budget.

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
| **Dummy tools in the system** | Not applicable | `_guard_status` always available |
| **Voluntary agent calls** | Not applicable | Supported: the agent can query its state |
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
| **Can `_guard_status` calls be intercepted before middleware execution?** | ✅ Yes — the pipe extracts tool calls from history and handles `_guard_status` internally, never forwarding it to the middleware's tool executor |
| **Does `bypass_system_prompt=True` affect routing?** | ❌ No — it only skips system prompt injection, the pipe still receives the call |
| **Does the tool‑call loop support dynamic tool removal?** | ✅ Yes — `body["tools"]` is read fresh from the pipe response on each iteration |

---

## 14. Future Considerations

- **Customizable name:** Could be exposed as a valve so the administrator can choose the tool name (in case of conflicts with a real tool).
- **Language localisation:** The `message` field could support different languages through a valve setting.
- **Format selection:** A valve could be added to toggle between Option A (structured fields) and Option B (minimal) without code changes.
- **State caching:** For extremely long turns, replacing the previous pair avoids context growth; no performance issues are anticipated.
