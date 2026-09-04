# Design Document: Agent Loop Guard

**Version:** 3.1  
**Based on:** `agent_loop_guard.py` (current implementation)

---

## 1. Purpose

Prevent AI agents in Open WebUI from entering infinite tool-calling loops by
analysing conversation history **on every iteration** of the middleware's
tool-call loop, replacing the offending tool result with a guard message
that instructs the agent to stop repeating or summarise.

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
in-place slice assignment that survives the middleware's shallow copy),
**skip the LLM call entirely**, and act as a **manifold proxy** with
dynamic model discovery.

---

## 3. Core Mechanism

The pipe sits between Open WebUI and the LLM gateway. On every tool-call
iteration, the middleware calls `pipe()` with the accumulated message history.
The pipe:

1. **Analyses** the current turn's tool calls using `_analyse()` — scans
   backwards from the end of messages until the last user message, collects
   real tool calls (skipping those whose results were already replaced by the
   guard), counts consecutive identical calls, and decides whether to block.
2. **Blocks** by replacing the content of the most recent tool result with a
   guard message instructing the agent to stop. Messages are then forwarded
   to the gateway so the LLM receives the instruction.
3. **Does not remove tools from the body or metadata** — relies on the guard
   message to steer the LLM. If the LLM ignores the instruction and repeats,
   the guard fires again on the next iteration (the guarded call is tracked
   via its `tool_call_id`).

### Why result replacement instead of tool removal?

| Approach | Issue |
|----------|-------|
| **Remove tools from body** | `body["tools"]` is set once per turn from the workspace model. Mutating it mid-turn would permanently deny the agent access to tools for the rest of the conversation. The guard only wants to stop the current loop, not disable tools forever. |
| **Replace tool result** | The LLM sees a clear instruction in the tool result field and can choose to change behaviour. The tool list remains intact for legitimate future use. |

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
| **State is self-contained** | Every `pipe()` call recalculates state fresh from `_analyse()` — no cross-iteration memory needed. |

The current design replaces the last tool result's `content` in-place, which
**does** survive the middleware's shallow copy (because `messages` is a new
list but each message dict inside is the same object).

---

## 5. Architecture Overview

### 5.1 Manifold: One Pipe, Many Protected Models

The pipe uses Open WebUI's manifold pattern. A single `pipes()` method
queries the gateway for available models and creates one protected sub-pipe
per model. **Nothing is hardcoded.**

```
Pipe: "Agent Loop Guard" (manifold)

pipes() → GET {gateway}/models

Returns:
  🔧 deepseek/deepseek-v4-flash
  🔧 deepseek/deepseek-v4-pro
  🔧 anthropic/claude-haiku-4-5
  ... (whatever the gateway returns)
```

### 5.2 Runtime Flow

```
User selects "🔧 DeepSeek v4 Flash"
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
|  2. Analyse messages via _analyse()      |
|  3. If loop or runaway: replace last     |
|     tool result with guard message       |
|  4. Emit UI notification + status pill   |
|  5. Apply tool blocklist                 |
|  6. Forward to gateway with real model   |
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
    # self.valves is overwritten by Open WebUI with the stored admin
    # configuration on every request; per-user overrides arrive in
    # __user__["valves"] (no _admin_valves twin is kept).
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
        {"id": m["id"], "name": f"🔧 {m.get('name', m['id'])}"}
        for m in data.get("data", [])
    ]
    return self._models_cache
```

---

## 6. Middleware Integration

The pipe relies on one key property of Open WebUI's `streaming_chat_response_handler`
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

### 6.2 `body["messages"]` is ephemeral for appending, but message dicts are shared

`body["messages"]` is a **new list** on every iteration. However, each message
dict inside is the **same object** as the previous iteration. This means
**in-place mutation of a message's `content` field** (what the guard does)
**does** survive to the next iteration — the same dict is referenced by the
new list.

---

## 7. Tool-Call Analysis

### 7.1 `_analyse()` — Single-pass analysis

```python
def _analyse(self, body: dict, user_valves=None, user_id=None) -> tuple[bool, str | None, str, int, int]:
```

Returns `(should_block, tool_to_blame, block_kind, total, max_calls)`.

**Algorithm:**

