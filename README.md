# DeepSeek Reasoning Effort Selector

An Open WebUI **Filter Function** that lets users control the reasoning depth
(`high` or `max`) when chatting with DeepSeek models.

## How It Works

The filter is **toggleable** (`self.toggle = True`). A chip appears in the chat
input bar; clicking it opens a modal where the user picks the reasoning effort.

| Filter state | Behaviour |
|---|---|
| **Disabled** (chip removed) | `inlet()` is never called. The request reaches DeepSeek untouched. |
| **Enabled** (chip present) | The filter strips any pre-existing `reasoning_effort` and `extra_body.thinking` from the request body, then injects its own values. |

## Injection Logic

1. **Thinking mode** — always forced to `{"type": "enabled"}` when the filter is
   active. There is no user-facing toggle for this.
2. **Reasoning effort** — resolved in this order:
   - User's per-chat choice (`UserValves.reasoning_effort`), if set.
   - Admin's `default_effort` Valve (default: `"high"`).

## Configuration

### Admin Valves

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `0` | Execution order (lower runs first). |
| `default_effort` | `"high"` / `"max"` | `"high"` | Default reasoning depth when the user hasn't set a preference. |
| `model_pattern` | `str` | `"deepseek"` | Case-insensitive substring match against the model name. Only matching models get the injected parameters. |

### User Valves (per-chat)

| Valve | Type | Default | Description |
|---|---|---|---|
| `reasoning_effort` | `"high"` / `"max"` | `"high"` | Reasoning depth for this chat. |

## Design Notes

- **Override, don't merge.** The filter explicitly removes any
  `reasoning_effort` or `thinking` keys that Open WebUI or a previous filter
  may have already placed in the body, guaranteeing that this filter's values
  are the ones the DeepSeek API sees.
- **Toggleable.** Users enable/disable the filter via the chip in the chat
  input bar or the Integrations menu. When disabled, the filter is not invoked
  at all — no conditional branches inside `inlet()`.
- **Open WebUI 0.9.0+ required.** Uses the `UserValves` + `self.toggle` API
  introduced in 0.9.0.
