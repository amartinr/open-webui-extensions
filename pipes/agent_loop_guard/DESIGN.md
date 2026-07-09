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
|  3. Inject warnings or force-terminate   |
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
2. **Analyses messages** and injects warnings / force-terminates if needed
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

Returns a plain string (force-termination), an async generator (streaming
proxy), or a dict (non-streaming proxy).

---

## 5. Valves

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| `GATEWAY_BASE_URL` | str | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | str | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | str (password) | `""` | Credential value sent in the configured auth header |
| `GATEWAY_CUSTOM_HEADERS` | str (JSON) | `""` | JSON object of extra HTTP headers. Supports template variables: `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` (e.g. `{"x-bf-dim-host": "myhost", "x-user": "{{USER_NAME}}"}`) |
| `MAX_TOOL_CALLS_PER_TURN` | int | 15 | Max tool calls in a turn before tools are removed (soft-block). Set to 0 to disable. |
| `MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING` | int | 2 | Consecutive identical tool calls before first warning. Set to 0 to disable. |
| `MAX_WARNINGS_BEFORE_TERMINATE` | int | 2 | Warnings before tools are removed (soft-block). Set to 0 to soft-block immediately on first loop detection. |
| `ENABLE_PREVENTIVE_REMINDER` | bool | True | Inject periodic self-evaluation reminders |
| `REMINDER_INTERVAL` | int | 3 | Inject preventive reminder every N user messages |
| `INJECTION_POSITION` | Literal["prepend","append_system","append_user"] | `"prepend"` | Where to inject the warning message |

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

### 7.2 Detecting Consecutive Duplicates

```python
def _has_consecutive_duplicates(self, history: list[dict], threshold: int) -> bool:
    if len(history) < threshold:
        return False
    recent = history[-threshold:]
    first = recent[0]
    return all(
        c["name"] == first["name"] and c["args"] == first["args"]
        for c in recent
    )
```

### 7.3 Escalation Level from History

Deduced from already-injected messages — no external state.

```python
def _escalation_level(self, messages: list[dict]) -> int:
    """0 = clean, 1 = warning, 2 = final warning."""
    for msg in reversed(messages):
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if "FINAL WARNING:" in content:
            return 2
        if "WARNING:" in content:
            return 1
    return 0
```

---

## 8. Injection & Escalation Strategy

### 8.1 Escalation Ladder

```
Level 0: Silent monitoring
  │  Consecutive duplicate tool calls detected
  ▼
Level 1: WARNING
  "WARNING: You called the same tool with the same arguments
   multiple times. Stop and change strategy, or summarize."
  │  Agent ignores → continues looping
  ▼
Level 2: FINAL WARNING
  "FINAL WARNING: You are still repeating tool calls. You MUST
   stop and provide a summary NOW."
  │  Agent still ignores
  ▼
FORCE TERMINATE → plain string, no LLM call
```

### 8.2 Injection Messages

**Loop Warning (level 1):**
```python
{"role": "system", "content": "WARNING: In this turn you called the same "
    "tool with the same arguments multiple times without achieving a "
    "different result. Stop and change strategy, or provide a summary."}
```

**Final Warning (level 2):**
```python
{"role": "system", "content": "FINAL WARNING: You are still repeating "
    "tool calls despite receiving a warning. You MUST stop calling tools "
    "immediately. Provide a summary of everything you have gathered."}
```

**Runaway Prevention (skip escalation, immediate force-terminate):**
```python
{"role": "system", "content": f"LIMIT EXCEEDED: You have made {N} tool "
    f"calls in this turn (max {MAX}). You must stop now and provide a "
    f"final answer."}
```

**Preventive Reminder:**
```python
{"role": "system", "content": "REMINDER: Periodically evaluate whether "
    "your tool calls are making progress. If you detect repetition, "
    "stop and provide a summary."}
```

### 8.3 Injection Position

| Value | Behaviour |
|-------|-----------|
| `"prepend"` | Insert at position 0 (before all messages) |
| `"append_system"` | Insert after the last existing system message |
| `"append_user"` | Insert before the last user message |

### 8.4 Avoiding Stale Injections

