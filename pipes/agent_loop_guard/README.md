# Agent Loop Guard

Open WebUI **Pipe Function** that interrupts tool-calling loops in LLM
agents by replacing the offending tool result with a guard message, without
discarding the tool results already collected.

## Problem

LLM agents with tool access can repeat tool calls:

- calling the same tool with the same arguments repeatedly;
- chaining expensive tools until Open WebUI's hard iteration limit
  (default **256**) stops them.

Open WebUI's built-in `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (256) is a
brute-force ceiling: it lets the agent exhaust all iterations before
stopping. The Agent Loop Guard stops earlier, on a more specific signal
(repeated calls), not on a raw iteration count.

## Solution

The pipe sits between the UI and the LLM gateway (Bifrost, LiteLLM, any
OpenAI-compatible proxy). It:

1. analyses each request for consecutive identical tool calls;
2. replaces the last tool result with a guard message when a loop or
   runaway is detected, instructing the model to stop and summarise;
3. preserves the tool results already collected;
4. caps runaway with a configurable tool-call limit per turn.

### Result replacement vs. force-terminate

Force-terminate discards the pending batch of tool results. Result
replacement keeps all collected results and swaps only the last one for a
guard instruction to summarise. Tools stay in the body, so the model could
issue new calls, but the guard message discourages it and the guard
re-fires if it persists.

## How it works

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

| State | Condition | Action |
|:-----:|:---------:|--------|
| **Loop** | Consecutive identical tool calls (same name **and** same arguments) reach `MAX_CONSECUTIVE_TOOL_CALLS` | Tool result replaced with loop-specific message naming the tool |
| **Runaway** | Total tool calls in the turn reach `MAX_TOOL_CALLS_PER_TURN` (only if no loop detected) | Tool result replaced with runaway message |

- There is **no escalation ladder** — the guard fires at the configured
  threshold without intermediate warnings.
- **Loop wins over runaway**: if both conditions are met, the guard fires
  as a loop block (more specific message naming the tool).

## Installation

1. Open WebUI → **Admin Panel → Functions → Create Function**
2. Select **Pipe** as the function type
3. Paste the contents of `agent_loop_guard.py`
4. Save. The function registers one sub-pipe per model from your gateway.
5. Create **Workspace Models** pointing at the protected sub-pipes.

## Configuration

