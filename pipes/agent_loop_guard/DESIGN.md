# Design Document: Agent Loop Guard

**Version:** 3.1  
**Based on:** `agent_loop_guard.py` (current implementation)

---

## 1. Purpose

Prevent AI agents in Open WebUI from entering infinite tool-calling loops by
analysing conversation history **on every iteration** of the middleware's
tool-call loop, replacing the offending tool result with a guard message
that instructs the agent to stop repeating or summarise.

---

## 2. Why a Pipe Instead of a Filter

Open WebUI offers two extensibility mechanisms that could intercept the LLM
request path:

| Capability | Filter (`inlet`) | Pipe (`pipe`) |
|-----------|:----------------:|:--------------:|
| Called on each tool-call iteration | ✅ | ✅ |
| Detect consecutive duplicates | ✅ | ✅ |
| Inject warning messages | ✅ | ✅ |
| **Remove tools from body** | ❌ Unreliable | ✅ **Definitive** |
| **Skip LLM call / force-terminate** | ❌ Must return body | ✅ Returns string (soft-block preferred) |
| **Manifold** (dynamic model discovery) | ❌ | ✅ |
| **Proxy + prefix stripping** | ❌ | ✅ |

Since [commit 5064506](https://github.com/open-webui/open-webui/commit/5064506de4eb6c0aae560c82b79fcf8f1a56c123),
both Filters and Pipes are invoked on every tool-call iteration. However,
only a Pipe can **definitively remove tools** from the request body (via
in-place slice assignment that survives the middleware's shallow copy),
**skip the LLM call entirely**, and act as a **manifold proxy** with
dynamic model discovery.

---

## 3. Core Mechanism

The pipe sits between Open WebUI and the LLM gateway. On every tool-call
iteration, the middleware calls `pipe()` with the accumulated message history.
The pipe:

1. **Analyses** the current turn's tool calls using `_analyse()` — scans
   backwards from the end of messages until the last user message, collects
   real tool calls (skipping those whose results were already replaced by the
   guard), counts consecutive identical calls, and decides whether to block.
2. **Blocks** by replacing the content of the most recent tool result with a
   guard message instructing the agent to stop. Messages are then forwarded
   to the gateway so the LLM receives the instruction.
3. **Does not remove tools from the body or metadata** — relies on the guard
   message to steer the LLM. If the LLM ignores the instruction and repeats,
   the guard fires again on the next iteration (the guarded call is tracked
   via its `tool_call_id`).

### Why result replacement instead of tool removal?

| Approach | Issue |
|----------|-------|
| **Remove tools from body** | `body["tools"]` is set once per turn from the workspace model. Mutating it mid-turn would permanently deny the agent access to tools for the rest of the conversation. The guard only wants to stop the current loop, not disable tools forever. |
| **Replace tool result** | The LLM sees a clear instruction in the tool result field and can choose to change behaviour. The tool list remains intact for legitimate future use. |

---

## 4. Why Not a Fabricated Tool Pair

Earlier versions (v1.x, v2.0.0) used a dummy tool `_guard_status` with
fabricated assistant+tool pairs injected into the message history. This was
removed because:

| Issue | Detail |
|-------|--------|
| **Stripped before forwarding** | The pair was removed by `clean_messages` to avoid `reasoning_content` validation errors on DeepSeek thinking mode — the LLM never saw it. |
| **Did not survive iterations** | `body["messages"]` is a new list each iteration (rebuilt from `form_data`), so the pair was lost between pipe calls. |
| **Tool definition misled the agent** | `_guard_status` was in `body["tools"]` but not in `metadata["tools"]` — if the agent called it, it received `"Tool _guard_status not found"`, wasting a turn. |
| **State is self-contained** | Every `pipe()` call recalculates state fresh from `_analyse()` — no cross-iteration memory needed. |

The current design replaces the last tool result's `content` in-place, which
**does** survive the middleware's shallow copy (because `messages` is a new
list but each message dict inside is the same object).

---

## 5. Architecture Overview

### 5.1 Manifold: One Pipe, Many Protected Models

The pipe uses Open WebUI's manifold pattern. A single `pipes()` method
queries the gateway for available models and creates one protected sub-pipe
per model. **Nothing is hardcoded.**

```
Pipe: "Agent Loop Guard" (manifold)

pipes() → GET {gateway}/models

Returns:
  🔧 deepseek/deepseek-v4-flash
  🔧 deepseek/deepseek-v4-pro
  🔧 anthropic/claude-haiku-4-5
  ... (whatever the gateway returns)
```

### 5.2 Runtime Flow

```
User selects "🔧 DeepSeek v4 Flash"
     │
     ▼
Open WebUI loads workspace model:
  • Applies system prompt, tools, temperature, etc.
  • Resolves base_model_id → body["model"] = "pipe-uuid.deepseek/deepseek-v4-flash"
     │
     ▼
Open WebUI calls pipe()
     │
     ▼
+------------------------------------------+
|  pipe(body)                              |
|                                          |
|  1. Strip pipe prefix from body["model"] |
|     → "deepseek/deepseek-v4-flash"       |
|  2. Analyse messages via _analyse()      |
|  3. If loop or runaway: replace last     |
|     tool result with guard message       |
|  4. Emit UI notification + status pill   |
|  5. Apply tool blocklist                 |
|  6. Forward to gateway with real model   |
+------------------------------------------+
     │
     ▼
Gateway routes to model provider
     │
     ▼
Response streams back through pipe → Open WebUI → user
```

### 5.3 Model Discovery with Cache

`pipes()` queries the gateway. If the gateway is unreachable, it falls back
to the last successful cache so protected models don't disappear from the
selector.

```python
def __init__(self):
    self.valves = self.Valves()
    self._admin_valves = self.Valves()
    self._models_cache: list[dict] = []

async def pipes(self):
    if not self.valves.GATEWAY_BASE_URL:
        return [{"id": "config", "name": "⚠️ Configure gateway URL"}]

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return self._models_cache or [{"id": "error", "name": "⚠️ Gateway unreachable"}]

    self._models_cache = [
        {"id": m["id"], "name": f"🔧 {m.get('name', m['id'])}"}
        for m in data.get("data", [])
    ]
    return self._models_cache
```

---

## 6. Middleware Integration

The pipe relies on one key property of Open WebUI's `streaming_chat_response_handler`
in `backend/open_webui/utils/middleware.py`:

### 6.1 `pipe()` is called on every tool-call iteration

The middleware's tool-call loop:

```python
while tool_calls and (iterations < CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS):
    # ... execute tools, collect results ...
    new_form_data = {**form_data, ...}                              # shallow copy
    new_form_data['messages'] = [*form_data['messages'], *tool_messages]  # new list
    res = await generate_chat_completion(request, new_form_data, ...)  # → pipe()
```

The model ID retains its pipe prefix, so every iteration routes through `pipe()`.

### 6.2 `body["messages"]` is ephemeral for appending, but message dicts are shared

`body["messages"]` is a **new list** on every iteration. However, each message
dict inside is the **same object** as the previous iteration. This means
**in-place mutation of a message's `content` field** (what the guard does)
**does** survive to the next iteration — the same dict is referenced by the
new list.

---

## 7. Tool-Call Analysis

### 7.1 `_analyse()` — Single-pass analysis

```python
def _analyse(self, body: dict) -> tuple[bool, str | None, str, int, int]:
```

Returns `(should_block, tool_to_blame, block_kind, total, max_calls)`.

**Algorithm:**

1. **Determine limits** — resolve user vs admin valve values using
   `_resolve_limit()` (user value wins if > 0, otherwise admin default).
2. **Identify guarded results** — scan messages backwards from the end,
   stopping at the last user message. Collect `tool_call_id` values of any
   tool result whose `content` contains `GUARD_MARKER`. These calls were
   already handled by the guard and should be excluded from the consecutive
   count.
3. **Collect real tool calls** — scan backwards again, collecting every
   assistant `tool_call` whose `id` is NOT in the guarded set. Parse
   `function.arguments` as JSON. Reverse the list so it's in chronological
   order.
4. **Count consecutive identical calls** — from the end of the history,
   count how many consecutive calls share the same `name` **AND** `args`
   (both must match). If at least 2, record the `bad_tool` name.
5. **Decide**:
   - **Loop**: if `consecutive >= MAX_CONSECUTIVE_TOOL_CALLS > 0` and
     `bad_tool` is set → block with `kind="loop"`.
   - **Runaway**: if `total >= MAX_TOOL_CALLS_PER_TURN > 0` (and no loop
     was detected) → block with `kind="runaway"`.
   - Otherwise → no block (`should_block=False`).

### Why both name AND args must match?

Two calls to the same tool with **different arguments** are considered
different actions, not a loop. Only identical name + identical args
indicates the agent is stuck repeating itself.

---

## 8. Guard States

The guard has exactly **two** blocking states:

| State | Condition | What the LLM sees |
|:-----:|:---------:|-------------------|
| **Loop** | Consecutive identical calls >= `MAX_CONSECUTIVE_TOOL_CALLS` | `"[Tool call budget exhausted] - loop detected\n{tool}: {total} identical calls exceed the limit.\nStop repeating. Try a different tool or summarise what you have."` |
| **Runaway** | Total tool calls in turn >= `MAX_TOOL_CALLS_PER_TURN` (no loop detected) | `"[Tool call budget exhausted] - turn limit reached\nYou've used all {max_calls} allowed calls this turn (attempted {total}).\nNo more tools available. Summarise what you have."` |

**Priority:** Loop wins over runaway. If both conditions are met, the
guard fires as a loop block (more specific — names the offending tool).

There is **no escalation ladder** (no intermediate WARNING or FINAL WARNING
states). The guard fires directly at the configured threshold.

---

## 9. Guard Mechanism

When `_analyse()` returns `should_block=True`:

1. **Replace tool result** — iterate `messages` in reverse, find the last
   message with `role: "tool"`, and set its `content` to the guard message
   text. This mutates the message dict **in-place**, so the change survives
   the middleware's new message list on the next iteration.
2. **Emit UI notification** — via `__event_emitter__`:
   - Loop: `{"type": "notification", "data": {"type": "error", "content": MSG_NOTIFY_LOOP}}`
   - Runaway: `{"type": "notification", "data": {"type": "error", "content": MSG_NOTIFY_RUNAWAY}}`
3. **Emit status pill** — always shows remaining tool calls when `total > 0`
   and `max_calls > 0`:
   `{"type": "status", "data": {"description": "🔧 Remaining tool calls: {remaining}/{max_calls}", "done": True, "hidden": False}}`
4. **Forward to gateway** — the modified `messages` list (with the guard
   text as the last tool result) is packed into the payload and sent to
   `{GATEWAY_BASE_URL}/chat/completions`. The tool blocklist is also applied
   before forwarding.

### What the LLM receives

```
User: "Search for cats"
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "[Tool call budget exhausted] - loop detected
search: 4 identical calls exceed the limit.
Stop repeating. Try a different tool or summarise what you have."
```

The LLM sees the guard message as the latest tool result. If it calls
`search("cats")` again on the next iteration, `_analyse()` will skip the
guarded call (tracked via `guarded_ids`) but count the new one. The guard
can fire again if the LLM persists.

---

## 10. Tool Blocklist (TOOL_BLOCKLIST)

The `TOOL_BLOCKLIST` valve lets administrators permanently remove tools by
name before forwarding. It runs after the guard analysis but before the
gateway call.

- Accepts comma-separated and/or newline-separated tool names.
- Matching is **exact** (`==`) — `fetch_url` does not match `smart_fetch_url`.
- Unknown names are logged as warnings but don't break execution.
- If `tool_choice` targets a blocked tool, it is reset so the LLM can choose freely.

```python
body["tools"][:] = [
    t for t in tools
    if t.get("function", {}).get("name") not in blocked
]
```

---

## 11. Guard Message Templates

```python
GUARD_MARKER = "[Tool call budget exhausted]"

MSG_TOOL_LOOP = (
    "{marker} - loop detected\n"
    "{tool}: {total} identical calls exceed the limit.\n"
    "Stop repeating. Try a different tool or summarise what you have."
)

MSG_TOOL_RUNAWAY = (
    "{marker} - turn limit reached\n"
    "You've used all {max_calls} allowed calls this turn (attempted {total}).\n"
    "No more tools available. Summarise what you have."
)

MSG_NOTIFY_LOOP = "\U0001f527 {tool} budget exhausted after too many identical calls."
MSG_NOTIFY_RUNAWAY = "\U0001f527 Tool call budget exhausted ({total}/{max_calls})."
MSG_COUNTER = "\U0001f527 Remaining tool calls: {remaining}/{max_calls}"
```

The `_build_guard_message(status, tool, total, max_calls)` function selects
the appropriate template and formats it via `str.format()`.

---

## 12. UI Events

| Event | Trigger | Type | Content |
|:-----:|:-------:|:----:|---------|
| Notification | Loop detected | `error` | `"🔧 {tool} budget exhausted after too many identical calls."` |
| Notification | Runaway detected | `error` | `"🔧 Tool call budget exhausted ({total}/{max_calls})."` |
| Status pill | Always (if total > 0 and max_calls > 0) | `status` | `"🔧 Remaining tool calls: {remaining}/{max_calls}"` |

The status pill fires on **every iteration** where there are tool calls,
regardless of whether the guard blocked anything. It shows a descending
counter so the user knows how many tool calls remain.

---

## 13. Valves

> **Admin valves** are configured in the Function admin panel.
> **User valves** (`UserValves`) can be overridden per workspace model.
> A user valve value of `0` means "use admin default".

### Admin valves (Pipe.Valves)

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | Credential value (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers with template variable support |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before runaway guard fires. `0` = disabled |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `4` | Consecutive identical calls before loop guard fires (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove |

**Validation:** `MAX_TOOL_CALLS_PER_TURN` must be **greater than**
`MAX_CONSECUTIVE_TOOL_CALLS` when both are enabled. Enforced by Pydantic's
`@model_validator`. If runaway is ≤ loop, the configuration is rejected with
a clear error message.

### User valves (Pipe.UserValves)

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-model override. `0` = use admin default. |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `0` | Per-model override. `0` = use admin default. |

If the user's effective limits violate the `runaway > loop` constraint at
runtime, a warning is logged and the pipe continues (runaway may fire before
loop detection).

---

## 14. Custom Headers with Templates

The `GATEWAY_CUSTOM_HEADERS` valve accepts a JSON object of extra HTTP
headers sent with every gateway request. Supports template variables that
are resolved at runtime:

```json
{
  "x-bf-dim-host": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}",
  "x-user-email": "{{USER_EMAIL}}",
  "x-chat-id": "{{CHAT_ID}}"
}
```

Supported variables: `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`,
`{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}`.

Unlike Open WebUI's global `ENABLE_FORWARD_USER_INFO_HEADERS` (which only
works for native OpenAI/Ollama routing), this works inside the pipe for any
gateway destination.

---

## 15. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation (no guard) | 0 | Pipe forwards body transparently. No modification. |
| Guard fires (loop or runaway) | ~80-100 | Guard message (~80-100 chars) replaces a potentially much longer tool result. LLM then produces a summary response. |

---

## 16. Edge Cases

| Case | Handling |
|------|----------|
| No tool calls in current turn | Forward unchanged. `_analyse()` finds empty history, no block. |
| Gateway unreachable during `pipes()` | Returns cached model list. Selector still works. |
| Gateway unreachable during `pipe()` | Exception caught → descriptive error string returned. |
| Both loop AND runaway simultaneously | Loop wins — `kind="loop"`, agent sees a tool-specific message rather than a generic limit message. |
| LLM ignores guard and repeats same call | Guard fires again on the next iteration. The guarded call's ID is tracked, so the consecutive count resets for the new batch. The LLM accumulates guard messages. |
| LLM switches to a different tool after guard | Different tool → different name → no consecutive match → no loop. Runaway may still fire if total calls ≥ limit. |
| `MAX_CONSECUTIVE_TOOL_CALLS = 3` | Loop fires at 3 consecutive identical calls. |
| `MAX_TOOL_CALLS_PER_TURN ≤ MAX_CONSECUTIVE_TOOL_CALLS` | Error at config time — Pydantic `@model_validator` rejects the configuration. |
| `TOOL_BLOCKLIST` contains unknown names | Logged as warnings; only matching tools are blocked. |
| `tool_choice` targets a blocked tool | Reset via `body.pop("tool_choice", None)` so the LLM can choose freely. |
| Workspace model has no system prompt | Open WebUI skips system prompt injection. Pipe unaffected. |
| No `__event_emitter__` provided | Guard still fires (tool result replaced). Notifications and status pill are skipped silently. |
| Tool result is not a string (e.g. list/dict) | Guard checks `isinstance(content, str)` before matching `GUARD_MARKER`. Non-string contents are not guarded, but the replacement sets `content` to a string. |

---

## 17. Risk Analysis

### Mitigated risks

| # | Risk | Mitigation |
|:-:|------|------------|
| R1 | Guard message ignored by LLM | Guard tracks `tool_call_id` of guarded results. If LLM repeats, guard fires again. The tracked call is excluded from the consecutive count, so the guard can fire repeatedly on fresh identical calls. |
| R2 | Guard fires on legitimate repeated calls | Guard cannot distinguish intent. Two genuinely identical calls will trigger loop detection at the configured threshold. Administrators should set thresholds high enough for legitimate use cases (default 4). |
| R3 | `tool_choice: "none"` causes raw DSML leakage | Pipe forwards transparently; the gateway handles DSML parsing. |
| R4 | DSML buffer corrupts `reasoning_content` | No buffering in `_stream()` — transparent SSE proxy. |
| R5 | Guard text appears as tool result content | By design — the guard message is a legitimate tool result. The LLM is instructed via the message text to stop and summarise. |
