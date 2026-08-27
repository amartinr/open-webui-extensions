# Agent Loop Guard

Open WebUI **Pipe Function** that interrupts tool-calling loops in LLM
agents by replacing the offending tool result with a guard message, without
discarding the tool results already collected.

---

## Problem

LLM agents with tool access can repeat tool calls:

- calling `search_web("X")` with the same arguments repeatedly;
- chaining expensive tools until Open WebUI's hard iteration limit
  (default **256**) stops them;
- consuming tokens and credits at scale, particularly with batch tool calls.

Open WebUI's built-in `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (256) is a
brute-force ceiling: it lets the agent exhaust all iterations before
stopping. The Agent Loop Guard stops earlier and on a more specific signal
(repeated calls), not on a raw iteration count.

---

## Solution

The Agent Loop Guard is an **Open WebUI Pipe Function** placed between the
UI and the LLM gateway (e.g. Bifrost, LiteLLM). It:

1. **analyses** each request for consecutive identical tool calls;
2. **replaces** the last tool result with a guard message when a loop or
   runaway is detected, instructing the model to stop and summarise;
3. **preserves** the tool results already collected;
4. **caps** runaway with a configurable tool-call limit per turn.

### Result replacement vs. force-terminate

A hard force-terminate discards the pending batch of tool results. Result
replacement instead keeps all collected results and swaps only the last one
for a guard instruction to summarise. Tools stay present in the body, so the
model could issue new calls, but the guard message discourages it and the
guard re-fires if it persists.

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
| `ATTACHED_FILES_CLEANUP` | `True` | Collapse and deduplicate `<attached_files>` blocks **within each user message** (per-turn: the core's block and the image_filter's current-turn block for the same upload merge to one tag; re-uploads in later turns keep their own block). Cache-safe: historical messages stay byte-stable between turns. `False` = forward payloads unchanged |

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

## Attached-Files Cleanup

Since v2.2.0 the pipe also cleans up `<attached_files>` blocks that Open
WebUI injects (the `image_filter` inlet prepends one block with the
current turn's images to the last user message; the core's
`add_file_context()` prepends one block per stored user message with
files). Within a turn the same upload can be tagged twice (core + filter),
and before filter v2.12.0 the filter re-announced a growing union block
that moved every turn — which broke LLM prefix caching. The cleanup
collapses the duplicates within each message and keeps historical
messages byte-stable between turns.

The cleanup is **cache-safe**: dedup is scoped **within each user
message** (one message = one turn), so the history prefix stays
byte-identical between turns (deterministic, idempotent, fail-open). Each
turn keeps **its own files** — including a file deliberately re-uploaded
in a later turn, which gets a fresh block and stays visible to the agent
(it is never deduplicated away, since v2.4.0). Within a message, the
core's block and the filter's current-turn block for the same upload
collapse to one tag. Dedup is a **UUID match** (the file UUID is unique)
plus a **content-hash backstop** (v2.3.0): two *different* UUIDs whose
files share `meta["file_hash"]` also collapse within the same message, so
a `+` upload never shows twice even when the filter and the core tag
different copies of the same image. Every **image** tag is re-emitted in
**our canonical format** (`id` + absolute `/api/v1/files/{id}/content`
URL) so the same image looks identical regardless of which source produced
it (Open WebUI's raw bare-UUID form, the core's relative form, or the
filter's absolute form). **Non-image tags (PDFs, documents) are left
untouched**: they are never deduplicated nor rewritten. Disable with the
`ATTACHED_FILES_CLEANUP` valve.

Since filter v2.12.0, pasted images are announced only in the turn they
are pasted (the filter no longer re-announces re-hydrated history), so the
"moving union block" scenario no longer occurs — the pipe's remaining job
is to collapse the core's per-message block with the filter's current-
turn block in the same message (dedup by UUID, plus the content-hash
backstop for the fallback path) while leaving re-uploads in later turns
visible. With the filter at **v2.12.3** the
filter and the core converge on the current upload's UUID (confirmed in
runtime logs: `reused this-turn upload ... (content hash match)`, pipe
`kept 1 (1 dropped)`), so the UUID dedup collapses the pair; the
content-hash backstop (v2.3.0) remains for the fallback path.

See `DESIGN.md` §18 and `filters/image_filter/DESIGN.md` →
"Attached-Files Accumulation" for details, and `EXAMPLE.md` for a
side-by-side before/after walkthrough.

## Robust SSE forwarding (v2.6.0)

The pipe proxies the gateway's raw SSE to Open WebUI. `_stream` forwards
only well-formed OpenAI `data: { ... }` chunk events; everything else is
dropped:

- **SSE comments / keep-alives** (lines starting with `:`). Open WebUI's
  pipe handler renders any non-`data:` line as chat content, so a keep-alive
  (e.g. `: heartbeat`) is otherwise rendered into the reply and desyncs the
  reasoning deltas.
- **Blank lines** and **`data: [DONE]`** (Open WebUI emits its own closing
  chunk).
- **Any other non-JSON noise** (lines that do not start with `data: {`).

The relay, before and after filtering:

```text
# Gateway emits (raw SSE)
: heartbeat

data: {"id":"1","choices":[{"delta":{"reasoning_content":"let me"}}]}

data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}
data: [DONE]
```

```text
# After _stream filtering (what reaches Open WebUI)
data: {"id":"1","choices":[{"delta":{"reasoning_content":"let me"}}]}
data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}
```

The keep-alive `: heartbeat`, the blank line, and `data: [DONE]` are
dropped; only the two `data: { ... }` chunk events pass through. This
keeps the relay OpenAI-compatible and the reasoning deltas aligned with
the frontend. Validated against a live Bifrost stream (long reasoning):
1917 events, 0 corruption.

### Why the guard exists

Bifrost's OpenAI-compatible SSE is not reliable: keep-alive lines leak into
the stream, and upstream
[#6523](https://github.com/maximhq/bifrost/issues/6523) dropped opening
role-only chunks break stream assembly in OpenAI-compatible SDKs. If
Bifrost's SSE normalization is fixed, the filter can be relaxed or removed.

## Bifrost reasoning normalization (v2.7.0)

DeepSeek requires `reasoning_content` on **every** assistant message of a
tool-calling history. When Open WebUI executes a tool call, it rebuilds the
assistant `tool_calls` message from stored output items and omits
`reasoning_content` (OpenAI-compatible providers replay reasoning as
`reasoning_details`, or nothing at all), and the request goes straight back
to the pipe — filter inlets do **not** run on tool-call continuations.
Bifrost does not translate the fields back, so DeepSeek silently drops
reasoning on that turn.

Since v2.7.0 the pipe normalizes every outbound payload before forwarding
(mirroring `pi-bifrost-reasoning-fix`):

1. Renames assistant `reasoning` / `reasoning_details` into
   `reasoning_content` (content-driven).
2. Once tool-calling is in scope (request `tools` or tool-call history),
   forces `reasoning_content` (empty if none yet) on every assistant
   message.

The rewrite is deterministic and never touches user/system/tool messages,
so the provider prefix cache is preserved. Validated against a live Bifrost
endpoint (`deepseek/deepseek-v4-flash`):

| Continuation history (assistant with `tool_calls`) | Reasoning on next turn |
|---|---|
| no `reasoning_content` | ❌ lost |
| `reasoning_content` (even `""`) | ✅ kept |
| `reasoning_details` (Bifrost dialect) | ❌ lost |

This complements `filters/bifrost_reasoning_content_fix` (v3.2.0), whose
`inlet` applies the same normalization to fresh user turns (where history
from earlier tool-calling turns is replayed).

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