### Admin valves (Pipe.Valves)

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway (e.g. Bifrost) |
| `GATEWAY_AUTH_HEADER` | `"x-bf-vk"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | API key/credential (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers. Supports `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls before runaway guard fires. `0` = disabled |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `4` | Consecutive identical calls before loop guard fires (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove from the agent's tool list. Example: `"delete_file, terminal_execute"` |
| `ATTACHED_FILES_CLEANUP` | `True` | Collapse and deduplicate `<attached_files>` blocks within each user message (see [Attached-Files Cleanup](#attached-files-cleanup)). `False` = forward payloads unchanged |
| `REASONING_DEBUG_LOG` | `False` | Trace reasoning_content on the outbound payload: `R0`/`R{n}` flags in the messages summary plus the last assistant's reasoning_content. For debugging reasoning drops only |

> **Validation**: `MAX_TOOL_CALLS_PER_TURN` must be **greater than**
> `MAX_CONSECUTIVE_TOOL_CALLS` when both are enabled. Open WebUI rejects
> the configuration otherwise.

### User valves (Pipe.UserValves)

Configured per workspace model. A value of `0` defers to the admin default.

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-model override. `0` = use admin default. |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `0` | Per-model override. `0` = use admin default. |

If the effective limits violate the `runaway > loop` constraint, a warning
is logged at runtime and the pipe continues (runaway may fire before loop
detection).

### Custom headers with templates

```json
{
  "x-bf-dim-host": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}",
  "x-user-email": "{{USER_EMAIL}}"
}
```

Template variables resolve at runtime with the current user's data. Unlike
Open WebUI's global `ENABLE_FORWARD_USER_INFO_HEADERS` (native OpenAI/Ollama
routing only), this works inside the pipe for any gateway destination.

## Attached-Files Cleanup

Open WebUI injects `<attached_files>` blocks in two places: the
`image_filter` inlet prepends one block with the current turn's images to
the last user message, and the core's `add_file_context()` prepends one
block per stored user message with files. Within a turn the same upload can
be tagged twice (core + filter).

The cleanup (`ATTACHED_FILES_CLEANUP`, since v2.2.0) collapses duplicates
**within each user message** (one message = one turn) and keeps historical
messages byte-stable between turns. Cache-safe: the history prefix stays
byte-identical (deterministic, idempotent, fail-open).

- Dedup is scoped **within a message**, never across messages: a file
  deliberately re-uploaded in a later turn gets a fresh block and stays
  visible (v2.4.0).
- Dedup key: **UUID match** plus a **content-hash backstop** (v2.3.0): two
  different UUIDs sharing `meta["file_hash"]` also collapse, so a `+`
  upload never shows twice.
- Every **image** tag is re-emitted in a canonical format (`id` + absolute
  `/api/v1/files/{id}/content` URL) so the same image renders identically
  regardless of source (raw bare-UUID form, core relative form, filter
  absolute form).
- **Non-image tags** (PDFs, documents) are never deduplicated nor
  rewritten.

With the image filter at **v2.12.x**, pasted images are announced only in
the turn they are pasted, and the filter converges on the current upload's
UUID — the pipe's remaining job is collapsing the core's per-message block
with the filter's current-turn block in the same message.

See `DESIGN.md` §18 and `filters/image_filter/DESIGN.md` →
"Attached-Files Accumulation" for details, and `EXAMPLE.md` for a
before/after walkthrough.

## Robust SSE forwarding (v2.6.0)

`_stream` proxies the gateway's raw SSE to Open WebUI, forwarding only
well-formed OpenAI `data: { ... }` chunk events. Everything else is
dropped:

- **SSE comments / keep-alives** (`: heartbeat`) — Open WebUI's pipe
  handler renders any non-`data:` line as chat content, which would desync
  the reasoning deltas.
- **Blank lines** and **`data: [DONE]`** — Open WebUI emits its own closing
  chunk.
- **Any other non-JSON noise**.

Before:

```text
: heartbeat

data: {"id":"1","choices":[{"delta":{"reasoning_content":"let me"}}]}

data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}
data: [DONE]
```

After `_stream` filtering:

```text
data: {"id":"1","choices":[{"delta":{"reasoning_content":"let me"}}]}
data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}
```

Validated against a live Bifrost stream (long reasoning): 1917 events, 0
corruption.

## Bifrost reasoning normalization (v2.7.0)

DeepSeek requires `reasoning_content` on **every** assistant message of a
tool-calling history. When Open WebUI executes a tool call, it rebuilds the
assistant `tool_calls` message from stored output items and omits
`reasoning_content` (OpenAI-compatible providers replay reasoning as
`reasoning_details`, or nothing at all), and the request goes straight back
to the pipe — filter inlets do **not** run on tool-call continuations.

The pipe normalizes every outbound payload before forwarding (mirroring
`pi-bifrost-reasoning-fix`):

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
`inlet` applies the same normalization to fresh user turns.

### Monkey patch of Open WebUI internals (v2.12.0)

To replay the reasoning above, the pipe **replaces a function inside Open
WebUI at import time** (`_install_reasoning_replay_patch`): it swaps
`open_webui.utils.middleware.get_reasoning_format` for a wrapped version.

**Purpose.** Open WebUI asks `get_reasoning_format(model)` how to replay
stored reasoning when rebuilding assistant history for a tool-call
continuation. In v0.11.x the function returns a format only for Ollama
(`'thinking'`) and llama.cpp (`'reasoning_content'`) models, and **`None`
for every OpenAI-compatible model — including pipe models**. With `None`,
`convert_output_to_messages` **discards the reasoning entirely** — the
replayed assistant reaches Bifrost without `reasoning_content` and DeepSeek
stops reasoning on the next turn. Payload normalization downstream cannot
recover text already dropped here.

**Scope.** For models with `owned_by == "openai"` **and** a `pipe` key (the
pipe's own models), the patch makes `get_reasoning_format` return
`'reasoning_content'`, so Open WebUI rebuilds the assistant with the real
reasoning text in that field. Direct OpenAI connections and
Ollama/llama.cpp models keep their original behavior.

**Fail-open and idempotent.** The patch is applied once at import and
short-circuits on a marker attribute (`_bf_reasoning_patched`) if already
installed. If Open WebUI changes these internals, the patch raises and the
pipe falls back to the empty-string forcing — no crash, no total reasoning
loss.

**Trade-off.** The patch depends on Open WebUI private implementation
details (`middleware.get_reasoning_format` and the
`convert_output_to_messages` contract). A future Open WebUI release can
break or supersede it (e.g. returning `'reasoning_content'` natively for
pipe models, which would make the patch a no-op).

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
both Filters and Pipes run on every tool-call iteration. Only a Pipe can
**definitively remove tools** from the request body and **skip the LLM
call entirely** when needed.

### Router bypass

The pipe is a **custom router** — it does not pass through Open WebUI's
`routers/openai.py` or `routers/ollama.py`. It makes direct HTTP requests
to the gateway via `httpx.AsyncClient`:

- ✅ Full control over headers, auth, and body modifications
- ✅ Gateway-agnostic (Bifrost, LiteLLM, OpenAI-compatible proxies)
- ❌ `ENABLE_FORWARD_USER_INFO_HEADERS` has no effect (solved via
  `GATEWAY_CUSTOM_HEADERS` templates)

## Compatibility

Validated against Open WebUI **0.11.1** + Bifrost **2.0.0** (core 1.8.3) +
DeepSeek **v4 flash/pro**:

- Bifrost core ≥ 1.7.10 required for tool-call reasoning replay
  ([maximhq/bifrost#5887](https://github.com/maximhq/bifrost/issues/5887)).
- Bifrost core ≥ 1.8.0 required for `reasoning_content` in stream deltas
  ([maximhq/bifrost#6523](https://github.com/maximhq/bifrost/issues/6523)).
- On 2.0.0 the reasoning path is clean (0/34 SSE mismatches with
  `tests/repro_bifrost_reasoning_loss.mjs`). The pipe stays necessary:
  stream deltas still carry `reasoning_details`, which Open WebUI v0.11.1
  suppresses from the live reasoning event unless stripped.

## File Layout

```
pipes/agent_loop_guard/
├── README.md              ← This file
├── DESIGN.md              # Full design document (reference)
├── EXAMPLE.md             # Before/after walkthrough
├── agent_loop_guard.py    # Single-file pipe
└── tests/                 # Unit tests + Bifrost integration probe
```

The pipe is a single Python file because Open WebUI Functions are stored
as a single source blob in the database. No `__init__.py`, no package.

## License

MIT
