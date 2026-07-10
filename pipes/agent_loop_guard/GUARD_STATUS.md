# Design Document: `_guard_status` — Agent Loop Guard Control System

**Version:** 2.1  
**Author:** Design team (in collaboration with Abel)  
**Based on:** Agent Loop Guard v1.1.0 (amartinr/open-webui-extensions)  
**Middleware source:** Open WebUI `backend/open_webui/utils/middleware.py` (main branch)

---

## 1. Purpose

Replace the current system of injecting system messages and plain text appended to tool results with a **hybrid mechanism**:

- **Warnings (`warning`, `final_warning`)** use a fabricated `_guard_status` tool-call pair injected into the message history, **plus** a system message so the LLM actually receives the feedback.
- **Soft-blocks (`runaway`, `blocked_tool`)** remove tools from both `body["tools"]` (what the LLM sees) and `metadata["tools"]` (what the middleware executes), then inject a system message instructing the agent to summarise. No early return — the flow falls through to the normal forwarding path, which uses the modified tools list.

The hybrid emerged from practical testing: the `_guard_status` pair alone is invisible to the LLM (it gets stripped before forwarding), so system messages are essential for the agent to receive warnings and soft-block instructions.

---

## 2. Core Concept

The pipe exposes an additional tool called `_guard_status` that is **always available** in `body["tools"]` alongside the agent's other tools — except during soft-block, where no tools at all are added back.

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
| `body["tools"]` | Add `_guard_status` to the tool list. Remove specific tools on soft-block. Skip re-adding `_guard_status` during soft-block. |
| `body["messages"]` | Scan the full history for existing `_guard_status` pairs. Inject or replace them. Append system messages. |
| `body["tool_choice"]` | Reset if it targets a blocked tool. |

### 3.3 Tool list mutation must be in-place

The middleware loop constructs the next request as:

```python
new_form_data = {**form_data, ...}
```

This is a **shallow copy** of `form_data`. Lists inside `form_data` (including `body["tools"]`) are the **same objects** — the copy only copies references. So modifying the list **in-place** with slice assignment (`body["tools"][:] = [...]`) ensures the change survives across iterations. Reassigning a new list (`body["tools"] = [...]`) creates a new object that the original `form_data["tools"]` reference does not follow.

### 3.4 Tool execution must be blocked at the metadata level

The middleware executes tools by looking them up in `metadata["tools"]` (a dict of callable functions), **not** in `body["tools"]` (the list of tool specs sent to the LLM). Therefore, clearing only `body["tools"]` is insufficient — the agent's LLM may still produce DSML tool-call markup, which Bifrost converts to `delta.tool_calls`, and the middleware will try to execute them via `metadata["tools"]`.

