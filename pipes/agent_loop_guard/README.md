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
                               ├─ Detect consecutive duplicates
                               ├─ Check escalation level
                               │
                               ├─ Runaway? (≥ MAX_TOOL_CALLS_PER_TURN)
                               │     └─ Soft-block: remove tools, inject
                               │        system msg, forward to gateway
                               │
                               ├─ Loop detected?
                               │     ├─ Escalation ≥ max? → Soft-block
                               │     ├─ Escalation = 1? → FINAL WARNING
                               │     └─ Escalation = 0? → WARNING
                               │
                               └─ Forward to gateway (tools untouched)
                                     → LLM responds normally
```

### Escalation Ladder

```
Level 0: Silent monitoring
  │  Consecutive duplicate tool calls detected
  ▼
Level 1: WARNING (tools still available)
  "{tool_name} called {total}x with same args. Change approach or summarize."
  │  Agent ignores → continues looping
  ▼
Level 2: FINAL WARNING (tools still available)
  "{tool_name} called {total}x. Still repeating. Stop now and summarize."
  │  Agent still ignores
  ▼
SOFT-BLOCK: only the looping tool removed from body["tools"]
  Agent receives instruction + all collected results, can use other tools
  "TOOL REMOVED: {tool_name} blocked after {total} identical calls."
  → Agent has other tools available or can summarise.
```

---

## Installation

1. In Open WebUI, go to **Admin Panel → Functions → Create Function**
2. Select **Pipe** as the function type
3. Paste the contents of `agent_loop_guard.py`
4. Save. The function registers one sub-pipe per model from your gateway.
5. Create **Workspace Models** pointing at the protected sub-pipes.

### Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for your OpenAI-compatible gateway (e.g. Bifrost) |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | API key/credential (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers. Supports `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before soft-block. `0` = disabled |
| `MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING` | `2` | Identical consecutive calls before first warning. `0` = disabled |
| `MAX_WARNINGS_BEFORE_TERMINATE` | `2` | Warnings before soft-block. `0` = soft-block on first detection |
| `SHOW_TOOL_COUNTER` | `True` | Append descending counter (`remaining tool calls: N`) to every tool result |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to **remove** from the agent's tool list. Example: `"delete_file, terminal_execute"` |
| `INJECTION_POSITION` | `"append_user"` | Where to inject: `"append_user"` (before last user message) or `"merge_last_tool"` (append to last tool result) |

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

See [PLAN.md](./PLAN.md) for details.

- **Per-model overrides**: different thresholds per sub-pipe
- **Production hardening**: comprehensive tests, edge case coverage

### Implemented

- **Phase 6 — Tool Blocklist** (`TOOL_BLOCKLIST` valve): remove tools from
  the agent's tool list by name (e.g. block `delete_file`, `terminal_execute`).
  Matching is exact — `fetch_url` does not block `smart_fetch_url`.

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