The pipe scans the last system messages. If a warning level is already
present, it escalates rather than re-injecting.

```python
def _last_system_contains(self, messages: list[dict], text: str) -> bool:
    return any(
        msg.get("role") == "system"
        and text.lower() in msg.get("content", "").lower()
        for msg in messages
    )
```

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
- `_has_consecutive_duplicates()` detects N identical consecutive calls
- `_escalation_level()` scans existing system messages for guard markers
  (`[GUARD_WARN]`, `[GUARD_FINAL]`) to determine current escalation level
- Escalation ladder:
  - Level 0 → inject WARNING (tools still available)
  - Level 1 → inject FINAL WARNING (tools still available)
  - Level 2 → soft-block (tools removed)

### Phase 4 — Preventive Reminders
- `_last_system_contains()` checks for existing guard markers
- Every `REMINDER_INTERVAL` user messages, injects a REMINDER message

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
  ├─ escalation=0 → inject WARNING
  └─ Forward to Bifrost (with warning)

LLM: tool_calls [get_weather("Paris")]  ← ignores warning
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris") × 3]
  ├─ escalation=1 → inject FINAL WARNING
  └─ Forward to Bifrost (with final warning)

LLM: tool_calls [get_weather("Paris")]  ← still ignores
OWUI executes tool

pipe()  ← continuation
  ├─ history = [get_weather("Paris") × 4]
  ├─ escalation=2 ≥ MAX_WARNINGS(2)
  └─ Return plain string — NO LLM call

🛑 Soft-blocked. LLM forced to summarise with results already gathered.
```

---

## 12. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation | 0 | Pipe forwards body as-is |
| Loop warning | ~60-80 | Injected system message |
| Final warning | ~70-90 | Second injection |
| Preventive reminder | ~50-70 | Every N user messages |
| Soft-block (runaway limit) | ~60-80 (system msg) + LLM response | Pipe removes tools, injects instruction, forwards |
| Soft-block (loop) | ~60-80 (system msg) + LLM response | Same — all results preserved, LLM summarises |

---

## 13. Edge Cases

| Case | Handling |
|------|----------|
| No tool calls in current turn | No loop detection. Forward unchanged. |
| First user message of conversation | Only preventive reminder check. |
| Gateway unreachable during `pipes()` | Returns cached model list. Selector still works. |
| Gateway unreachable during `pipe()` | Exception caught → error string. |
| Warning already present from earlier | Escalate, don't re-inject. |
| Bifrost adds a new model | Appears automatically on next model refresh. |
| `MAX_WARNINGS_BEFORE_TERMINATE = 0` | Force-terminate on first loop detection. |
| `MAX_WARNINGS_BEFORE_TERMINATE = 1` | Warn once, then terminate on next duplicate. |
| Workspace model has no system prompt | Open WebUI skips system prompt injection. Pipe unaffected. |

---

## 14. Validation Criteria

1. **Dynamic model discovery**: `pipes()` queries the gateway and populates
   the selector without hardcoded model IDs.

2. **Cache with fallback**: If the gateway is unreachable, `pipes()` returns
   the last successful cache so protected models don't vanish.

3. **Transparent proxy**: When no loop is detected, the pipe forwards
   `body` to the gateway and streams the response back unchanged.

4. **Loop detection in real time**: After N consecutive identical tool calls
   within the same turn, a warning is injected before the next LLM call.

5. **Escalation**: If the agent ignores the warning, the pipe escalates to
   final warning → force-termination.

6. **Runaway limit**: When tool calls in a turn exceed
   `MAX_TOOL_CALLS_PER_TURN`, the pipe force-terminates immediately.

7. **Force-termination skips LLM**: No HTTP call to the gateway is made.

8. **Respects workspace model config**: System prompts, tools, and
   parameters from the workspace model are applied by Open WebUI before
   the pipe runs. The pipe does not touch them.

9. **Works with any model the gateway exposes**: Adding a provider to
   Bifrost adds a protected sub-pipe automatically.

10. **Single set of credentials**: Only `GATEWAY_BASE_URL` and
    `GATEWAY_AUTH_VALUE` are needed — all models share the same gateway.


