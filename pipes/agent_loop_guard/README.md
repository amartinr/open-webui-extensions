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

The pipe sits between the UI and the LLM gateway (LiteLLM or any
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
                               ├─ Runaway? (total > MAX_TOOL_CALLS_PER_TURN)
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
| **Runaway** | Total tool calls in the turn **exceed** `MAX_TOOL_CALLS_PER_TURN` (the limit itself is allowed — only if no loop detected) | Result of the first call **beyond** the limit replaced with runaway message |

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
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway (e.g. LiteLLM) |
| `GATEWAY_AUTH_HEADER` | `"Authorization"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | API key/credential (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers. Supports `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`, `{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}` |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls allowed per turn before the runaway guard fires on the next call (the call beyond the limit is blocked). `0` = disabled |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `4` | Consecutive identical calls before loop guard fires (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove from the agent's tool list. Example: `"delete_file, terminal_execute"` |
| `ATTACHED_FILES_CLEANUP` | `True` | Collapse and deduplicate `<attached_files>` blocks within each user message (see [Attached-Files Cleanup](#attached-files-cleanup)). `False` = forward payloads unchanged |
| `DEBUG_LOG` | `False` | Per-request diagnostics: `R0`/`R{n}` flags in the messages summary, last assistant's reasoning_content, and the full outbound request. For debugging reasoning behavior only |
| `REPLAY_REASONING_TEXT` | `True` | Replay the REAL reasoning text on tool-call continuations by monkey-patching Open WebUI's `get_reasoning_format` for pipe models. Off = placeholder forcing only (safe); on = richer continuation reasoning (~19% more per A/B probe) but depends on Open WebUI internals (fails open). **On by default** |

> **Validation**: `MAX_TOOL_CALLS_PER_TURN` must be **greater than**
> `MAX_CONSECUTIVE_TOOL_CALLS` when both are enabled. Open WebUI rejects
> the configuration otherwise.

### User valves (Pipe.UserValves)

Configured per **user** (Open WebUI `UserValves`): on every request Open
WebUI delivers the user's override to the pipe inside `__user__["valves"]`
(a `Pipe.UserValves` instance). A value of `0` defers to the function's
admin valve (`self.valves`, which Open WebUI fills with the stored admin
configuration on every request).

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-user override of the runaway limit. `0` = use the admin value. |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `0` | Per-user override of the loop threshold. `0` = use the admin value. |

Effective limit = user override when non-zero, otherwise the admin value.
An admin `MAX_TOOL_CALLS_PER_TURN` of `0` disables the runaway guard; a
non-zero per-user override re-enables it for that user only.

**Constraint watchdog.** The admin configuration is validated at save time
(`runaway > loop` when both are enabled — rejected otherwise). Per-user
overrides are **not** pre-validated: the effective pair (admin + user mixed
per field) can violate the constraint at runtime — e.g. user
`MAX_TOOL_CALLS_PER_TURN=3` over an admin `MAX_CONSECUTIVE_TOOL_CALLS=4`
gives an effective `(3, 4)`. When that happens on a request with tool
activity, a **rate-limited warning is logged** (once per 5 minutes per
user, naming the user and the effective numbers) and the pipe continues:
with `loop >= runaway` the loop threshold is effectively unreachable (the
runaway cap fires first; a loop can only trip on an all-identical history
at the boundary), so blocks are reported as `runaway`. Fix the admin or
per-user configuration.

### Custom headers with templates

```json
{
  "x-tenant-id": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}",
  "x-user-email": "{{USER_EMAIL}}"
}
```

Template variables resolve at runtime with the current user's data. Unlike
Open WebUI's global `ENABLE_FORWARD_USER_INFO_HEADERS` (native OpenAI/Ollama
routing only), this works inside the pipe for any gateway destination.

## Tool budget in the system prompt

By default the model only discovers the tool-call budget **reactively**: it
keeps calling tools until the guard fires with
`[Tool call budget exhausted]`. To let the model self-regulate **before**
exhausting the budget, the pipe can substitute the effective limits into
the workspace model's system prompt — no new valves, the admin valves stay
the single source of truth.

### Usage

1. In **Workspace → Models**, edit the model's **System Prompt** and write
the budget tokens where you want the numbers to appear:

   ```
   You operate under a tool-call budget:
   - At most {{MAX_TOOL_CALLS_PER_TURN}} tool calls per turn.
   - At most {{MAX_CONSECUTIVE_TOOL_CALLS}} consecutive identical calls.
   Plan your tool usage so you do not exhaust this budget.
   ```

2. Save. On every request through the pipe — including every tool-call
iteration — the tokens are replaced with the **effective** limits before
the payload reaches the gateway:

   | Token | Replaced with |
   |-------|---------------|
   | `{{MAX_TOOL_CALLS_PER_TURN}}` | Effective runaway limit (per-user override when set, else the admin valve) |
   | `{{MAX_CONSECUTIVE_TOOL_CALLS}}` | Effective loop threshold (per-user override when set, else the admin valve) |
   | either token, effective limit `0` (runaway disabled) | `unlimited` |

### Semantics and guarantees

- **Same source as the guard**: the substituted numbers come from the same
  `_effective_limits()` resolution the guard enforces, so the prompt can
  never tell the model a budget different from the one applied.
- **Per-user aware**: the admin valve is the base; a user's
  `Pipe.UserValves` override (`> 0`) is substituted for that user's requests.
- **Backwards compatible**: no tokens in the system prompt → no-op, payload
  unchanged.
- **Cache-safe**: substitution is deterministic per valve state; between
  requests with unchanged valves the outgoing system prompt is
  byte-identical, so the provider prefix cache is not invalidated. Only an
  admin/per-user valve change alters the output — which is exactly when the
  model should see a new budget.
- **Fail-open**: any unexpected condition logs a warning and forwards the
  payload unchanged.

> The tokens are NOT Open WebUI variables. Open WebUI resolves only its own
> template families (`{{CURRENT_DATE}}`, `{{USER_NAME}}`, `{{USER_GROUPS}}`,
> `{{chat.variables.*}}`, `{{user.variables.*}}`, `{{prompt}}`) and leaves
> any other `{{...}}` token as literal text, so the tokens arrive intact at
> the pipe (verified in `backend/open_webui/utils/payload.py`,
> `resolve_system_prompt`).

### Debugging

Enable `DEBUG_LOG` and each substituted request logs:

```
agent-loop-guard: system-prompt budget tokens replaced=2 (max_calls=15, max_consecutive=4)
```

## Attached-Files Cleanup

Open WebUI injects `<attached_files>` blocks in two places: the
`image_filter` inlet prepends one block with the current turn's images to
the last user message, and the core's `add_file_context()` prepends one
block per stored user message with files. Within a turn the same upload can
be tagged twice (core + filter).

The cleanup (`ATTACHED_FILES_CLEANUP`) collapses duplicates **within each
user message** (one message = one turn) and keeps historical messages
byte-stable between turns. Cache-safe: the history prefix stays
byte-identical (deterministic, idempotent, fail-open).

- Dedup is scoped **within a message**, never across messages: a file
  deliberately re-uploaded in a later turn gets a fresh block and stays
  visible.
- Dedup key: **UUID match** plus a **content-hash backstop**: two different
  UUIDs sharing `meta["file_hash"]` also collapse, so a `+` upload never
  shows twice.
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

## Robust SSE forwarding

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

Validated against a live long-reasoning stream (1917 events, 0
corruption).

## DeepSeek reasoning forcing

DeepSeek requires `reasoning_content` on **every** assistant message of a
tool-calling history. When Open WebUI executes a tool call, it rebuilds the
assistant `tool_calls` message from stored output items and omits
`reasoning_content` (OpenAI-compatible providers — LiteLLM included — replay
the reasoning without that field), and the request goes straight back to the
pipe — filter inlets do **not** run on tool-call continuations.

This is the **DeepSeek API contract**, not a gateway quirk: LiteLLM itself
warns about it (`transformation.py`, "DeepSeek thinking mode"):

> assistant message is missing `reasoning_content` … A single-space placeholder
> is being injected to satisfy API validation, but the model will receive a
> blank reasoning chain for this turn, which may silently degrade multi-turn
> response quality.

The pipe forces the field on every outbound payload once tool-calling is in
scope (request `tools` or tool-call history):

- `reasoning_content` is set to `" "` (single space) when missing or empty —
  enough for DeepSeek to keep reasoning, and exactly the placeholder LiteLLM
  would inject anyway (its check treats `""` as absent and warns about it).
- Never touches user/system/tool messages and is deterministic, so the
  provider prefix cache is preserved.

### Replaying the real reasoning text (default on)

The forcing above can only inject a placeholder: Open WebUI discards the real
reasoning text when rebuilding assistant history for OpenAI-compatible
models (`get_reasoning_format` returns `None` — verified in the open-webui
source). The pipe monkey-patches `get_reasoning_format` (default:
`REPLAY_REASONING_TEXT` on) so pipe models replay the REAL text as
`reasoning_content` (scoped to pipe models, fails open, idempotent). A/B
probe against LiteLLM (`probes/litellm/03_replay_ab.py`): both modes reason
on every continuation (8/8), but with the real text the continuation
reasoning is ~19% richer and continues the previous chain instead of
re-deriving from scratch. Trade-off: depends on Open WebUI private internals
(`middleware.get_reasoning_format`); a future Open WebUI release can break or
supersede it — the pipe then degrades to placeholder forcing (rate-limited
warning).

Validated with the LiteLLM probes in `probes/litellm/` (see `01_toolcall_ab.js`
and its verdict in `probes/litellm/README.md`).

### `thinking` passthrough

`thinking` is a native DeepSeek request parameter and a **user control**: the
user chooses `{"thinking": {"type": "enabled" | "disabled"}}` and the request
propagates that choice as-is. The pipe must not touch it.

Earlier versions stripped `thinking: {"type": "disabled"}` from the outbound
payload, based on the assumption that Open WebUI injects that field on
server-side tool-call continuations. That assumption is **false** — the field
is the user's explicit choice — and the strip silently annulled the user's
option to run the model without reasoning. The strip was removed; the pipe now
forwards `thinking` unchanged.

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
- ✅ Gateway-agnostic (LiteLLM, OpenAI-compatible proxies)
- ✅ Shared connection pool across requests/tool-call iterations (no
  per-request handshake), stream read timeout (5 min safety net), and
  gateway error bodies logged (v2.17.4+)
- ❌ `ENABLE_FORWARD_USER_INFO_HEADERS` has no effect (solved via
  `GATEWAY_CUSTOM_HEADERS` templates)

## Compatibility

Validated against Open WebUI **0.11.1** + LiteLLM (`http://litellm.private`)
+ DeepSeek **v4 flash/pro** (Claude Haiku 4.5 is exposed by the gateway but
not validated with this pipe):

- LiteLLM emits standard OpenAI-compatible responses: `reasoning_content` in
  both non-stream messages and stream deltas — no field normalization
  needed (the Bifrost-specific `reasoning`/`reasoning_details` handling was
  removed in v2.17.0).
- LiteLLM warns when a tool-call continuation replays an assistant without
  `reasoning_content` (blank reasoning chain) — the pipe's forcing step
  exists for exactly that.

## File Layout

```
pipes/agent_loop_guard/
├── README.md              ← This file
├── DESIGN.md              # Full design document (reference)
├── EXAMPLE.md             # Before/after walkthrough
├── agent_loop_guard.py    # Single-file pipe
└── tests/                 # Unit tests (attached-files cleanup, reasoning forcing, replay patch)
```

The pipe is a single Python file because Open WebUI Functions are stored
as a single source blob in the database. No `__init__.py`, no package.

## License

MIT
