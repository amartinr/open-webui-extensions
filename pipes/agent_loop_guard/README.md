# Agent Loop Guard

> 🛡️ An Open WebUI **Pipe Function** that prevents AI agents from entering
> infinite tool-calling loops — without wasting tool results or burning
> LLM tokens.

---

## Problem

LLM agents with tool access can easily fall into loops:

- Calling `search_web("X")` with the same arguments over and over
- Executing expensive tools in endless chains until a hard iteration limit
  (default **256**) kicks in
- Wasting tokens, API credits, and time — especially with batch tool calls

Open WebUI's built-in `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (256) is a
brute-force limit that lets the agent exhaust all iterations before stopping.
The Agent Loop Guard intercepts **much earlier** and with **more
intelligence**.

---

## Solution

The Agent Loop Guard is an **Open WebUI Pipe Function** that sits between
the UI and your LLM gateway (e.g. Bifrost, LiteLLM). It:

1. **Analyses** every request for consecutive identical tool calls
2. **Replaces** the last tool result with a guard message when a loop or
   runaway is detected — the LLM receives a clear instruction to stop
   repeating and summarise
3. **Preserves** all collected tool results — nothing is wasted
4. **Prevents runaway** loops with a configurable tool-call limit per turn

### Result replacement vs Force-terminate

Unlike a hard force-terminate (which wastes the last batch of tool results),
result replacement preserves everything the agent has already gathered. The
LLM gets all tool results, with the most recent one replaced by a guard
instruction to summarise. Tools remain available in the body, so the LLM
*could* make new calls — but the guard message strongly discourages this,
and the guard will fire again if the agent persists.

---

## How it Works

```
User message → Open WebUI → Agent Loop Guard pipe()
                               │
                               ├─ Analyse tool calls via _analyse()
                               │    ├─ Scan messages backwards from end
                               │    ├─ Skip previously guarded calls
                               │    ├─ Count consecutive identical calls
                               │    └─ Decide: loop? runaway? none?
                               │
                               ├─ Loop detected? (consecutive ≥ threshold)
                               │     └─ Replace last tool result with
                               │        "[Tool call budget exhausted] - loop detected"
                               │
                               ├─ Runaway? (total ≥ MAX_TOOL_CALLS_PER_TURN)
                               │     └─ Replace last tool result with
                               │        "[Tool call budget exhausted] - turn limit reached"
                               │
                               ├─ Emit UI notification + status pill
                               ├─ Apply tool blocklist
                               └─ Forward to gateway (with modified messages)
                                     → LLM responds (ideally summarises)
```

### Detection logic

The guard detects **two** conditions, evaluated in order:

| State | Condition | Action |
|:-----:|:---------:|--------|
| **Loop** | Consecutive identical tool calls (same name **and** same arguments) reach `MAX_CONSECUTIVE_TOOL_CALLS` | Tool result replaced with loop-specific message naming the tool |
| **Runaway** | Total tool calls in the turn reach `MAX_TOOL_CALLS_PER_TURN` (only if no loop detected) | Tool result replaced with runaway message |

There is **no escalation ladder** — the guard fires directly at the
configured threshold without intermediate warnings.

### Loop vs Runaway priority

**Loop wins over runaway.** If both conditions are met simultaneously, the
guard fires as a loop block. This gives the agent a more specific message
(naming the offending tool) rather than a generic limit message.

---

## Installation

1. In Open WebUI, go to **Admin Panel → Functions → Create Function**
2. Select **Pipe** as the function type
3. Paste the contents of `agent_loop_guard.py`
4. Save. The function registers one sub-pipe per model from your gateway.
5. Create **Workspace Models** pointing at the protected sub-pipes.

### Configuration

### Admin valves (Pipe.Valves)

Configured in the Function admin panel.

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for your OpenAI-compatible gateway (e.g. Bifrost) |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | API key/credential (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers. Supports `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before runaway guard fires. `0` = disabled |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `4` | Consecutive identical calls before loop guard fires (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to **remove** from the agent's tool list. Example: `"delete_file, terminal_execute"` |

> **Validation**: `MAX_TOOL_CALLS_PER_TURN` must be **greater than**
> `MAX_CONSECUTIVE_TOOL_CALLS` when both are enabled. The pipe validates
> this at config time — if runaway's threshold is equal or lower, Open
> WebUI will reject the configuration with an error.

### User valves (Pipe.UserValves)

Configured per workspace model. A value of `0` defers to the admin default.

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-model override. `0` = use admin default. |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `0` | Per-model override. `0` = use admin default. |

If the user's effective limits violate the `runaway > loop` constraint, a
warning is logged at runtime and the pipe continues (but runaway may fire
before loop detection).

### Custom headers with templates

```json
{
  "x-bf-dim-host": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}",
  "x-user-email": "{{USER_EMAIL}}"
}
```

These template variables are resolved at runtime with the current user's
data. Unlike Open WebUI's global `ENABLE_FORWARD_USER_INFO_HEADERS` (which
only works for native OpenAI/Ollama routing), this works inside the pipe
for any gateway destination.

---

## Architecture

### Why a Pipe instead of a Filter?

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
only a Pipe can **definitively remove tools** from the request body and
**skip the LLM call entirely** when needed.

### Router bypass

The Agent Loop Guard is a **custom router** — it does NOT pass through
Open WebUI's `routers/openai.py` or `routers/ollama.py`. It makes direct
HTTP requests to your gateway via `httpx.AsyncClient`. This means:

- ✅ Full control over headers, auth, and body modifications
- ✅ Gateway-agnostic (Bifrost, LiteLLM, OpenAI-compatible proxies)
- ❌ `ENABLE_FORWARD_USER_INFO_HEADERS` has no effect (solved via
  `GATEWAY_CUSTOM_HEADERS` templates)

---

## File Layout

```
pipes/agent_loop_guard/
├── README.md              ← This file
├── DESIGN.md              # Full design document (reference)
└── agent_loop_guard.py    # Single-file pipe
```

The pipe is a single Python file because Open WebUI Functions are stored
as a single source blob in the database. No `__init__.py`, no package.

---

## License

MIT
