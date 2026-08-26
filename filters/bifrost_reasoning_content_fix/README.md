# Bifrost reasoning_content fix

Open WebUI filter (>= 0.11, v3.0.0) that converts Bifrost's non-standard
`reasoning` + `reasoning_details` response fields back to the standard
OpenAI `reasoning_content` format.

## Rationale

Bifrost (the Maxim AI gateway) injects `reasoning` and
`reasoning_details` into the chat completion response object when
routing through certain providers (e.g., DeepSeek v4 models). These
fields are **not part of the OpenAI Chat Completion API schema** and
break clients that expect the standard `reasoning_content` field.

- Upstream issue: [maximhq/bifrost#3139](https://github.com/maximhq/bifrost/issues/3139)
- OpenAI `reasoning_content` spec: https://platform.openai.com/docs/guides/reasoning

## What Bifrost returns (non-standard)

```json
{
  "message": {
    "content": "hello",
    "reasoning": "We ask...",
    "reasoning_details": [{"type": "reasoning.text", "text": "..."}]
  },
  "usage": {
    "completion_tokens_details": {
      "reasoning_tokens": 49
    }
  }
}
```

- `reasoning` and `reasoning_details` are **not part of the OpenAI spec**.
- `reasoning_content` (the standard field) is absent.
- `usage.reasoning_tokens` is also non-standard and stripped.

## What the filter produces (standard OpenAI)

```json
{
  "message": {
    "content": "hello",
    "reasoning_content": "We ask..."
  },
  "usage": {}
}
```

## How it works

### stream (provider → Open WebUI, streaming)

Open WebUI >= 0.11 refactored the filter pipeline: it no longer lets a
filter wrap the `StreamingResponse` from the `outlet`. Instead it parses
**each SSE chunk into a dict** (`JSONCodec.loads(payload)`), passes that
dict to the filter's `stream()` method (`filter_type='stream'`), and
re-serialises the result. So the fix now runs **per event** on
`event['choices'][i]['delta']`:

1. **`delta.reasoning`** → appended to `delta.reasoning_content`.
2. **`delta.reasoning_details`** → only used as a fallback when
   `reasoning` carried no text (some providers drop `delta.reasoning`);
   its text blocks are appended to `reasoning_content`. Never discarded —
   this was the cause of the "model stops reasoning" regression.
3. **Top-level `event['usage']`** (final streaming chunk) → stripped of
   `reasoning_tokens`.
4. **Exception safety**: errors are logged and the event is passed
   through unchanged to avoid crashing the SSE stream.

### Validated empirically against a live Bifrost endpoint

Captured raw SSE from Bifrost (`deepseek/deepseek-v4-flash`, `stream: true`)
and ran the full stream through `stream()` exactly as Open WebUI >= 0.11
does (parse chunk → `stream()` → re-serialise). Findings:

- The chunk carries `choices[0].delta` plus a top-level `model` and final
  `usage`. `model` is used for the prefix gate.
- Each reasoning fragment arrives **duplicated** in both `delta.reasoning`
  and `delta.reasoning_details` with the **same incremental text**; the
  reasoner keeps `delta.reasoning` as the source of truth and drops the
  redundant `reasoning_details` to avoid double-appending.
- Result: 125 chunks processed, `reasoning_content` reconstructed without
  duplication, `content` untouched, `usage.completion_tokens_details` no
  longer contains `reasoning_tokens`, and no Bifrost residue left in any
  delta (SSE stays valid).

### outlet (provider → Open WebUI, non-streaming only)

The `outlet` now only handles **non-streaming** (dict) responses:

1. **`message.reasoning`** → `reasoning_content` (rename).
2. **`message.reasoning_details`** → text blocks concatenated into
   `reasoning_content`.
3. **`reasoning_tokens`** in `usage.*_details` stripped (non-standard).

### inlet (Open WebUI → provider)

Cleans historical assistant messages **only** if they still carry
non-standard Bifrost fields (`reasoning` or `reasoning_details`).
Messages that were already normalized by the outlet in a previous
turn are left untouched.

## Important caveats

- **Open WebUI >= 0.11 required**: the per-event `stream()` contract is a
  hard requirement. Older versions wrap `StreamingResponse` from the
  `outlet`, which the filter no longer does. Use the matching Open WebUI
  release (`required_open_webui_version: 0.11.0`).
- **Bifrost #974** (streaming `delta.reasoning` silently dropped for
  Gemini): this is a Bifrost-side bug; the filter cannot recover
  reasoning that never arrives. Pin a known-good Bifrost version or
  report upstream.
- **Bifrost #5169** (Chat→Responses stream converter emits reasoning
  deltas without an opening event, crashing Anthropic SDK clients):
  also a Bifrost-side bug affecting Anthropic-compat streaming.
- The filter **does not** inspect `content` for embedded XML
  reasoning tags - that is Open WebUI's own responsibility via its
  `reasoning_tags` configuration.
