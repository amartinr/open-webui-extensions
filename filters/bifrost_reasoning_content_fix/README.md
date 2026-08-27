# Bifrost reasoning_content fix

Open WebUI filter (>= 0.11, v3.2.0) that converts Bifrost's non-standard
`reasoning` + `reasoning_details` response fields back to the standard
OpenAI `reasoning_content` format, and keeps DeepSeek reasoning alive
across tool-calling turns.

## Scope

Bifrost (the Maxim AI gateway) injects `reasoning` and
`reasoning_details` into the chat completion response when routing
providers such as DeepSeek v4. These fields are **not part of the OpenAI
Chat Completion API schema** and are ignored by clients that expect the
standard `reasoning_content` field.

- Upstream: [maximhq/bifrost#3139](https://github.com/maximhq/bifrost/issues/3139)
- OpenAI `reasoning_content` reference: https://platform.openai.com/docs/guides/reasoning

## Behaviour

### Non-streaming (outlet)

Bifrost returns reasoning in non-standard message fields:

```json
{
  "message": {
    "content": "hello",
    "reasoning": "We ask...",
    "reasoning_details": [{"type": "reasoning.text", "text": "We ask..."}]
  },
  "usage": {
    "completion_tokens_details": {
      "reasoning_tokens": 49
    }
  }
}
```

The filter renames it to the standard OpenAI shape (`reasoning_content`):

```json
{
  "message": {
    "content": "hello",
    "reasoning_content": "We ask..."
  },
  "usage": {
    "completion_tokens_details": {
      "reasoning_tokens": 49
    }
  }
}
```

- `reasoning` and `reasoning_details` are not part of the OpenAI spec.
- `reasoning_content` is absent in Bifrost output and reconstructed here.
- `reasoning_tokens` is a non-standard usage field read by Open WebUI and
  token-usage filters; it is **kept** by default and only removed when the
  `strip_reasoning_tokens` valve is enabled.

### Streaming (stream)

Bifrost emits reasoning under proprietary `delta` fields and duplicates
each fragment across two of them. The filter normalises each event's
`delta` and, if enabled, strips non-standard `reasoning_tokens` from the
final `usage` payload.

The differences, event by event, are described in the
[SSE comparison](#sse-format-differences) section below.

## SSE format differences

An OpenAI-compatible streaming endpoint emits each chunk as a
self-contained event: message text in `delta.content`, reasoning in
`delta.reasoning_content`.

### OpenAI-compatible (expected shape)

```json
data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"reasoning_content":"Let's compute"},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"reasoning_content":" 17 * 19"},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"content":"323"},"finish_reason":"stop"},"usage":{"completion_tokens_details":{"reasoning_tokens":49}}]}
data: [DONE]
```

### Bifrost (as emitted)

```json
data: {"choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"reasoning":"Let's compute","reasoning_details":[{"type":"reasoning.text","text":"Let's compute"}]},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"reasoning":" 17 * 19","reasoning_details":[{"type":"reasoning.text","text":" 17 * 19"}]},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"content":"323"},"finish_reason":"stop"},"usage":{"completion_tokens_details":{"reasoning_tokens":49}}]}
data: [DONE]
```

Differences:

- reasoning is carried in `delta.reasoning` and `delta.reasoning_details`
  instead of `delta.reasoning_content`;
- the same incremental text is duplicated across both fields (neither is
  the standard field);
- `delta.reasoning_content` is never emitted, so an OpenAI-compatible
  client sees no reasoning;
- the final `usage` carries non-standard `reasoning_tokens`.

The filter maps the Bifrost event to the OpenAI shape:

```json
data: {"choices":[{"index":0,"delta":{"reasoning_content":"Let's compute 17 * 19"},"finish_reason":null}]}
data: {"choices":[{"index":0,"delta":{"content":"323"},"finish_reason":"stop"},"usage":{"completion_tokens_details":{"reasoning_tokens":49}}]}
data: [DONE]
```

## How it works

### stream (provider → Open WebUI, streaming)

Open WebUI >= 0.11 no longer lets a filter wrap the `StreamingResponse`
from the `outlet`. It parses **each SSE chunk into a dict**
(`JSONCodec.loads(payload)`), passes it to the filter's `stream()`
(`filter_type='stream'`), and re-serialises the result. The fix runs
**per event** on `event['choices'][i]['delta']`:

1. **`delta.reasoning`** — appended to `delta.reasoning_content`.
2. **`delta.reasoning_details`** — fallback only when `reasoning` carried
   no text (some providers drop `delta.reasoning`; see
   [maximhq/bifrost#974](https://github.com/maximhq/bifrost/issues/974)).
   Its blocks are appended to `reasoning_content`. Never discarded.
3. **Top-level `event['usage']`** (final streaming chunk) —
   `reasoning_tokens` are kept by default (Open WebUI and token-usage
   filters read them) and are removed only when the
   `strip_reasoning_tokens` valve is enabled.
4. **Exception safety** — errors are logged and the event passes through
   unchanged.

Detection is **content-driven**: only events that actually carry Bifrost
fields are rewritten, so `stream()` does not depend on `event['model']`
matching the valve prefixes. Non-Bifrost chunks are left untouched.

### outlet (provider → Open WebUI, non-streaming only)

The `outlet` handles only non-streaming (dict) responses, gated by model
id via `model_prefixes`:

1. **`message.reasoning`** — renamed to `reasoning_content`.
2. **`message.reasoning_details`** — its text blocks concatenated into
   `reasoning_content`.
3. **`reasoning_tokens`** in `usage.*_details` kept by default; removed
   only when `strip_reasoning_tokens` is enabled.

### inlet (Open WebUI → provider)

Cleans historical assistant messages **only** if they still carry
non-standard Bifrost fields (`reasoning` or `reasoning_details`).
Messages already normalised by the `outlet` in a previous turn are left
untouched.

Since v3.2.0 it also ports the fix from the
[`pi-bifrost-reasoning-fix`](https://github.com/amartinr/pi-bifrost-reasoning-fix)
pi extension: **once the history contains an assistant tool call (or the
request carries `tools`), every assistant message is forced to carry
`reasoning_content`** (empty string if none yet).

DeepSeek requires `reasoning_content` on every assistant message of a
tool-calling history, whether or not the current request still ships
`tools`. Open WebUI rebuilds assistant messages from stored `output`
items and, for OpenAI-compatible providers, omits `reasoning_content`
(it only keeps the non-standard `reasoning_details`, or nothing at all).
A missing field makes DeepSeek drop reasoning for that turn without an
error. Reasoning resumes after history compaction removes the tool call
from the context.

Validated against a live Bifrost endpoint (`deepseek/deepseek-v4-flash`):

| History replayed on the next turn | Reasoning on next turn |
|---|---|
| assistant with tool_calls, **no** `reasoning_content` | ❌ lost |
| assistant with tool_calls, `reasoning_content` (even `""`) | ✅ kept |
| assistant with tool_calls, `reasoning_details` (Bifrost dialect) | ❌ lost |
| no tool_calls in history (with or without `tools` in payload) | ✅ kept |

## Upstream Bifrost issues

The underlying problems are tracked upstream and remain open:

- **[maximhq/bifrost#5325](https://github.com/maximhq/bifrost/issues/5325)** —
  reasoning is emitted in Bifrost-specific fields (`reasoning` /
  `reasoning_details`) instead of `reasoning_content`, so a generic
  OpenAI-compatible client ignores it.
- **[maximhq/bifrost#974](https://github.com/maximhq/bifrost/issues/974)** —
  streaming `delta.reasoning` is dropped for some providers (Gemini). The
  filter cannot recover reasoning that never arrives; pin a known-good
  Bifrost version or report upstream.
- **[maximhq/bifrost#6523](https://github.com/maximhq/bifrost/issues/6523)** —
  opening role-only SSE chunks are dropped, breaking stream assembly in
  OpenAI-compatible SDKs (LangChain tool-calls/usage). Mitigated by the
  `agent_loop_guard` SSE filter (v2.6.0).
- **[maximhq/bifrost#5169](https://github.com/maximhq/bifrost/issues/5169)** —
  the Chat→Responses stream converter emits reasoning deltas without an
  opening event, crashing strict Anthropic SDK clients. Bifrost-side;
  affects Anthropic-compat streaming only.

If Bifrost implements a standard `reasoning_content` dialect
([#5325](https://github.com/maximhq/bifrost/issues/5325)) and fixes its SSE
normalization, this filter can be relaxed or removed.

## Requirements

- **Open WebUI >= 0.11** — the per-event `stream()` contract is a hard
  requirement. Older versions wrap `StreamingResponse` from the `outlet`,
  which the filter no longer does. Use the matching Open WebUI release
  (`required_open_webui_version: 0.11.0`).

## Valves

- **`model_prefixes`** — gates only the `inlet`/`outlet` cleanup (those do
  get Open WebUI's real model id). `stream()` is content-driven and applies
  to any event that actually carries Bifrost fields, regardless of model id.
  If only DeepSeek routes via Bifrost, no configuration is needed.
- **`strip_reasoning_tokens`** — default `false` (keep `reasoning_tokens`).
  Set `true` only when a strict OpenAI client rejects them.

## Notes

- The filter does **not** inspect `content` for embedded XML reasoning tag -
  that is handled by Open WebUI's own `reasoning_tags` configuration.