The pipe also receives `__metadata__` (the **same object** as the middleware's `metadata`). Clearing `__metadata__["tools"]` (or removing individual tools from it) prevents the middleware from executing tools past the soft-block, even if Bifrost emits `tool_calls` from parsed DSML.

### 3.5 Pair replacement is safe

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

### 3.6 What happens if the LLM calls `_guard_status` voluntarily

The pipe is a custom router that forwards requests directly to the gateway via `httpx` — it does **not** go through Open WebUI's tool execution pipeline. The sequence when the LLM decides to call `_guard_status` on its own initiative is:

```
pipe() → gateway → LLM responds with tool_calls (incl. _guard_status)
→ middleware: _guard_status not found in metadata['tools'] → skip
   (harmless warning in server logs, no crash)
→ sanitize_tool_pairs() in the middleware cleans up the orphaned
   tool_call (no tool result to pair it with)
→ middleware re‑calls pipe() for the next iteration
→ pipe() auto‑injects the _guard_status pair as usual
```

**Key insight:** `_guard_status` lives in `body["tools"]` (what the LLM sees) but is **never registered** in Open WebUI's `metadata["tools"]` (the callable registry). When the middleware tries to look it up, it returns `"Tool _guard_status not found"`. This is a useful signal: the agent sees the error and learns that calling `_guard_status` fails, so it stops attempting it.

---

## 4. Tool Registration

The tool is added to `body["tools"]` before forwarding, **except** during soft-block states where no tools (including `_guard_status`) are added back. Its definition:

| Field | Value |
|---|---|
| **Name** | `_guard_status` |
| **Description** | Internal read‑only tool. Returns the current state of the agent loop guard: number of tool calls in the turn, consecutive identical calls, block status, and remaining budget. |
| **Parameters** | `{}` (none) |

No additional configuration is required on the gateway or in workspace models. The tool is managed entirely by the pipe.

---

## 5. Response Schema — Option B (Implemented)

The implementation uses **Option B**: minimal two-field format. This was chosen empirically — LLMs process a single narrative message better than multiple structured fields.

| Field | Type | Description |
|---|---|---|
| `status` | string | Current state: `"ok"`, `"warning"`, `"final_warning"`, `"blocked_tool"`, `"runaway"` |
| `message` | string | Single phrase describing the current state: what is happening, remaining budget, and recommended action. Self‑contained — the agent needs no other message to understand its situation. |

**Examples:**

```json
{"status": "ok", "message": "12/15 tool calls remaining."}
```

```json
{"status": "warning", "message": "smart_fetch_url called 2x with the same arguments. 11/15 tool calls remaining. Change your approach or summarise."}
```

```json
{"status": "final_warning", "message": "smart_fetch_url called 3x with the same arguments. 10/15 tool calls remaining. This is your final warning. Stop repeating and summarise."}
```

```json
{"status": "blocked_tool", "message": "TOOL REMOVED: smart_fetch_url blocked after 3 identical calls. 7/15 tool calls remaining. Other tools are still available or you may summarise now."}
```

```json
{"status": "runaway", "message": "TOOL LIMIT REACHED: 8/8. Tools used in this turn have been removed. Other tools are still available or you may summarise now."}
```

---

## 6. Automatic Injection into History

### 6.1 When injection happens

Automatic injection occurs on every iteration of the turn **once at least one real tool call has been made** (`total > 0`). On the very first call of a turn (before any tools run) nothing is injected — there is no guard state to report yet and injecting would add unnecessary tokens to conversations that never use tools.

### 6.2 Content by state

| Detected state | `status` in tool result | System message injected? | Are tools modified? |
|---|---|---|---|
| No repetition (consecutive < 2) | `"ok"` | No | No |
| First warning (consecutive == 2) | `"warning"` | **Yes** | No |
| Final warning (consecutive == intermediate threshold) | `"final_warning"` | **Yes** | No |
| Loop block (consecutive >= max) | `"blocked_tool"` | **Yes** | Yes: only the offending tool removed from `body["tools"]` and `metadata["tools"]` |
| Turn limit reached (total >= max, not looping) | `"runaway"` | **Yes** | Yes: all tools used this turn removed from `body["tools"]` and `metadata["tools"]` |

### 6.3 History management: replacement, not accumulation

Multiple `_guard_status` pairs are **never accumulated** in the history. On each iteration, the previous pair (assistant + tool) is **replaced** with the new one. This keeps the context lean and ensures there is never more than one `_guard_status` pair in the turn's message history.

### 6.4 Position in history

The injected pair is placed **at the end of the message history**, just before forwarding the request to the gateway. System messages for warnings/soft-blocks are appended immediately before or after the pair.

### 6.5 Implementation mechanism within the middleware loop

During each invocation of `pipe()` by the middleware's tool‑call loop:

1. The pipe receives `body["messages"]` containing the full conversation history including all prior tool calls and results.
2. The pipe scans backwards through `messages` to find the last `_guard_status` assistant message (identified by `tool_calls[0].id == "guard_status"`).
3. If found, the pipe **replaces** that assistant message and its corresponding tool result message (identified by `tool_call_id == "guard_status"`) in-place with updated data.
4. If not found and `total > 0`, the pipe **appends** a new assistant + tool pair at the end of `messages`.
5. For `warning`/`final_warning` states, a system message is also appended so the LLM receives feedback.
6. For `blocked_tool`/`runaway` states, tools are removed in-place from `body["tools"]` (via `[:]` slice assignment) and from `__metadata__["tools"]`. Then a system message is appended.
7. Execution falls through to the **single forwarding path** shared by all states. The `_guard_status` pair is stripped from `clean_messages`; system messages survive.

> **Important:** The `_guard_status` pair is **stripped from the forwarded payload** via `clean_messages` before reaching the gateway. This is necessary because fabricated assistant messages trigger `reasoning_content` validation errors on DeepSeek (thinking mode). The pair remains in `body["messages"]` (modified in-place) for the middleware loop to carry forward between iterations.

**Fabricated message IDs:** both the auto‑injected assistant message and tool result use a fixed, deterministic `tool_call_id` of `"guard_status"`. The assistant message's own `id` field is also set to `"guard_status"`. These fixed IDs make the pair trivially discoverable for the replacement mechanism and ensure `sanitize_tool_pairs()` in the middleware does not strip the pair (the fabricated `tool` message carries a `tool_call_id` that matches the assistant's `tool_calls[0].id`).