1. **Determine limits** — resolve user vs admin valve values using
   `_resolve_limit()`. The user values come from the per-user override in
   `__user__["valves"]` (extracted by `_extract_user_valves()`; None when
   absent); the admin values from `self.valves` (the function's stored
   admin config). User value wins if > 0, otherwise the admin value.
2. **Constraint watchdog** — per-user overrides are not pre-validated, so
   the resolved pair can violate `runaway > loop`. On a request with tool
   traffic (tool history or `body["tools"]`), if the runaway is enabled and
   `loop >= runaway`, log a rate-limited warning (once per 5 min per user
   slot) and continue — with `loop >= runaway` the loop guard can never
   fire: `consecutive <= total`, so the total always reaches the runaway
   cap before the identical-call count reaches the loop threshold.
3. **Identify guarded results** — scan messages backwards from the end,
   stopping at the last user message. Collect `tool_call_id` values of any
   tool result whose `content` contains `GUARD_MARKER`. These calls were
   already handled by the guard and should be excluded from the consecutive
   count.
4. **Collect real tool calls** — scan backwards again, collecting every
   assistant `tool_call` whose `id` is NOT in the guarded set. Parse
   `function.arguments` as JSON. Reverse the list so it's in chronological
   order.
5. **Count consecutive identical calls** — from the end of the history,
   count how many consecutive calls share the same `name` **AND** `args`
   (both must match). If at least 2, record the `bad_tool` name.
6. **Decide**:
   - **Loop**: if `consecutive >= MAX_CONSECUTIVE_TOOL_CALLS > 0` and
     `bad_tool` is set → block with `kind="loop"`.
   - **Runaway**: if `total > MAX_TOOL_CALLS_PER_TURN > 0` (and no loop
     was detected) → block with `kind="runaway"`. The budget itself is
     allowed — calls 1..max_calls return their real results and the model
     may finish cleanly; the guard fires on the first call BEYOND the
     limit, replacing that extra call's result.
   - Otherwise → no block (`should_block=False`).

### Why both name AND args must match?

Two calls to the same tool with **different arguments** are considered
different actions, not a loop. Only identical name + identical args
indicates the agent is stuck repeating itself.

---

## 8. Guard States

The guard has exactly **two** blocking states:

| State | Condition | What the LLM sees |
|:-----:|:---------:|-------------------|
| **Loop** | Consecutive identical calls >= `MAX_CONSECUTIVE_TOOL_CALLS` | `"[Tool call budget exhausted] - loop detected\n{tool}: {total} identical calls exceed the limit.\nStop repeating. Try a different tool or summarise what you have."` |
| **Runaway** | Total tool calls in turn >= `MAX_TOOL_CALLS_PER_TURN` (no loop detected) | `"[Tool call budget exhausted] - turn limit reached\nYou've used all {max_calls} allowed calls this turn (attempted {total}).\nNo more tools available. Summarise what you have."` |

**Priority:** Loop wins over runaway. If both conditions are met, the
guard fires as a loop block (more specific — names the offending tool).

There is **no escalation ladder** (no intermediate WARNING or FINAL WARNING
states). The guard fires directly at the configured threshold.

---

## 9. Guard Mechanism

When `_analyse()` returns `should_block=True`:

1. **Replace tool result** — iterate `messages` in reverse, find the last
   message with `role: "tool"`, and set its `content` to the guard message
   text. This mutates the message dict **in-place**, so the change survives
   the middleware's new message list on the next iteration.
2. **Emit UI notification** — via `__event_emitter__`:
   - Loop: `{"type": "notification", "data": {"type": "error", "content": MSG_NOTIFY_LOOP}}`
   - Runaway: `{"type": "notification", "data": {"type": "error", "content": MSG_NOTIFY_RUNAWAY}}`
3. **Emit status pill** — always shows remaining tool calls when `total > 0`
   and `max_calls > 0`:
   `{"type": "status", "data": {"description": "🔧 Remaining tool calls: {remaining}/{max_calls}", "done": True, "hidden": False}}`
4. **Forward to gateway** — the modified `messages` list (with the guard
   text as the last tool result) is packed into the payload and sent to
   `{GATEWAY_BASE_URL}/chat/completions`. The tool blocklist is also applied
   before forwarding.

### What the LLM receives

