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
2. **Warns** the agent with escalating system messages (WARNING → FINAL
   WARNING)
3. **Soft-blocks** by removing tools from the request body — the LLM
   receives all collected results but cannot call more tools, forcing it
   to summarise
4. **Prevents runaway** loops with a configurable tool-call limit per turn

### Soft-block vs Force-terminate

Unlike a hard force-terminate (which wastes the last batch of tool
results), soft-block preserves everything the agent has already gathered.
The LLM gets all tool results plus a system instruction to summarise, but
with `tools` removed from the body, it **cannot** schedule more tool
calls — it must respond with text.

---

## How it Works

```
User message → Open WebUI → Agent Loop Guard pipe()
                               │
                               ├─ Extract tool calls in current turn
                               ├─ Count consecutive duplicates
                               ├─ Apply formula-based escalation
                               │
                               ├─ Runaway? (≥ MAX_TOOL_CALLS_PER_TURN)
                               │     └─ Soft-block: remove tools, inject
                               │        system msg, forward to gateway
                               │
                               ├─ Loop detected?
                               │     ├─ consecutive ≥ N  → Soft-block
                               │     ├─ consecutive == final_pos → FINAL WARNING
                               │     ├─ consecutive == 2 → WARNING
                               │     └─ otherwise → silent
                               │
                               └─ Forward to gateway (tools untouched)
                                     → LLM responds normally
```

### Escalation Ladder

With `MAX_CONSECUTIVE_BEFORE_BLOCK` (default 4):

```
consecutive == 2  → WARNING (system message, tools still available)
consecutive == 3  → FINAL WARNING (system message, tools still available, if threshold > 3)
consecutive >= 4  → SOFT-BLOCK: only the looping tool removed from body + metadata
```

Each level fires **exactly once**. Higher thresholds spread FINAL WARNING further
from the block. For `N=3` there is no FINAL WARNING (WARNING → block directly).

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
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before soft-block. `0` = disabled |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | `4` | Consecutive identical tool calls before soft-block (min 3). Warnings spaced automatically: WARNING on first detection, FINAL WARNING at ~60% of threshold |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to **remove** from the agent's tool list. Example: `"delete_file, terminal_execute"` |

> **Validation**: `MAX_TOOL_CALLS_PER_TURN` must be greater than `MAX_CONSECUTIVE_BEFORE_BLOCK`
> when both are enabled. The pipe validates this at config time — if runaway's threshold
> is equal or lower, Open WebUI will reject the configuration with an error.

### User valves (Pipe.UserValves)

Configured per workspace model. A value of `0` defers to the admin default.

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-model override. `0` = use admin default. |
| `MAX_CONSECUTIVE_BEFORE_BLOCK` | `0` | Per-model override. `0` = use admin default. |

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
| **Force-terminate / skip LLM call** | ❌ Must return body | ✅ Returns string (soft-block preferred) |
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

## Upcoming Features

- **Per-model overrides**: different thresholds per sub-pipe
- **Production hardening**: comprehensive tests, edge case coverage

---

## File Layout

```
pipes/agent_loop_guard/
├── README.md            ← This file
├── DESIGN.md            # Full design document (reference)
├── PLAN.md              # Phased implementation plan
└── agent_loop_guard.py  # Single-file pipe (all phases)
```

The pipe is a single Python file because Open WebUI Functions are stored
as a single source blob in the database. No `__init__.py`, no package.

---

## License

MIT
