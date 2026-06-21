# DeepSeek Reasoning — Two-Filter Architecture

Two **Filter Functions** for Open WebUI that together give users control over
DeepSeek's thinking mode and reasoning depth.

## The Problem

DeepSeek **defaults thinking mode to ON** if no `thinking` parameter is sent.
This means models think by default on every request — slower, more expensive,
and often unnecessary for simple queries.  Until now there was no way to make
"thinking off" the default while still letting users opt in per chat.

## The Solution: Two Filters

| Filter | File | Priority | Toggleable | Behaviour |
|---|---|---|---|---|
| **Thinking Default Off** | `deepseek_thinking_default_off.py` | `0` (first) | **No** (always-on) | Sets `thinking: disabled` for DeepSeek models. Runs globally on every request. |
| **Reasoning Effort Selector** | `deepseek_reasoning_effort_filter.py` | `1` (second) | **Yes** (chip) | Overrides to `thinking: enabled` + user-chosen `reasoning_effort`. Only runs when the user enables it. |

### Flow

```
Request → Filter 0 (Thinking Default Off)
            → strips any pre-existing thinking from extra_body
            → sets extra_body.thinking = {"type": "disabled"}
        → Filter 1 (Reasoning Effort Selector) → ONLY if chip active:
            → strips any pre-existing reasoning_effort from top level
            → strips any pre-existing thinking from extra_body
            → sets reasoning_effort at top level
            → sets extra_body.thinking = {"type": "enabled"}
        → LLM API
```

| User action | Filter 0 runs? | Filter 1 runs? | Result |
|---|---|---|---|
| No chip activated | ✅ Always | ❌ No | Thinking OFF (fast, cheap) |
| Chip activated, effort = "high" | ✅ Always | ✅ Yes | Thinking ON + effort HIGH |
| Chip activated, effort = "max" | ✅ Always | ✅ Yes | Thinking ON + effort MAX |

---

## Filter 1: DeepSeek Reasoning Effort Selector

A **toggleable** filter that lets users control the reasoning depth
(`high` or `max`) when chatting with DeepSeek models. A chip appears in the
chat input bar; clicking it opens a modal where the user picks the reasoning
effort.

### Injection Logic

1. **Strips `reasoning_effort`** from top level (removes any workspace default).
2. **Strips `thinking`** from `extra_body` (removes whatever Filter 0 injected).
3. **Injects `thinking`** inside `extra_body` — DeepSeek expects it there when
   using the OpenAI-compatible format (the OpenAI Python SDK passes non-standard
   params via `extra_body`).
4. **Injects `reasoning_effort`** at top level — it's a standard OpenAI Chat
   Completions parameter.
5. **Effort** resolved in this order:
   - User's per-chat choice (`UserValves.reasoning_effort`), if set.
   - Admin's `default_effort` Valve (default: `"high"`).

### Admin Valves

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `1` | Execution order. Should run **after** Filter 0 (priority 0). |
| `default_effort` | `"high"` / `"max"` | `"high"` | Default reasoning depth when the user hasn't set a preference. |
| `model_pattern` | `str` | `"deepseek"` | Case-insensitive substring match against the model name. Only matching models get the injected parameters. |

### User Valves (per-chat)

| Valve | Type | Default | Description |
|---|---|---|---|
| `reasoning_effort` | `"high"` / `"max"` | `"high"` | Reasoning depth for this chat. |

---

## Filter 0: DeepSeek Thinking Default Off

An **always-on** (not toggleable) global filter that sets `thinking: disabled`
for DeepSeek models on every request, preventing unwanted default thinking.

### Admin Valves

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `0` | Execution order. Should run **before** Filter 1 (priority 1). |
| `model_pattern` | `str` | `"deepseek"` | Case-insensitive substring match against the model name. |

### No User Valves

Since this filter is not toggleable and has no user-configurable options, it
defines no `UserValves` class. It simply does its job silently on every request.

---

## Setup Instructions

1. **Import both filters** in Open WebUI Admin Panel → Functions.
2. Set Filter 0 (**Thinking Default Off**) as a **Global** filter
   (`is_global = True`, `is_active = True`) so it applies to all models.
3. Attach Filter 1 (**Reasoning Effort Selector**) to the specific DeepSeek
   model(s) you want, or make it global too (users will see the chip only
   when they select a model it's attached to).
4. **Do NOT set** `reasoning_effort`, `thinking`, or `extra_body` in the
   model's workspace advanced options — the filters handle these.

## Design Notes

- **Override, don't merge.** Both filters strip pre-existing values before
  injecting their own. Filter 1 removes `thinking` from `extra_body` that
  Filter 0 may have set, then injects its own values.
- **Toggleable reasoning.** Filter 1 is the only user-facing control.
  Enabling it overrides Filter 0's default-off and enables thinking + effort.
- **`thinking` goes in `extra_body`**, not at top level, because the DeepSeek
  API follows the OpenAI SDK convention where non-standard parameters are
  passed via `extra_body`. See [DeepSeek reasoning docs](https://api-docs.deepseek.com/guides/reasoning).
- **`reasoning_effort` goes at top level** — it is a standard Chat Completions
  parameter in the OpenAI API spec.
- **Open WebUI 0.9.0+ required.** Uses `UserValves` + `self.toggle` API
  introduced in 0.9.0.