---

## 7. Voluntary Agent Calls (Not Implemented)

If the agent decides to invoke `_guard_status` on its own initiative, the middleware silently skips it (see §3.6). The pipe does **not** attempt to intercept, shortcut, or fabricate results for voluntary calls. The agent loses at most one tool‑call iteration; on the next `pipe()` invocation the normal automatic injection restores the guard state. This is an accepted trade‑off: the added implementation complexity of intercepting voluntary calls is not justified for this low‑probability, self‑resolving edge case.

**Real-world observation:** When the agent calls `_guard_status` voluntarily, it receives `"Tool _guard_status not found"`. This error is useful — the agent deduces that the guard system has blocked all tools, helping it understand the situation.

---

## 8. Behavior During Blocks

### 8.1 Escalation ladder priority

Loop detection takes precedence over runaway. Runaway only fires when `consecutive < block_threshold`:

```
runaway = max_calls > 0 and total >= max_calls and consecutive < block_threshold
```

This means an agent that hits both the loop threshold and the total-turn limit simultaneously gets `blocked_tool` (only the offending tool removed, other tools stay available) instead of `runaway` (all used tools removed). This is the more productive outcome — the agent can continue with other tools.

### 8.2 Loop block

When the consecutive identical call threshold is exceeded:

1. The pipe removes from `body["tools"]` **only** the tool that is looping, using **in-place slice assignment** (`tools[:] = [...]`) so the change survives the middleware's shallow copy.
2. The pipe removes the same tool from `__metadata__["tools"]` so the middleware cannot execute it.
3. A **system message** with `status: "blocked_tool"` is appended to messages.
4. Execution falls through to the normal forwarding path. The `_guard_status` pair is not re-added (guarded by `state["status"]` check).
5. `clean_messages` strips any `_guard_status` pairs before forwarding.
6. The agent receives the request: it can use other real tools or summarise.

### 8.3 Runaway block (turn limit reached)

When the total tool call limit per turn is exceeded and there is no loop:

1. The pipe computes the set of tool names used in this turn: `{tc["name"] for tc in history}`.
2. The pipe removes **only those tools** from `body["tools"]` (in-place) and from `__metadata__["tools"]`.
3. A **system message** with `status: "runaway"` is appended.
4. Identical to loop block from here: fall through, no `_guard_status` re-added, `clean_messages`, forward.

This is **symmetric** with loop block — both remove only the problematic tools (offending tool for loop, all used tools for runaway) and leave unused tools available.

### 8.4 No early return

Unlike earlier designs, soft-block does **not** use an early return with a duplicated forward call. Instead:
- Tools are cleared in-place (both `body["tools"]` and `__metadata__["tools"]`)
- A system message is injected
- Execution continues to the **single, shared forwarding path** at the end of `pipe()`

This eliminates code duplication and ensures consistent error handling.

---

## 9. Filtering in Turn Analysis

During tool call extraction from the turn (`_extract_tool_calls_in_turn`), the auto‑injected `_guard_status` pair is **excluded** from computation. The assistant message with `tool_calls[0].function.name == "_guard_status"` is skipped entirely — it is not a real tool call, it is a status update injected by the pipe. This applies to:

- **Total tool count per turn** — the auto‑injected pair does not affect the runaway limit.
- **Consecutive duplicate detection** — the pair does not break or contribute to the chain.
- **Remaining calls** — derived from real tool calls only.

---

## 10. Configuration (Valves)

The `INJECTION_POSITION` and `SHOW_TOOL_COUNTER` valves from v1 have been **removed**. The injection position is fixed: at the end of the history, as an independent message pair. The tool counter is always included in the `_guard_status` tool result message.

