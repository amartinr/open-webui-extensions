# DeepSeek Reasoning Filters

Two Open WebUI filter functions that control DeepSeek **thinking mode** and
**reasoning effort** (`low` / `high` / `max`), with per-model defaults managed
by the administrator.

## Components

| Filter | File | Priority | Toggleable | Purpose |
|---|---|---|---|---|
| Thinking Default Off | `deepseek_thinking_default_off.py` | 0 | No | Sets `thinking: disabled` for all DeepSeek requests. |
| Reasoning Effort Selector | `deepseek_reasoning_effort_filter.py` | 1 | Yes | Sets `thinking: enabled` and resolves `reasoning_effort` per model. |

## Background

- DeepSeek enables thinking mode by default when the `thinking` parameter is
  absent. Filter 0 disables it globally so only explicit opt-in enables it.
- Open WebUI re-applies model advanced parameters after filter inlets execute
  (`apply_model_params_to_body_openai` in `routers/openai.py` and, for
  pipe-based models, `functions.py`). Therefore `reasoning_effort` and
  `thinking` **must not** be configured in the model's advanced parameters:
  any value set there would overwrite the values injected by these filters.
- For this reason, per-model defaults are managed through Filter 1's admin
  valve `model_effort_map`, not through model configuration.

## Request Flow

```
Request
  → Filter 0 (Thinking Default Off)
      - body["thinking"] = {"type": "disabled"}
  → Filter 1 (Reasoning Effort Selector)  [only if enabled by user]
      - body["thinking"] = {"type": "enabled"}
      - body["reasoning_effort"] = <resolved effort>
  → LLM endpoint (direct, or via pipe/gateway such as agent_loop_guard → Bifrost)
```

## Filter 1: Reasoning Effort Selector

### Valves (admin)

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `1` | Execution order. Must run after Filter 0 (priority 0). |
| `model_pattern` | `str` | `deepseek` | Case-insensitive substring matched against the model name. Only matching models are modified. |
| `model_effort_map` | `str` (JSON) | `{"deepseek-v4-pro": "high", "deepseek-v4-flash": "low"}` | Maps a model ID or substring pattern to a default effort (`low` / `high` / `max`). The longest matching pattern takes precedence. |
| `default_effort` | `low` / `high` / `max` | `low` | Fallback effort for models not matched by `model_effort_map`. |

### Valves (user, per chat)

| Valve | Type | Default | Description |
|---|---|---|---|
| `reasoning_effort` | `low` / `high` / `max` | unset | Explicit per-chat override. Unset falls back to the model's default. |

Implemented as a plain `str` with a select input rather than a `Literal`
because Open WebUI persists user valves as a partial dict: an unset field
materializes as `""`, and a `Literal` would reject that value. Only an
explicit `low` / `high` / `max` selection overrides the model default.

### Effort Resolution

Resolved in the `inlet` handler, in order:

1. **User selection** — `UserValves.reasoning_effort`, if set explicitly.
2. **Model default** — longest matching pattern in `model_effort_map`.
3. **Fallback** — `default_effort` valve.

### Behavior

| Chip (Filter 1) | Model in `model_effort_map` | User selection | Payload |
|---|---|---|---|
| Off | — | — | `thinking: disabled`; no `reasoning_effort` |
| On | `high` | — | `thinking: enabled`; `reasoning_effort: high` |
| On | `high` | `max` | `thinking: enabled`; `reasoning_effort: max` |
| On | not listed | — | `thinking: enabled`; `reasoning_effort: low` (fallback) |

## Filter 0: Thinking Default Off

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `0` | Execution order. Must run before Filter 1 (priority 1). |
| `model_pattern` | `str` | `deepseek` | Case-insensitive substring matched against the model name. Only matching models are modified. |

Always-on filter. It does not manage `reasoning_effort`; with Filter 1's
injection logic and the constraint on model parameters above, `reasoning_effort`
is absent from the payload whenever the chip is off.

## Setup

1. Import both filters in **Admin Panel → Functions**.
2. Configure Filter 0 as a global filter (`is_global = true`, `is_active = true`).
3. Attach Filter 1 to the target DeepSeek models, or enable it globally.
4. Set `model_effort_map` and `default_effort` in Filter 1's admin valves.
5. Leave `reasoning_effort` and `thinking` **unset** in the model's advanced
   parameters.

### Example `model_effort_map`

```json
{
  "deepseek-v4-coding-assistant": "max",
  "deepseek-v4-pro": "high",
  "deepseek-v4-media-assistant": "high",
  "deepseek-v4-assistant": "low",
  "deepseek-v4-flash": "low",
  "agent_loop_guard": "low"
}
```

Resolution examples:

| Model ID | Matching pattern | Effort |
|---|---|---|
| `deepseek-v4-coding-assistant` | `deepseek-v4-coding-assistant` | `max` |
| `deepseek/deepseek-v4-pro` | `deepseek-v4-pro` | `high` |
| `deepseek-v4-media-assistant` | `deepseek-v4-media-assistant` | `high` |
| `deepseek-v4-assistant` | `deepseek-v4-assistant` | `low` |
| `deepseek-v4-flash` | `deepseek-v4-flash` | `low` |
| `deepseek/deepseek-v4-flash` | `deepseek-v4-flash` | `low` |
| `agent_loop_guard.deepseek/deepseek-v4-flash` | `agent_loop_guard` | `low` |
| `anthropic/claude-haiku-4-5` | no match | untouched |

## Compatibility

- **Pipe-based models** (e.g. `agent_loop_guard` → Bifrost): `reasoning_effort`
  originates in the filter inlet, is absent from model `params`, and is
  forwarded verbatim by the pipe. No downstream component can overwrite it.
  No monkey-patching required.
- **`keep_reasoning_content`**: compatible. That filter patches
  `middleware.get_reasoning_format`, which runs in `process_chat_payload`
  before dispatch; re-injected `reasoning_content` is forwarded in the body
  to the gateway. Required to satisfy DeepSeek's `reasoning_content` pass-back
  requirement during tool-call turns.
- **Open WebUI ≥ 0.9.0**: required (`UserValves` + `self.toggle` API).

## Effort Mapping (DeepSeek API)

DeepSeek V4 accepts `low`, `high` and `max`. Since the 0731 model update, all
three levels are available on `deepseek-v4-flash` and `deepseek-v4-pro`.
Verify the per-deployment mapping against the official
[DeepSeek thinking mode documentation](https://api-docs.deepseek.com/guides/thinking_mode)
when pinning older model versions.

| Requested effort | deepseek-v4-flash | deepseek-v4-pro |
|---|---|---|
| `low` | low | high* |
| `high` | high | high |
| `max` | max | max |

\* `deepseek-v4-pro` may map `low` up to `high` depending on deployment; the
API applies the mapping silently.
