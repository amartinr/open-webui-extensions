---
title: Agent Loop Guard
author: open-webui-tools
author_url: https://github.com/your-org/open-webui-tools
version: 1.0.0
required_open_webui_version: 0.5.0
requirements: httpx, pydantic
---

# Pipe Function: Agent Loop Guard

## 1. Purpose

Prevent AI agents in Open WebUI from entering infinite tool-calling loops by
analysing conversation history **in real time** — on every request, including
tool-call continuations — and escalating through warning injection up to
force-termination when the agent ignores the warnings.

---

## 2. Why a Pipe Instead of a Filter

Open WebUI offers two extensibility mechanisms that could intercept the LLM
request path:

| | Filter Function (`inlet`) | Pipe Function (`pipe`) |
|---|---|---|
| Called on initial user request | ✅ Yes | ✅ Yes |
| Called on tool-call continuations | ✅ **Yes** — since [commit 5064506](https://github.com/open-webui/open-webui/commit/5064506de4eb6c0aae560c82b79fcf8f1a56c123) (fixes [#18222](https://github.com/open-webui/open-webui/issues/18222)) | ✅ **Yes** — every iteration |
| Can detect loops inside a turn | ✅ Yes — real-time | ✅ Yes — real-time |
| Can force-terminate a runaway turn | ❌ No — `inlet()` must return the body | ✅ Yes — returns plain string |

Since [commit 5064506](https://github.com/open-webui/open-webui/commit/5064506de4eb6c0aae560c82b79fcf8f1a56c123)
(`"refac/fix: inherit request form data"`), Open WebUI spreads `**form_data`
into the `new_form_data` of every tool-call iteration. This means **both**
the Pipe `pipe()` and the Filter `inlet()` are invoked on every iteration
of the tool-calling loop — the fix carries the original model ID (with
pipe prefix) and all filter modifications forward.

A **Pipe** is still the right choice for this project because it alone can
**force-terminate**: return a plain string to skip the LLM call entirely,
saving tokens. A Filter must always return the body, so the LLM call
happens regardless.

---

## 3. Architecture Overview

### 3.1 Manifold: One Pipe, Many Protected Models

The pipe uses Open WebUI's manifold pattern. A single `pipes()` method
queries the gateway (Bifrost) for available models and creates one protected
sub-pipe per model. **Nothing is hardcoded.**

```
┌──────────────────────────────────────────────────┐
│ Pipe: "Agent Loop Guard" (manifold)               │
│                                                    │
│ pipes() → GET {gateway}/models → Bifrost           │
│                                                    │
│ Returns:                                           │
│   🛡️ deepseek/deepseek-v4-flash                    │
│   🛡️ deepseek/deepseek-v4-pro                      │
│   🛡️ anthropic/claude-haiku-4-5                    │
│   ... (whatever Bifrost returns)                   │
└──────────────────────────────────────────────────┘
```

### 3.2 How the Admin Deploys It

**Step 1 — Upload the pipe once.** Configure two valves:
- `GATEWAY_BASE_URL` → Bifrost endpoint
- `GATEWAY_AUTH_VALUE` → gateway credential

**Step 2 — The selector populates automatically.** Open WebUI calls
`pipes()` during model discovery, which queries Bifrost's `/models` endpoint
and creates one sub-pipe per model.

**Step 3 — Create protected workspace models.** In Admin Panel → Models,
create workspace entries pointing at the sub-pipes. Each keeps its own
system prompt, tools, capabilities, and temperature:

```
┌────────────────────────────────────────────────────────────┐
│ "DeepSeek v4 Assistant (Protegido)"                         │
│   base_model_id = "pipe-uuid.deepseek/deepseek-v4-flash"   │
│   system prompt  = "You are a helpful assistant..."         │
│   tools          = [smart_fetch_url]                        │
│   temperature    = 0.7                                       │
├────────────────────────────────────────────────────────────┤
│ "DeepSeek v4 SW Engineer (Protegido)"                       │
│   base_model_id = "pipe-uuid.deepseek/deepseek-v4-pro"     │
│   system prompt  = "You are an expert coding assistant..."  │
│   terminal       = ephedrine                                 │
├────────────────────────────────────────────────────────────┤
│ ... etc. for every combination you need                     │
└────────────────────────────────────────────────────────────┘
```

### 3.3 Runtime Flow

```
User selects "DeepSeek v4 Assistant (Protegido)"
     │
     ▼
Open WebUI loads workspace model:
  • Applies "You are a helpful assistant..." to body["messages"]
  • Resolves base_model_id → body["model"] = "pipe-uuid.deepseek/deepseek-v4-flash"
     │
     ▼
Open WebUI detects model["pipe"] → calls pipe()
     │
     ▼
+------------------------------------------+
|  pipe(body)                              |
|                                          |
|  1. Strip pipe prefix from body["model"] |
|     → "deepseek/deepseek-v4-flash"       |
|  2. Analyse messages for loops           |
|  3. Inject warnings or soft-block        |
|  4. Forward to Bifrost with real model   |
+------------------------------------------+
     │
     ▼
Bifrost routes to deepseek-v4-flash
     │
     ▼
Response streams back through pipe → Open WebUI → user
```

Open WebUI applies the system prompt and resolves `base_model_id` **before**
the pipe runs. The pipe receives a body with the system prompt already
injected and the model ID already set, then does three things:

1. **Strips the pipe prefix** from `body["model"]` to get the real model ID
2. **Analyses messages** and injects warnings / soft-blocks if needed
3. **Forwards** to the gateway with the real model ID

Tus workspace models conservan **todo**: system prompts, tools,
parámetros de temperatura, capabilities. El pipe no sabe nada de eso — solo
protege contra bucles.

---

## 4. Open WebUI Pipe Contract

### 4.1 Manifold: pipes()

A `pipes()` method (sync, async, or a plain list) tells Open WebUI which
sub-pipes the manifold exposes. Each entry is `{"id": "...", "name": "..."}`.

```python
async def pipes(self):
    """Query gateway for available models. Returns one sub-pipe per model."""
    ...
    return [
        {"id": "deepseek/deepseek-v4-flash", "name": "🛡️ DeepSeek v4 Flash"},
        {"id": "deepseek/deepseek-v4-pro",   "name": "🛡️ DeepSeek v4 Pro"},
    ]
```

Open WebUI registers each as `{pipe_id}.{sub_pipe_id}` and they appear in
the model selector.

### 4.2 pipe() — Request Handler

The `pipe()` method receives every chat completion request. `body["model"]`
contains the full sub-pipe ID (e.g. `"pipe-uuid.deepseek/deepseek-v4-flash"`).

```python
async def pipe(self, body: dict, __user__: Optional[dict] = None, __metadata__: Optional[dict] = None):
    # Strip prefix to get the real model ID
    model = body["model"].split(".", 1)[-1]
    ...
```

Returns an async generator (streaming proxy), a dict (non-streaming proxy),
or the result of `_soft_block()` (forwards to gateway with tools removed).
Unlike a traditional force-termination (which returns a plain string to skip
the LLM call), soft-block sends an instruction to summarise while keeping
tools unavailable.

---

## 5. Valves

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| `GATEWAY_BASE_URL` | str | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | str | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | str (password) | `""` | Credential value sent in the configured auth header |
| `GATEWAY_CUSTOM_HEADERS` | str (JSON) | `""` | JSON object of extra HTTP headers. Supports template variables: `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` (e.g. `{"x-bf-dim-host": "myhost", "x-user": "{{USER_NAME}}"}`) |
| `MAX_TOOL_CALLS_PER_TURN` | int | 15 | Max tool calls in a turn before tools are removed (soft-block). Set to 0 to disable. |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | int | 4 | Consecutive identical tool calls before soft-block (min 3). Warnings are spaced automatically: WARNING on first detection (consecutive=2), FINAL WARNING at ~60% of threshold, soft-block at threshold |

> **Validation**: `MAX_TOOL_CALLS_PER_TURN` must be greater than `MAX_CONSECUTIVE_BEFORE_BLOCK`
> when both are enabled. If runaway's threshold is equal or lower, Pydantic rejects the
> configuration with a descriptive error.

| `INJECTION_POSITION` | Literal["append_user","merge_last_tool"] | `"append_user"` | Where to inject guard messages: `"append_user"` (before last user message) or `"merge_last_tool"` (append to last tool result) |
| `SHOW_TOOL_COUNTER` | bool | `True` | Append descending counter (`remaining tool calls: N`) to every tool result |
| `TOOL_BLOCKLIST` | str | `""` | Comma/newline-separated tool names to **remove** from the agent's tool list. Example: `"delete_file, terminal_execute"` |

---

## 6. Model Discovery with Cache

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

    headers = self._build_gateway_headers()
    url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/models"

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

If Bifrost adds a model (e.g. a new provider), it appears automatically on
the next model refresh. If Bifrost is down, the cached list keeps working.

---

## 7. Analysis of Conversation History

### 7.1 Extracting Tool Calls Per Turn

Scan backwards from the end of `body["messages"]` until the last user
message. Every assistant message with `tool_calls` in that range is part of
the current turn.

```python
def _extract_tool_calls_in_turn(self, messages: list[dict]) -> list[dict]:
    history = []
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
    """Count consecutive identical tool calls from the end of history.

    Returns (count, name, args) of the repeated tool.
    count=1 means the last call is unique (no consecutive duplicate).
    """
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

### 7.3 Formula-Based Escalation

Instead of tracking warnings via instance variables, escalation is determined
by a simple formula that guarantees each level fires exactly once:

```
final_pos = 2 + (N - 2) * 3 // 5    # ≈60% of the range [2, N)

consecutive == 2       → WARNING
consecutive == final_pos → FINAL WARNING (if N > 3)
consecutive >= N        → soft-block
otherwise               → silent
```

For `N=3` there is no room for FINAL WARNING: WARNING at consecutive=2,
soft-block at consecutive=3.

---

## 8. Injection & Escalation Strategy

### 8.1 Escalation Ladder

```
consecutive == 2                    → WARNING (tools still available)
  "{tool_name} called {total}x with same args. Change approach or summarize."
  │  Agent ignores → continues looping

consecutive == final_pos (≈60% of N) → FINAL WARNING (if N > 3, tools still available)
  "{tool_name} called {total}x. Still repeating. Stop now and summarize."
  │  Agent still ignores

consecutive >= N                     → SOFT-BLOCK: only the looping tool removed
  Agent receives instruction + all collected results, can use other tools
  "TOOL REMOVED: {tool_name} blocked after {total} identical calls."
  → Agent has other tools available or can summarise.
```

Where `final_pos = 2 + (N - 2) * 3 // 5`. For `N=3` there is no FINAL WARNING.

### 8.2 Injection Messages

**Loop Warning (level 1):**
```python
def _warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x with same args. Change approach or summarize."
```

**Final Warning (level 2):**
```python
def _final_warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x. Still repeating. Stop now and summarize."
```

**Runaway Prevention (skip escalation, immediate soft-block):**
```python
def _runaway_instruction(total: int, max_calls: int) -> str:
    return (
        f"TOOL LIMIT: {total}/{max_calls} used. "
        f"No more tools this turn. Summarize now."
    )
```

**Loop soft-block (only the offending tool removed):**
```python
def _loop_blocked_tool_instruction(tool_name: str, total: int) -> str:
    return (
        f"TOOL REMOVED: {tool_name} blocked after {total} identical calls. "
        f"Other tools still available. Summarize or continue."
    )
```

### 8.3 Injection Position

| Value | Behaviour |
|-------|-----------|
| `"append_user"` (default) | Insert a new `system` message before the last user message |
| `"merge_last_tool"` | Append the guard message content to the last tool result in the current turn, after a clear separator |

### 8.4 Formula-Based Injection (No Stale State)

Because escalation is purely formula-based (`consecutive` count determines
the action), there is no instance state to reset. Each `pipe()` invocation
is stateless with respect to escalation. The `_merge_injected` flag is the
only per-turn state, reset when `total == 0`.

---

## 9. Soft-block (replaces force-termination)

When the guard decides to stop an agent, it does **not** skip the LLM call.
Instead, it removes `tools` from the request body and injects a system
message instructing the agent to summarise. The LLM receives all tool
results already in the chat and is forced to respond with text — it
cannot call more tools because none are available.

This preserves all collected results and produces a useful final answer
instead of a hardcoded guard message impersonating the agent.

---

## 10. Architecture Summary

The pipe is structured as a pipeline of independent phases, each one
additive on the previous:

### Phase 1 — Manifold Proxy
- `pipes()` queries the gateway's `/models` endpoint
- Caches results; falls back to cache on gateway failure
- `pipe()` strips the pipe prefix from the model ID, forwards to gateway

### Phase 2 — Runaway Protection
- `_extract_tool_calls_in_turn()` scans messages backwards from the end
  until the last user message, collecting all tool_calls in the current turn
- If the count reaches `MAX_TOOL_CALLS_PER_TURN`, the pipe **soft-blocks**
  (removes tools, injects instruction, forwards)

### Phase 3 — Loop Detection & Escalation
- `_count_consecutive_duplicates()` counts consecutive identical calls from end of history
- Formula-based escalation (each level fires exactly once):
  - `consecutive == 2` → inject WARNING (tools still available)
  - `consecutive == final_pos` (≈60% of N, if N > 3) → inject FINAL WARNING (tools still available)
  - `consecutive >= N` → soft-block (only the looping tool removed)

### Phase 6 — Tool Blocklist
- `TOOL_BLOCKLIST` valve: comma/newline-separated list of tool names to **remove**
- `_parse_tool_list()` splits on commas, newlines, or mixed input
  using `re.split(r"[,\n\r]+", raw)`
- `_apply_tool_blocklist()` mutates `body['tools']` before forwarding;
  logs a warning for names that don't match any available tool;
  resets `tool_choice` if it targets a removed tool
- Matching is exact (`==`); `fetch_url` does not match `smart_fetch_url`
- Runs on every normal forward before reaching the gateway;
  soft-block modifications happen after the filter

### Gateway proxy
- `_stream()` forwards SSE lines from the gateway back to Open WebUI
- `_call()` handles non-streaming requests
- `_build_gateway_headers()` assembles auth, custom headers, and resolves
  template variables (`{{USER_NAME}}`, `{{USER_ID}}`, etc.)

---

## 11. Flow Diagrams

### Normal Turn (No Loop)

```
User: "Search for AI trends" via "DeepSeek v4 Assistant (Protegido)"
     │
     ▼
Open WebUI: system prompt applied → body["model"] = "pipe-id.deepseek/deepseek-v4-flash"
     │
     ▼
pipe()
  ├─ real_model = "deepseek/deepseek-v4-flash"
  ├─ history = [] → no loop
  ├─ no injections
  └─ Forward to Bifrost → POST /v1/chat/completions
     │
     ▼
LLM: tool_calls [search_web("AI trends")]
OWUI executes tool, sends continuation

pipe()  ← continuation
  ├─ history = [search_web] → no loop
  └─ Forward to Bifrost

LLM: "Here are the AI trends..." ✅
```

### Loop Detected — Escalation Within One Turn

```
pipe() → forward → LLM: tool_calls [get_weather("Paris")]
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris")]
  └─ no loop → forward

LLM: tool_calls [get_weather("Paris")]  ← same call
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris") × 2]
  ├─ duplicates detected (threshold=2)
  ├─ consecutive=2 → inject WARNING
  └─ Forward to Bifrost (with warning)

LLM: tool_calls [get_weather("Paris")]  ← ignores warning
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris") × 3]
  ├─ consecutive=3, final_pos=3 → inject FINAL WARNING
  └─ Forward to Bifrost (with final warning)

LLM: tool_calls [get_weather("Paris")]  ← still ignores
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris") × 4]
  ├─ consecutive=4 ≥ N(=4)
  └─ _soft_block(bad_tool="get_weather")

🛑 Soft-blocked: only `get_weather` removed from body["tools"], instruction
    injected, forwarded to gateway. LLM receives all results but cannot call
    `get_weather`. Other tools (if any) remain available.
```

---

## 12. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation | 0 | Pipe forwards body as-is |
| Loop warning | ~60-80 | Injected system message |
| Final warning | ~70-90 | Second injection |
| Soft-block (runaway limit) | ~60-80 (system msg) + LLM response | Pipe removes tools, injects instruction, forwards |
| Soft-block (loop) | ~60-80 (system msg) + LLM response | Same — all results preserved, LLM summarises |

---

## 13. Edge Cases

| Case | Handling |
|------|----------|
| No tool calls in current turn | No loop detection. Forward unchanged. |

| Gateway unreachable during `pipes()` | Returns cached model list. Selector still works. |
| Gateway unreachable during `pipe()` | Exception caught → error string. |
| Warning already present from earlier | Formula-based: consecutive count determines action, no repeated injections. |
| Bifrost adds a new model | Appears automatically on next model refresh. |
| `MAX_CONSECUTIVE_BEFORE_BLOCK = 3` | WARNING on consecutive=2, soft-block at consecutive=3 (no FINAL WARNING). |
| `MAX_CONSECUTIVE_BEFORE_BLOCK = 6` | WARNING at 2, FINAL WARNING at 4 (≈60%), soft-block at 6. |
| `MAX_TOOL_CALLS_PER_TURN ≤ MAX_CONSECUTIVE_BEFORE_BLOCK` | Error at config time — runaway would fire before loop detection, making loop invisible. |
| Workspace model has no system prompt | Open WebUI skips system prompt injection. Pipe unaffected. |

---

## 14. Validation Criteria

1. **Dynamic model discovery**: `pipes()` queries the gateway and populates
   the selector without hardcoded model IDs.

2. **Cache with fallback**: If the gateway is unreachable, `pipes()` returns
   the last successful cache so protected models don't vanish.

3. **Transparent proxy**: When no loop is detected, the pipe forwards
   `body` to the gateway and streams the response back unchanged.

4. **Loop detection in real time**: When consecutive identical tool calls are
   detected, a warning is injected before the next LLM call.

5. **Escalation**: Formula-based: WARNING at consecutive=2, FINAL WARNING at
   ≈60% of threshold (if N > 3), soft-block at threshold (only the looping tool
   removed from `body["tools"]`, instruction injected, forwarded to gateway).

6. **Runaway limit**: When tool calls in a turn exceed
   `MAX_TOOL_CALLS_PER_TURN`, the pipe soft-blocks immediately (all tools removed,
   instruction injected, forwarded to gateway).

7. **Soft-block preserves LLM call**: Unlike force-termination, soft-block
   forwards to the gateway with all tool results preserved and an instruction
   to summarise. The LLM call still happens, but no tools are available.

8. **Respects workspace model config**: System prompts, tools, and
   parameters from the workspace model are applied by Open WebUI before
   the pipe runs. The pipe does not touch them.

9. **Works with any model the gateway exposes**: Adding a provider to
   Bifrost adds a protected sub-pipe automatically.

10. **Single set of credentials**: Only `GATEWAY_BASE_URL` and
    `GATEWAY_AUTH_VALUE` are needed — all models share the same gateway.

11. **Tool blocklist**: Setting `TOOL_BLOCKLIST` to `"delete_file, terminal_execute"`
    removes only those tools. Everything else remains visible.

12. **Flexible input format**: The valve accepts commas, newlines, or
    mixed input. `"delete_file, terminal_execute"` and
    `"delete_file\nterminal_execute"` produce the same set.

13. **Unknown name detection**: If a name in `TOOL_BLOCKLIST` does not
    match any available tool, a warning is logged and the name is ignored.
    The pipe does not break.

14. **Exact match**: `fetch_url` blocks only `fetch_url`, not
    `smart_fetch_url`. Matching is done with `==` via set membership.

15. **tool_choice cleanup**: If `tool_choice` targets a blocked tool,
    it is reset so the LLM can choose freely.