| Valve | Status | Description |
|---|---|---|
| `GATEWAY_BASE_URL` | Unchanged | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | Unchanged | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | Unchanged | Credential value |
| `GATEWAY_CUSTOM_HEADERS` | Unchanged | JSON object of extra HTTP headers |
| `MAX_TOOL_CALLS_PER_TURN` | Unchanged | Max tool calls before soft-block |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | Unchanged | Consecutive identical calls before soft-block |
| `TOOL_BLOCKLIST` | Unchanged | Comma/newline-separated tool names to remove |
| `INJECTION_POSITION` | **Removed** | No longer configurable |
| `SHOW_TOOL_COUNTER` | **Removed** | Always included in `_guard_status` message |

---

## 11. User Interface Events

The notification and status events emitted by the pipe (`__event_emitter__`):

| State | Notification type | Content |
|---|---|---|
| `warning` | `info` | `"🛡️ Agent Loop Guard: {tool} called {n}x with same args."` |
| `final_warning` | `warning` | `"🛡️ Agent Loop Guard: {tool} called {n}x. Final warning."` |
| `blocked_tool` | `error` | `"🛡️ Agent Loop Guard: {tool} blocked after {n} identical calls."` |
| `runaway` | `error` | `"🛡️ Agent Loop Guard: Tool call limit reached ({n}/{max})."` |
| Counter (non-block states) | `status` | `"🛡️ Remaining tool calls: {remaining}/{max}"` |

---

## 12. Advantages Over the Previous Design