```
User: "Search for cats"
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "10 results about cats..."
Assistant: [tool_call: search("cats")]
Tool result: "[Tool call budget exhausted] - loop detected
search: 4 identical calls exceed the limit.
Stop repeating. Try a different tool or summarise what you have."
```

The LLM sees the guard message as the latest tool result. If it calls
`search("cats")` again on the next iteration, `_analyse()` will skip the
guarded call (tracked via `guarded_ids`) but count the new one. The guard
can fire again if the LLM persists.

---

## 10. Tool Blocklist (TOOL_BLOCKLIST)

The `TOOL_BLOCKLIST` valve lets administrators permanently remove tools by
name before forwarding. It runs after the guard analysis but before the
gateway call.

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

## 11. Guard Message Templates

```python
GUARD_MARKER = "[Tool call budget exhausted]"

MSG_TOOL_LOOP = (
    "{marker} - loop detected\n"
    "{tool}: {total} identical calls exceed the limit.\n"
    "Stop repeating. Try a different tool or summarise what you have."
)

MSG_TOOL_RUNAWAY = (
    "{marker} - turn limit reached\n"
    "You've used all {max_calls} allowed calls this turn (attempted {total}).\n"
    "No more tools available. Summarise what you have."
)

MSG_NOTIFY_LOOP = "\U0001f527 {tool} budget exhausted after too many identical calls."
MSG_NOTIFY_RUNAWAY = "\U0001f527 Tool call budget exhausted ({total}/{max_calls})."
MSG_COUNTER = "\U0001f527 Remaining tool calls: {remaining}/{max_calls}"
```

The `_build_guard_message(status, tool, total, max_calls)` function selects
the appropriate template and formats it via `str.format()`.

---

## 12. UI Events

| Event | Trigger | Type | Content |
|:-----:|:-------:|:----:|---------|
| Notification | Loop detected | `error` | `"🔧 {tool} budget exhausted after too many identical calls."` |
| Notification | Runaway detected | `error` | `"🔧 Tool call budget exhausted ({total}/{max_calls})."` |
| Status pill | Always (if total > 0 and max_calls > 0) | `status` | `"🔧 Remaining tool calls: {remaining}/{max_calls}"` |

The status pill fires on **every iteration** where there are tool calls,
regardless of whether the guard blocked anything. It shows a descending
counter so the user knows how many tool calls remain.

---

## 13. Valves

> **Admin valves** are configured in the Function admin panel. On every
> request Open WebUI overwrites `self.valves` with the stored admin
> configuration (`open_webui/functions.py`, `get_function_module_by_id`).
> **User valves** (`UserValves`) are configured per user; Open WebUI
> delivers them in `__user__["valves"]` on every request.
> A user valve value of `0` means "use admin default".

### Admin valves (Pipe.Valves)

| Valve | Default | Description |
|-------|---------|-------------|
| `GATEWAY_BASE_URL` | `""` | Base URL for the OpenAI-compatible gateway |
| `GATEWAY_AUTH_HEADER` | `"Authorization"` | HTTP header name for the API key |
| `GATEWAY_AUTH_VALUE` | `""` | Credential value (password field) |
| `GATEWAY_CUSTOM_HEADERS` | `""` | JSON object of extra headers with template variable support |
| `MAX_TOOL_CALLS_PER_TURN` | `15` | Max tool calls allowed per turn before the runaway guard fires on the next call (the call beyond the limit is blocked). `0` = disabled |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `4` | Consecutive identical calls before loop guard fires (min 3) |
| `TOOL_BLOCKLIST` | `""` | Comma/newline-separated tool names to remove |
| `ATTACHED_FILES_CLEANUP` | `True` | Collapse and deduplicate `<attached_files>` blocks **within each user message** (per-turn: the core's block and the image_filter's current-turn block for the same upload merge to one tag; re-uploads in later turns keep their own block). Cache-safe: historical messages stay byte-stable between turns. `False` = forward payloads unchanged |

**Validation:** `MAX_TOOL_CALLS_PER_TURN` must be **greater than**
`MAX_CONSECUTIVE_TOOL_CALLS` when both are enabled. Enforced by Pydantic's
`@model_validator`. If runaway is ≤ loop, the configuration is rejected with
a clear error message.

