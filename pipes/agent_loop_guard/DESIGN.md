# Design Document: Agent Loop Guard

**Version:** 3.0  
**Based on:** `agent_loop_guard.py` (current implementation)

---

## 1. Purpose

Prevent AI agents in Open WebUI from entering infinite tool-calling loops by
analysing conversation history **on every iteration** of the middleware's
tool-call loop, escalating through system-message warnings up to **soft-block**
(removing tools from the request body and metadata).

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
in-place slice assignment that survives the middleware's shallow copy) and
**block tool execution at the metadata level** (via `__metadata__["tools"]`
which is the same dict the middleware uses).

---

## 3. Core Mechanism

The pipe sits between Open WebUI and the LLM gateway. On every tool-call
iteration, the middleware calls `pipe()` with the accumulated message history.
The pipe:

1. **Analyses** the current turn's tool calls (consecutive duplicates, total
   count) by scanning backwards from the last user message.
2. **Escalates** through a formula-based ladder: WARNING → FINAL WARNING →
   soft-block (each level fires exactly once).
3. **Acts** via two channels that both survive the middleware's shallow-copy
   loop:
   - **System messages** injected into `body["messages"]` — forwarded to the
     gateway, the LLM receives them.
   - **In-place tool removal** (`tools[:] = [...]`) on `body["tools"]` — the
     shallow copy shares the same list object, so the change persists.
   - **Metadata clearing** (`__metadata__["tools"].pop(name)`) — the same dict
     object as the middleware's `metadata["tools"]`, so removed tools cannot
     be executed.

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
| **State is self-contained** | Every `pipe()` call recalculates state fresh from `_extract_tool_calls_in_turn()` — no cross-iteration memory needed. |

The current design uses only the two channels that **do** work reliably:
system messages and in-place tool/metadata mutation.

---

## 5. Architecture Overview

### 5.1 Manifold: One Pipe, Many Protected Models

The pipe uses Open WebUI's manifold pattern. A single `pipes()` method
queries the gateway for available models and creates one protected sub-pipe
per model. **Nothing is hardcoded.**

```
┌──────────────────────────────────────────────────┐
│ Pipe: "Agent Loop Guard" (manifold)               │
│                                                    │
│ pipes() → GET {gateway}/models                    │
│                                                    │
│ Returns:                                           │
│   🛡️ deepseek/deepseek-v4-flash                    │
│   🛡️ deepseek/deepseek-v4-pro                      │
│   🛡️ anthropic/claude-haiku-4-5                    │
│   ... (whatever the gateway returns)               │
└──────────────────────────────────────────────────┘
```

### 5.2 Runtime Flow

```
User selects "🛡️ DeepSeek v4 Flash"
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
|  2. Analyse messages for loops           |
|  3. Inject warnings or soft-block        |
|  4. Forward to gateway with real model   |
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
        {"id": m["id"], "name": f"🛡️ {m.get('name', m['id'])}"}
        for m in data.get("data", [])
    ]
    return self._models_cache
```

---

## 6. Middleware Integration

The pipe relies on three properties of Open WebUI's `streaming_chat_response_handler`
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

### 6.4 `body["messages"]` is ephemeral

`body["messages"]` is a **new list** on every iteration. Modifications to it
(append, replace, delete) do **not** survive to the next iteration. This is
why the pipe uses **system messages** only for communicating with the LLM —
they are part of the forwarded payload and reach the gateway, but they do not
accumulate across iterations.

---

## 7. Tool-Call Analysis

### 7.1 Extracting Tool Calls Per Turn

```python
def _extract_tool_calls_in_turn(self, messages: list[dict]) -> list[dict]:
    """Scan backwards from the end until the last user message.
    Collect every assistant tool_call in the current turn."""
    history: list[dict] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user":
            break
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {}
                history.append({"name": tc["function"]["name"], "args": args})
    history.reverse()
    return history
```

### 7.2 Counting Consecutive Duplicates

```python
def _count_consecutive_duplicates(self, history: list[dict]) -> tuple[int, str | None, dict | None]:
    if not history:
        return 0, None, None
    last = history[-1]
    count = 0
    for tc in reversed(history):
        if tc["name"] == last["name"] and tc["args"] == last["args"]:
            count += 1
        else:
            break
    return count, last["name"], last["args"]
```

### 7.3 State-Free Escalation

Because escalation is purely formula-based (`consecutive` count determines
the action), there is no instance state to manage. Each `pipe()` invocation
is stateless with respect to escalation level — it reads the history and
computes the action fresh.

---

## 8. Escalation Ladder

Each level fires **exactly once** per turn, determined by the formula:

```
final_pos = 2 + (N - 2) * 3 // 5    # ≈60% of the range [2, N)
```

| consecutive | Action | What the LLM receives |
|:-----------:|:------:|-----------------------|
| 1 | None (silent) | Nothing |
| 2 | **WARNING** | System message: `"{tool} called 2x with the same arguments. Change your approach or summarise."` |
| `final_pos` (if N > 3) | **FINAL WARNING** | System message: `"{tool} called {n}x... This is your final warning. Stop repeating and summarise."` |
| ≥ N | **SOFT-BLOCK** | Offending tool removed from `body["tools"]` + `__metadata__["tools"]`. System message instructs the agent to summarise. Other tools remain available. |

For `N=3` there is no FINAL WARNING (WARNING → block directly).