| Aspect | Previous design (system messages + merge_last_tool) | New design (`_guard_status` hybrid) |
|---|---|---|
| **Real tool results** | Contaminated with appended plain text counter | Intact, unmodified |
| **Warning format** | Plain text in system messages | System message + `_guard_status` pair (dual channel) |
| **Agent visibility** | Medium (may ignore system messages) | High — system message delivers instruction, `_guard_status` pair enriches context |
| **History accumulation** | Yes, the counter was repeatedly appended | No, the previous pair is replaced (max 1 pair) |
| **Self‑contained messages** | No — context from previous iterations was needed | Yes — each message contains the full current state |
| **Software-block persistence** | Unreliable — `body["tools"] = [...]` created a new list, lost on shallow copy | `tools[:] = [...]` mutates in-place, survives middleware loop |
| **Middleware execution blocking** | Only `body["tools"]` modified, `metadata["tools"]` untouched | Both `body["tools"]` and `__metadata__["tools"]` cleared |
| **Middleware compatibility** | Unaware of middleware structure | Validated against `middleware.py` — pipe is called on every tool‑call iteration; shallow copy behaviour documented |
| **DSML leakage** | Not applicable (v1 didn't use DeepSeek) | Mitigated: `tool_choice` left unset so Bifrost stays in tool-calling mode and parses DSML into `tool_calls` that fail against the empty `metadata["tools"]` |

---

## 13. Middleware Integration Summary

| Concern | Verdict |
|---|---|
| **Is `pipe()` called on each tool‑call iteration?** | ✅ Yes — the middleware's `streaming_chat_response_handler` routes through the pipe model ID on every loop iteration |
| **Can the pipe modify `body["tools"]` in-place?** | ✅ Yes — `tools[:] = [...]` mutates the shared list object, surviving shallow copies |
| **Can the pipe modify `body["messages"]`?** | ✅ Yes — the pipe can scan, inject, and replace messages in the full history |
| **Can the pipe block tool execution at the middleware level?** | ✅ Yes — `__metadata__["tools"]` is the same object as the middleware's `metadata["tools"]`. Removing tools from it prevents execution. |
| **Is pair replacement safe?** | ✅ Yes — the pipe sees the complete accumulated history on each call |
| **What if the LLM calls `_guard_status` voluntarily?** | ✅ Harmless — the middleware skips it silently (`tool not found`), `sanitize_tool_pairs()` cleans the orphaned call. The pipe auto‑injects normally on the next iteration. |
| **Does `bypass_system_prompt=True` affect routing?** | ❌ No — it only skips system prompt injection, the pipe still receives the call |
| **Does the tool‑call loop support dynamic tool removal?** | ✅ Yes — `body["tools"]` is read fresh from the pipe response on each iteration |

---

## 14. Risk Analysis

### 14.1 Risks analysed and dismissed

| Risk | Why it is not a real concern |
|---|---|
| `sanitize_tool_pairs()` strips the fabricated pair | The fixed `tool_call_id` (`"guard_status"`) matches the assistant's `tool_calls[0].id` — the sanitizer requires this match and therefore **preserves** the pair. |
| `process_messages_with_output()` corrupts the pair | This function only touches messages with an `output` field. The fabricated pair has no `output` field — it is **ignored** entirely. |
| The LLM ignores `_guard_status`, rendering it useless | The escalation ladder is **coercive**, not persuasive. Warnings inform the agent via system messages, but soft‑block acts directly on tools in both `body` and `metadata` — the LLM is forced to stop regardless. `_guard_status` is informative, not critical. |
| Two `_guard_status` pairs accumulate in history | If the LLM calls `_guard_status` voluntarily, `sanitize_tool_pairs()` cleans the orphaned call **before** the next `pipe()` invocation. The pipe never sees a voluntary `_guard_status` tool_call — only its own fabricated pair. |
| `_guard_status` is visible to the end user in the chat UI | The pair is injected into `body["messages"]` (the **input** to the LLM). The gateway's SSE stream (the **output** rendered by the frontend) contains only the LLM's own response — the fabricated pair is never emitted. |

### 14.2 Real risks and mitigations

| # | Risk | Mitigation | Lines |
|---|------|------------|:-----:|
| R1 | `_extract_tool_calls_in_turn()` counts the auto‑injected `_guard_status` pair as a real tool call, inflating `total` and triggering runaway too early | Skip any `tool_calls` entry where `function.name == "_guard_status"` during extraction | ~1 |
| R2 | The remaining‑calls counter in the `_guard_status` response is off by one because the injected pair counts itself in `total` | Same mitigation as R1 — the pair is excluded from `total`, so `remaining = max - total` is accurate | ~0 (same line) |
| R3 | Shallow copy in `new_form_data = {**form_data, ...}` discards reassigned `body["tools"]` | Use **in-place slice assignment** (`tools[:] = [...]`) to mutate the shared list object | ~1 per assignment |
| R4 | Middleware executes tools from `metadata["tools"]` even when `body["tools"]` is empty | Clear `__metadata__["tools"]` (or remove individual tools from it) alongside `body["tools"]` | ~2 per block type |
| R5 | `tool_choice: "none"` causes Bifrost to stop parsing DSML, leaking raw DSML as text | **Don't set** `tool_choice`. Leave it as `"auto"` so Bifrost stays in tool-calling mode and parses DSML into structured `tool_calls` that fail harmlessly against the empty `metadata["tools"]` | 0 |
| R6 | DSML buffer in `_stream()` corrupts `reasoning_content` by merging it into `content` | **Don't buffer in `_stream()`**. Leave it as a transparent SSE proxy. DSML that escapes as text is a cosmetic issue, not a functional one. | 0 |

---

## 15. Guarding `_guard_status` Against Removal

The `_guard_status` tool is **never removable** by the tool blocklist (`TOOL_BLOCKLIST`). Even if an administrator accidentally adds `_guard_status` to the blocklist, `_apply_tool_blocklist()` explicitly preserves it — the filter skips any tool whose `function.name == "_guard_status"`.

> **Note:** During soft-block (runaway or loop block), `_guard_status` is NOT re-added to `body["tools"]` because the `_add_guard_status_tool(body)` call is guarded by `if state["status"] not in ("runaway", "blocked_tool")`. This is a deliberate design: during soft-block no tools at all should be visible, so the agent is forced to summarise. See §8 for details.

---

## 16. Future Considerations

- **Phase 6 — Cleanup:** Remove dead code functions (`_warning_msg`, `_final_warning_msg`, `_runaway_instruction`, `_loop_blocked_tool_instruction`, `_inject`, `_append_tool_counter`), update README valves table, bump version to 2.0.0.
- **Customizable name:** Could be exposed as a valve so the administrator can choose the tool name (in case of conflicts with a real tool).
- **Language localisation:** The `message` field could support different languages through a valve setting.
- **Voluntary call handling:** If future models start calling `_guard_status` frequently, the pipe could detect orphaned `_guard_status` tool_calls (from the middleware's skip) and fabricate tool results for them a posteriori in the next `pipe()` iteration. For now this is deferred — see §7.