### User valves (Pipe.UserValves)

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_TOOL_CALLS_PER_TURN` | `0` | Per-user override of the runaway limit. `0` = use the admin value. |
| `MAX_CONSECUTIVE_TOOL_CALLS` | `0` | Per-user override of the loop threshold. `0` = use the admin value. |

Effective limit = user override when non-zero, otherwise the admin value.
An admin `MAX_TOOL_CALLS_PER_TURN` of `0` disables the runaway guard; a
non-zero per-user override re-enables it for that user only.

**Constraint watchdog.** Per-user overrides are not pre-validated, so the
effective pair (admin + user mixed per field) can violate the `runaway >
loop` rule at runtime. On a request with tool activity the pipe logs a
**rate-limited warning** (once per 5 minutes per user slot, with the user
id and the effective numbers) and continues: with `loop >= runaway` the
loop threshold is effectively unreachable (the runaway cap fires first; a
loop can only trip on an all-identical history at the boundary), so blocks
are reported as `runaway`. The watchdog is deliberately a warning, not
an error — the request keeps working while the admin fixes the
configuration.

---

## 14. Custom Headers with Templates

The `GATEWAY_CUSTOM_HEADERS` valve accepts a JSON object of extra HTTP
headers sent with every gateway request. Supports template variables that
are resolved at runtime:

```json
{
  "x-tenant-id": "myhost",
  "x-authenticated-user": "{{USER_NAME}}",
  "x-user-id": "{{USER_ID}}",
  "x-user-email": "{{USER_EMAIL}}",
  "x-chat-id": "{{CHAT_ID}}"
}
```

Supported variables: `{{USER_NAME}}`, `{{USER_ID}}`, `{{USER_EMAIL}}`,
`{{USER_ROLE}}`, `{{CHAT_ID}}`, `{{MESSAGE_ID}}`.

Unlike Open WebUI's global `ENABLE_FORWARD_USER_INFO_HEADERS` (which only
works for native OpenAI/Ollama routing), this works inside the pipe for any
gateway destination.

---

## 15. Token Efficiency

| Scenario | Extra tokens | Notes |
|----------|:------------:|-------|
| Normal operation (no guard) | 0 | Pipe forwards body transparently. No modification. |
| Guard fires (loop or runaway) | ~80-100 | Guard message (~80-100 chars) replaces a potentially much longer tool result. LLM then produces a summary response. |

---

## 16. Edge Cases

| Case | Handling |
|------|----------|
| No tool calls in current turn | Forward unchanged. `_analyse()` finds empty history, no block. |
| Gateway unreachable during `pipes()` | Returns cached model list. Selector still works. |
| Gateway unreachable during `pipe()` | Exception caught → descriptive error string returned. |
| Both loop AND runaway simultaneously | Loop wins — `kind="loop"`, agent sees a tool-specific message rather than a generic limit message. |
| LLM ignores guard and repeats same call | Guard fires again on the next iteration. The guarded call's ID is tracked, so the consecutive count resets for the new batch. The LLM accumulates guard messages. |
| LLM switches to a different tool after guard | Different tool → different name → no consecutive match → no loop. Runaway may still fire if total calls ≥ limit. |
| `MAX_CONSECUTIVE_TOOL_CALLS = 3` | Loop fires at 3 consecutive identical calls. |
| `MAX_TOOL_CALLS_PER_TURN ≤ MAX_CONSECUTIVE_TOOL_CALLS` | Error at config time — Pydantic `@model_validator` rejects the configuration. |
| `TOOL_BLOCKLIST` contains unknown names | Logged as warnings; only matching tools are blocked. |
| `tool_choice` targets a blocked tool | Reset via `body.pop("tool_choice", None)` so the LLM can choose freely. |
| Workspace model has no system prompt | Open WebUI skips system prompt injection. Pipe unaffected. |
| No `__event_emitter__` provided | Guard still fires (tool result replaced). Notifications and status pill are skipped silently. |
| Tool result is not a string (e.g. list/dict) | Guard checks `isinstance(content, str)` before matching `GUARD_MARKER`. Non-string contents are not guarded, but the replacement sets `content` to a string. |

---

## 17. Risk Analysis

### Mitigated risks

| # | Risk | Mitigation |
|:-:|------|------------|
| R1 | Guard message ignored by LLM | Guard tracks `tool_call_id` of guarded results. If LLM repeats, guard fires again. The tracked call is excluded from the consecutive count, so the guard can fire repeatedly on fresh identical calls. |
| R2 | Guard fires on legitimate repeated calls | Guard cannot distinguish intent. Two genuinely identical calls will trigger loop detection at the configured threshold. Administrators should set thresholds high enough for legitimate use cases (default 4). |
| R3 | `tool_choice: "none"` causes raw DSML leakage | Pipe forwards transparently; the gateway handles DSML parsing. |
| R4 | DSML buffer corrupts `reasoning_content` | No buffering in `_stream()` — transparent SSE proxy. |
| R5 | Guard text appears as tool result content | By design — the guard message is a legitimate tool result. The LLM is instructed via the message text to stop and summarise. |

---

## 18. Attached-Files Cleanup (v2.2.0 → v2.4.0)

Since v2.2.0 the pipe also cleans up `<attached_files>` blocks (see
`filters/image_filter/DESIGN.md` → "Attached-Files Accumulation" for the
problem statement). The cleanup runs after the guard analysis and blocklist,
just before the payload is forwarded to the gateway, and is **fail-open**:
any error is logged and the payload is forwarded unchanged.

### Why the pipe, and why cache-safe

Two independent sources inject `<attached_files>` blocks into the payload:

1. The **image_filter inlet** prepends ONE block to the **last** user
   message — the **current turn's** images only (since filter v2.12.0;
   before that it was the union of all conversation images, re-announced
   every turn).
2. The core's **`add_file_context()`** (runs after filters, native function
   calling only) prepends one block **per stored user message** that has
   files — that message's own files, relative URLs.

So the payload can carry several per-message core blocks plus the
filter's current-turn block, with the same file tagged in more than one
place (a `+` upload is in its own message's core block and again in the
current-turn filter block when re-attached; a pasted image that the core
skips is only in the filter's block). That duplication — *within a
message* — is what the pipe collapses: the core's block and the filter's
block of the same message merge into one tag. Files are **never**
deduplicated across messages, so a deliberate re-upload keeps its own
block in its turn (v2.4.0).

### Semantics (deterministic, cache-safe)

- **Dedup is scoped PER USER MESSAGE** (one message = one turn), never
  across messages. Within a message, each file is tagged exactly once.
  Dedup keys, in order: (1) **UUID** — the same file collapses across the
  filter's absolute form, the core's relative form, and this deployment's
  bare-UUID raw form (`<file type="file" url="{uuid}" .../>`); (2)
  **content hash** (v2.3.0) — two *different* UUIDs whose files share
  `meta["file_hash"]` collapse too (first occurrence wins). External URLs
  key by the full URL; placeholders are never deduplicated.
- **Re-uploads are never deduplicated away.** A file deliberately
  uploaded again in a later turn gets a **new block in that turn** and
  stays visible to the agent — the pipe must not make a re-upload
  invisible. (The cross-message dedup of v2.2.0/v2.3.0 existed for the
  pre-v2.12.0 filter, which re-announced a moving union block every turn;
  the current filter never touches historical messages, so cross-message
  dedup had no remaining legitimate work — its only effect was hiding
  deliberate re-uploads, removed in **v2.4.0**.)
- Multiple blocks in one message **collapse into one** (core's exact
  format), preserving attribute order.
- Historical per-message blocks stay **byte-stable between turns** (pure
  function of each message + the stored history): the cleanup is a
  deterministic pure function per message, so the cached prefix extends
  through the whole conversation — the same depth as without the filter.
  A re-upload turn only appends to the conversation; the shared history
  renders identically in every later turn.
- The last user message keeps **its own files** (new or re-uploaded),
  collapsed to one block; historical images remain visible via their own
  message's block.
- Tags are re-emitted in **our canonical format** — `type="image"` for
  images, `id="{uuid}"`, and an **absolute**
  `/api/v1/files/{uuid}/content` URL (`webui.url` via `Config`, falling
  back to `__request__.base_url`) — regardless of which source produced
  them, so the same file always renders identically and both `view_file`
  (uses `id`) and ComfyUI (URL) keep working. Placeholder tags
  (`(base64 stripped)`) are preserved as-is.
- **Only image tags participate in the cleanup.** Non-image tags (PDFs,
  documents) are never deduplicated nor rewritten to our format — they
  keep their original attributes (`type`, `id`, `name`, `content_type`)
  and only their relative URL becomes absolute inside the re-emitted
  block, which is harmless and useful. The image_filter already lets
  non-image files pass through to RAG; the pipe must not change how they
  are presented to the model.
- Only **user** messages are touched; system/assistant/tool messages and
  non-text content parts pass through untouched.

### Interaction with the tool-call loop

The cleanup mutates message dicts **in place** (same objects that survive
the middleware's shallow copies, exactly like the guard does). It is
**idempotent**: re-running it on an already-cleaned list is a no-op, so
tool-call iterations re-submitting the shared history stay consistent.

### Valve

`ATTACHED_FILES_CLEANUP` (admin valve, default `True`) turns the cleanup
on/off. When off, payloads are forwarded exactly as Open WebUI built them.

### Edge cases

| Case | Handling |
|------|----------|
| Same file re-attached in a later turn | Keeps a **new block in its own turn** (per-message dedup only) — the re-upload stays visible to the agent |
| Same file tagged twice in ONE turn (core + filter) | Collapsed to one tag (UUID or content-hash match within the message) |
| Blocks in `str` content vs list content | Both parsed; blocks may be concatenated (core+filter) or merged into the first text part |
| `<attached_files>` block without `<file>` tags | Ignored — user text that literally contains the tag is left untouched |
| Message content empty after stripping | Empty text part inserted (`[{"type": "text", "text": ""}]`) to avoid 400s on strict providers |
| `base_url` unavailable | Relative URLs kept as-is (dedup still applies) |
| Any parsing/dedup error | Logged; payload forwarded unchanged (fail-open) |

### Change log (v2.2.0 → current)

- **v2.2.0** — initial cache-safe cleanup: collapse + dedup across user
  messages, each file tagged once in the earliest message.
- **v2.4.0** — **dedup scoped per user message (per turn)**. The
  cross-message dedup (each file tagged once in the earliest message) was
  only needed for the pre-v2.12.0 filter, which re-announced a moving
  union block every turn; the current filter only announces the current
  turn, so cross-message dedup's only remaining effect was hiding
  deliberate re-uploads — a re-uploaded file was invisible to the agent
  (while the `+` upload still persisted a duplicate on disk: the worst of
  both worlds). Now each user message keeps its own files, collapsed to
  one block (filter + core tags of the same message still merge by UUID
  and content hash, so the "two images in one turn" fix is unchanged),
  and historical messages stay byte-stable between turns (cache
  preserved). See `EXAMPLE.md` for the before/after.
- **v2.2.x (post-2.2.0 fixes)** — dedup simplified to a **UUID match** and
  tags re-emitted in **our canonical format** (`id` + absolute
  `/api/v1/files/{id}/content` URL) regardless of source, so the same
  image renders identically and both `view_file` (id) and ComfyUI (URL)
  keep working; cleanup restricted to **image tags only** (non-image
  tags untouched); **info-level logs** added for dropped duplicates,
  per-message and per-request summaries.
- **v2.3.0** — **content-hash backstop**: before the cleanup, the pipe
  resolves every image tag's UUID to its stored sha256
  (`meta["file_hash"]`, one indexed `Files.get_file_by_id` per unique
  UUID, fail-open) and also marks `hash:{digest}` in the seen set. Two
  **different** UUIDs with identical bytes now collapse (first occurrence
  wins, same as the UUID rule) — the guaranteed fix for the "model sees
  two images for one `+` upload" incident even if the filter's this-turn
  reuse fails (hash metadata missing, re-encoded copy). Non-image tags
  never participate; a resolution failure degrades to UUID-only dedup.
- **Filter interplay** — the image_filter must be at **v2.12.3** for
  deterministic convergence on `+` uploads. v2.12.1 made a single `+`
  upload produce a single UUID via `body["files"]`; v2.12.2 extended the
  reuse to the **stored current message** (native FC path — the
  middleware pops `files` off the payload message before filter inlets);
  v2.12.3 made the fallback deterministic (newest file with the digest
  wins) and added diagnostics. See
  `filters/image_filter/DESIGN.md` → "Content-Hash Deduplication". The
  pipe's v2.3.0 content-hash dedup remains as the safety net.

**⚠️ TO VERIFY (filter + pipe interplay)**: with the filter at v2.11 the
model sees **two** images on the first `+` upload turn (duplicate UUID),
then one. **RESOLVED (2026-08-01, v2.12.2 + v2.3.0):** runtime logs from
a single `+`-upload turn confirmed the mechanism — the filter reused an
**older identical copy** (`79cb1456...`) via the user-wide lookup while
`add_file_context()` tagged the current upload (`76680237...`);
`turn_hash_to_id` was empty because native FC never populates
`body["files"]` (the middleware pops `files` off the payload message
before filter inlets). Fixed in the filter (v2.12.2: seed from the
stored current message's `files`) and backstopped in the pipe (v2.3.0:
content-hash dedup). **Confirmed end-to-end (2026-08-01, 22:21, v2.12.3
+ pipe):** the seeding fires — `_current_turn_file_refs ... -> 1 stored
file ref(s)` + `reused this-turn upload 7f1d4ae9... (content hash match)`
— the filter's tag and the core's tag share one UUID, and the pipe logs
`dropping duplicate tag id:7f1d4ae9...` + `kept 1 (1 dropped as
duplicates)`. Model sees one image; the tool-iteration turn keeps the
single absolute-URL tag.

---

## 19. System-Prompt Budget Templating

### 19.1 Problem

The model discovers the tool-call budget only **reactively**: it calls tools
until the guard fires with `[Tool call budget exhausted]`. The guard message
is intentionally transient (context drift erases it — the model should not
be permanently limited by one past loop), but the *standing* budget should be
declarative: the model should know the limits up front and self-regulate
before exhausting them.

### 19.2 Why tokens survive to the pipe

Open WebUI resolves only its own variable families in a model's system
prompt (`resolve_system_prompt()` in `backend/open_webui/utils/payload.py`):

- `render_chat_variables` — regex-scoped to `{{chat.variables.X}}`;
- `render_user_variables` — regex-scoped to `{{user.variables.X}}`;
- `prompt_variables_template` — replaces only the exact pairs supplied in
  `metadata["variables"]`;
- `prompt_template` — exact `.replace()` of `{{CURRENT_DATE}}`,
  `{{USER_NAME}}`, `{{USER_GROUPS}}`, … plus the `{{prompt}}` family.

None is a catch-all, so any other `{{...}}` token survives byte-identical
into `body["messages"]` as a `role:"system"` message with string content.
`apply_system_prompt_to_body` runs again on every tool-call iteration
(messages are rebuilt per request), so the token reappears every time and
the pipe substitution must be idempotent and cheap.

### 19.3 Design

- Admin writes `{{MAX_TOOL_CALLS_PER_TURN}}` / `{{MAX_CONSECUTIVE_TOOL_CALLS}}`
  in the model's system prompt.
- `Pipe._effective_limits(user_valves)` resolves the effective pair — the
  SAME single source of truth `_analyse()` uses for the guard thresholds —
  so prompt numbers and enforcement can never disagree. Per-user overrides
  (`Pipe.UserValves`, `> 0` wins, `0` defers) apply per request.
- Module-level `_resolve_budget_tokens_in_system_prompt(messages, max_calls,
  max_consecutive)` replaces the tokens in `role:"system"` string messages
  only (multimodal list content skipped defensively). Effective limit `0`
  (runaway disabled) renders as `"unlimited"` — never tell the model to make
  zero calls.
- `pipe()` runs it after `_analyse()` and the reasoning forcing, right
  before `payload = {**body, ...}`, in a fail-open try/except; `DEBUG_LOG`
  reports `replaced=N (max_calls=…, max_consecutive=…)`.

### 19.4 Properties

- **Deterministic per valve state** → between requests with unchanged
  valves the outgoing system prompt is byte-identical; provider prefix
  cache is not churned. Only an admin/per-user valve change alters the
  output — exactly when the model should see a new budget.
- **Backwards compatible**: no token → no-op, payload untouched.
- **Fail-open**: errors log `budget templating failed (fail-open)` and the
  payload is forwarded unchanged.
- **Scope**: system messages only — user/assistant/tool messages are never
  modified (no exposure of the budget to user content).
