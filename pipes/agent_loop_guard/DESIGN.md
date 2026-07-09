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
| Called on tool-call continuations | ❌ **No** ([issue #18222](https://github.com/open-webui/open-webui/issues/18222)) | ✅ **Yes** — every iteration |
| Can detect loops inside a turn | ❌ No — only between turns | ✅ Yes — real-time |
| Can force-terminate a runaway turn | ❌ No | ✅ Yes |

A **Filter** is not invoked during the tool-calling loop. A **Pipe** is
invoked on **every** request: the initial user message *and* every
continuation with tool results.

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

Only four backend valves. The model list comes from the gateway, not from
configuration.

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| `GATEWAY_BASE_URL` | str | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | str | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | str (password) | `""` | Credential value sent in the configured auth header |
| `GATEWAY_HOST_HEADER` | str | `"x-bf-dim-host"` | HTTP header name for the host routing value |
| `GATEWAY_HOST_VALUE` | str | `""` | Value sent in the host routing header (e.g. Bifrost dimension) |
| `MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING` | int | 2 | Consecutive identical tool calls before first warning |
| `MAX_TOOL_CALLS_PER_TURN` | int | 15 | Max tool calls per turn before force-termination |
| `ENABLE_PREVENTIVE_REMINDER` | bool | True | Periodic self-evaluation reminder every N messages |
| `REMINDER_INTERVAL` | int | 3 | Inject preventive reminder every N user messages |
| `INJECTION_POSITION` | Literal["prepend","append_system","append_user"] | `"prepend"` | Where to inject the warning message |
| `MAX_WARNINGS_BEFORE_TERMINATE` | int | 2 | Escalation steps before force-termination |

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

## 9. Force-Termination

When the pipe force-terminates, it returns a plain string — no LLM call.

```
I've stopped because this turn reached the limit of 15 tool calls
without producing a final answer.

Please try a more specific query or reduce the scope of your request.
```

---

## 10. Complete Pipe Logic

```python
from pydantic import BaseModel, Field
from typing import Literal, Optional, AsyncGenerator
import httpx
import json


class Pipe:
    class Valves(BaseModel):
        GATEWAY_BASE_URL: str = Field(
            default="",
            description="Base URL for the OpenAI-compatible gateway (e.g. Bifrost).",
        )
        GATEWAY_AUTH_HEADER: str = Field(
            default="x-bf-vk",
            description="HTTP header name for the API key.",
        )
        GATEWAY_AUTH_VALUE: str = Field(
            default="",
            description="Credential value sent in the configured auth header.",
            json_schema_extra={"input": {"type": "password"}},
        )
        GATEWAY_HOST_HEADER: str = Field(
            default="x-bf-dim-host",
            description="HTTP header name for the host routing value.",
        )
        GATEWAY_HOST_VALUE: str = Field(
            default="",
            description="Value sent in the host routing header.",
        )
        MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING: int = Field(
            default=2,
            description="Consecutive identical tool calls before first warning.",
        )
        MAX_TOOL_CALLS_PER_TURN: int = Field(
            default=15,
            description="Max tool calls in a turn before force-termination.",
        )
        ENABLE_PREVENTIVE_REMINDER: bool = Field(
            default=True,
            description="Inject periodic self-evaluation reminders.",
        )
        REMINDER_INTERVAL: int = Field(
            default=3,
            description="Inject preventive reminder every N user messages.",
        )
        INJECTION_POSITION: Literal["prepend", "append_system", "append_user"] = Field(
            default="prepend",
            description="Where to inject the warning message.",
        )
        MAX_WARNINGS_BEFORE_TERMINATE: int = Field(
            default=2,
            description="Escalation steps before force-termination.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._models_cache: list[dict] = []

    # ------------------------------------------------------------------
    # Model discovery (manifold)
    # ------------------------------------------------------------------

    async def pipes(self) -> list[dict]:
        """Query gateway for available models. Cache on success, fallback on failure."""
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
            return self._models_cache or [
                {"id": "error", "name": "⚠️ Gateway unreachable"}
            ]

        self._models_cache = [
            {"id": m["id"], "name": f"🛡️ {m.get('name', m['id'])}"}
            for m in data.get("data", [])
        ]
        return self._models_cache

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

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

    def _has_consecutive_duplicates(
        self, history: list[dict], threshold: int
    ) -> bool:
        if len(history) < threshold:
            return False
        recent = history[-threshold:]
        first = recent[0]
        return all(
            c["name"] == first["name"] and c["args"] == first["args"]
            for c in recent
        )

    def _escalation_level(self, messages: list[dict]) -> int:
        for msg in reversed(messages):
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            if "FINAL WARNING:" in content:
                return 2
            if "WARNING:" in content:
                return 1
        return 0

    def _last_system_contains(self, messages: list[dict], text: str) -> bool:
        return any(
            msg.get("role") == "system"
            and text.lower() in msg.get("content", "").lower()
            for msg in messages
        )

    # ------------------------------------------------------------------
    # Injection
    # ------------------------------------------------------------------

    def _inject(self, messages: list[dict], message: dict) -> None:
        pos = self.valves.INJECTION_POSITION
        if pos == "prepend":
            messages.insert(0, message)
        elif pos == "append_system":
            idx = -1
            for i, m in enumerate(messages):
                if m.get("role") == "system":
                    idx = i
            messages.insert(idx + 1, message)
        elif pos == "append_user":
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    messages.insert(i, message)
                    return
            messages.append(message)

    # ------------------------------------------------------------------
    # Gateway helpers
    # ------------------------------------------------------------------

    def _build_gateway_headers(self) -> dict:
        """Build the headers dict for gateway requests."""
        headers = {}
        if self.valves.GATEWAY_AUTH_VALUE:
            headers[self.valves.GATEWAY_AUTH_HEADER] = self.valves.GATEWAY_AUTH_VALUE
        if self.valves.GATEWAY_HOST_VALUE:
            headers[self.valves.GATEWAY_HOST_HEADER] = self.valves.GATEWAY_HOST_VALUE
        return headers

    # ------------------------------------------------------------------
    # Gateway proxy
    # ------------------------------------------------------------------

    async def _stream(self, payload: dict, headers: dict, url: str) -> AsyncGenerator:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line:
                        yield line

    async def _call(self, payload: dict, headers: dict, url: str) -> dict:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def pipe(self, body: dict, __user__: Optional[dict] = None, __metadata__: Optional[dict] = None):
        messages = body.get("messages", [])
        if not messages:
            return ""

        # Strip pipe prefix from model ID
        # "pipe-uuid.deepseek/deepseek-v4-flash" → "deepseek/deepseek-v4-flash"
        real_model = body["model"].split(".", 1)[-1]

        # --- Analysis -------------------------------------------------
        history = self._extract_tool_calls_in_turn(messages)
        total = len(history)
        escalation = self._escalation_level(messages)
        max_esc = self.valves.MAX_WARNINGS_BEFORE_TERMINATE

        # --- Runaway prevention (immediate force-terminate) -----------
        if (
            self.valves.MAX_TOOL_CALLS_PER_TURN > 0
            and total >= self.valves.MAX_TOOL_CALLS_PER_TURN
        ):
            return (
                f"I've stopped because this turn reached the limit of "
                f"{self.valves.MAX_TOOL_CALLS_PER_TURN} tool calls "
                f"without producing a final answer.\n\n"
                f"Please try a more specific query or reduce the scope "
                f"of your request."
            )

        # --- Escalation: warn → final warn → terminate ----------------
        loop_detected = (
            history
            and self.valves.MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING > 0
            and self._has_consecutive_duplicates(
                history,
                self.valves.MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING,
            )
        )

        if loop_detected:
            if escalation >= max_esc:
                return (
                    f"I've stopped because I was repeating the same tool "
                    f"call ({total} calls in this turn) without making "
                    f"progress.\n\n"
                    f"Please refine your query or check whether the tools "
                    f"are returning useful results."
                )
            elif escalation == 1:
                self._inject(messages, {
                    "role": "system",
                    "content": (
                        "FINAL WARNING: You are still repeating tool "
                        "calls despite receiving a warning. You MUST "
                        "stop calling tools immediately. Provide a "
                        "summary of everything you have gathered."
                    ),
                })
            else:
                self._inject(messages, {
                    "role": "system",
                    "content": (
                        "WARNING: In this turn you called the same "
                        "tool with the same arguments multiple times "
                        "without achieving a different result. Stop "
                        "and change strategy, or provide a summary."
                    ),
                })

        # --- Preventive reminder --------------------------------------
        if self.valves.ENABLE_PREVENTIVE_REMINDER and not loop_detected:
            user_count = sum(1 for m in messages if m.get("role") == "user")
            if user_count > 0 and user_count % self.valves.REMINDER_INTERVAL == 0:
                if not self._last_system_contains(messages, "REMINDER:"):
                    self._inject(messages, {
                        "role": "system",
                        "content": (
                            "REMINDER: Periodically evaluate whether "
                            "your tool calls are making progress. If "
                            "you detect repetition without results, "
                            "stop and provide a summary."
                        ),
                    })

        # --- Forward to gateway ---------------------------------------
        payload = {**body, "model": real_model, "messages": messages}

        headers = {"Content-Type": "application/json", **self._build_gateway_headers()}

        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

        try:
            if body.get("stream", False):
                return self._stream(payload, headers, url)
            else:
                return await self._call(payload, headers, url)
        except Exception as e:
            return f"Error calling gateway: {e}"
```

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

🛑 Force-terminated. Zero wasted tokens.
```

---

## 12. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation | 0 | Pipe forwards body as-is |
| Loop warning | ~60-80 | Injected system message |
| Final warning | ~70-90 | Second injection |
| Preventive reminder | ~50-70 | Every N user messages |
| Force-termination | **0 LLM tokens** | Pipe returns string, skips LLM |
| Runaway limit | **0 LLM tokens** | Pipe returns string |

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