**Priority:** Loop detection wins over runaway. Runaway only fires when
`consecutive < block_threshold` — an agent in a loop gets `blocked_tool`
(only the looping tool removed) even if it also hit the total-turn limit.

---

## 9. Soft-block (Loop & Runaway)

When the guard decides to stop the agent:

1. **In-place tool removal** — `tools[:] = [...]` mutates the shared list
   object so the change survives the middleware's shallow copy.
2. **Metadata clearing** — `__metadata__["tools"].pop(name)` prevents the
   middleware from executing the tool even if the LLM emits tool_calls.
3. **System message** — `messages.append({"role": "system", ...})` instructs
   the agent to summarise.
4. **No early return** — execution falls through to the normal forwarding
   path. The LLM receives all collected tool results + system instruction,
   but without tools it **cannot** schedule more tool calls.

### Runaway vs Loop block

| Scenario | What is removed | Agent can still use |
|:---------|:---------------|:--------------------|
| **Loop** (consecutive ≥ threshold) | Only the looping tool | Other tools + summarise |
| **Runaway** (total ≥ max_calls, no loop) | All tools used this turn | Unused tools + summarise |

Both are symmetric: they remove only the problematic tools and leave the
rest available. The agent is forced to summarise only when no useful tools
remain.

---

## 10. Tool Blocklist (TOOL_BLOCKLIST)

The `TOOL_BLOCKLIST` valve lets administrators permanently remove tools by
name before forwarding. It runs after escalation but before the gateway call.

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

## 11. Guard Message Builder

The single function `_build_guard_message(state)` produces all system message
content based on the current state dict:

| status | Example message |
|--------|----------------|
| `ok` | `"14/15 tool calls remaining."` |
| `warning` | `"smart_fetch_url called 2x with the same arguments. 13/15 tool calls remaining. Change your approach or summarise."` |
| `final_warning` | `"smart_fetch_url called 3x... 12/15 tool calls remaining. This is your final warning."` |
| `blocked_tool` | `"TOOL REMOVED: smart_fetch_url blocked after 4 identical calls. 11/15 tool calls remaining. Other tools are still available or you may summarise now."` |
| `runaway` | `"TOOL LIMIT REACHED: 15/15. Tools used in this turn have been removed."` |

---

## 12. UI Events

| State | Notification | Type |
|-------|-------------|:----:|
| WARNING | `"🛡️ {tool} called {n}x with same args."` | `info` |
| FINAL WARNING | `"🛡️ {tool} called {n}x. Final warning."` | `warning` |
| Blocked tool | `"🛡️ {tool} blocked after {n} identical calls."` | `error` |
| Runaway | `"🛡️ Tool call limit reached ({n}/{max})."` | `error` |
| Counter | `"🛡️ Remaining tool calls: {remaining}/{max}"` | `status` |

---

## 13. Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | Credential value (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers with template variable support |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before soft-block. `0` = disabled |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | `4` | Consecutive identical calls before soft-block (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove |

**Validation:** `MAX_TOOL_CALLS_PER_TURN` must be > `MAX_CONSECUTIVE_BEFORE_BLOCK`
when both are enabled, enforced by Pydantic's `@model_validator`.

---

## 14. Custom Headers with Templates

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

## 15. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation (no warnings) | 0 | Pipe forwards body as-is |
| Loop warning | ~60-80 | Injected system message |
| Final warning | ~70-90 | Second injection |
| Soft-block (either type) | ~60-80 (system msg) + LLM response | All results preserved, LLM summarises |

---

## 16. Edge Cases

| Case | Handling |
|------|----------|
| No tool calls in current turn | Forward unchanged. No analysis needed. |
| Gateway unreachable during `pipes()` | Returns cached model list. Selector still works. |
| Gateway unreachable during `pipe()` | Exception caught → descriptive error string returned. |
| Both loop AND total-turn limit simultaneously | Loop wins — `blocked_tool` fires instead of `runaway`, keeping non-looping tools available. |
| `MAX_CONSECUTIVE_BEFORE_BLOCK = 3` | WARNING at consecutive=2, soft-block at consecutive=3 (no FINAL WARNING). |
| `MAX_CONSECUTIVE_BEFORE_BLOCK = 6` | WARNING at 2, FINAL WARNING at 4 (≈60%), soft-block at 6. |
| `MAX_TOOL_CALLS_PER_TURN ≤ MAX_CONSECUTIVE_BEFORE_BLOCK` | Error at config time — Pydantic rejects the configuration. |
| `TOOL_BLOCKLIST` contains unknown names | Logged as warnings; only matching tools are blocked. |
| `tool_choice` targets a blocked tool | Reset so the LLM can choose freely. |
| Workspace model has no system prompt | Open WebUI skips system prompt injection. Pipe unaffected. |

---

## 17. Risk Analysis

### Mitigated risks

| # | Risk | Mitigation |
|:-:|------|------------|
| R1 | Shallow copy discards reassigned `body["tools"]` | In-place slice assignment `tools[:] = [...]` mutates the shared list. |
| R2 | Middleware executes tools from `metadata["tools"]` | Clear `__metadata__["tools"]` alongside `body["tools"]`. |
| R3 | `tool_choice: "none"` causes raw DSML leakage | Leave `tool_choice` unset; Bifrost parses DSML into harmless `tool_calls` against empty metadata. |
| R4 | DSML buffer corrupts `reasoning_content` | No buffering in `_stream()` — transparent SSE proxy. |
