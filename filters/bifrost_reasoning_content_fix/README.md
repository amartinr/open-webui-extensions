# Bifrost reasoning_content fix

Open WebUI filter that converts Bifrost's non-standard `reasoning` +
`reasoning_details` response fields back to the standard OpenAI
`reasoning_content` format.

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

### outlet (provider → Open WebUI)

Applied to every response — both streaming and non-streaming:

1. **`reasoning`** → `reasoning_content` (rename to standard field).
2. **`reasoning_details`** → its text blocks are concatenated into
   `reasoning_content` (richer than the flat `reasoning` field).
3. **`reasoning_tokens`** in `usage.*_details` stripped (non-standard).
4. **Exception safety**: stream errors are logged and the raw chunk
   is passed through to avoid crashing the whole response.

### inlet (Open WebUI → provider)

Cleans historical assistant messages **only** if they still carry
non-standard Bifrost fields (`reasoning` or `reasoning_details`).
Messages that were already normalized by the outlet in a previous
turn are left untouched.

## Important caveats

- **Bifrost #974** (streaming `delta.reasoning` silently dropped for
  Gemini): this is a Bifrost-side bug; the filter cannot recover
  reasoning that never arrives. Pin a known-good Bifrost version or
  report upstream.
- **Bifrost #5169** (Chat→Responses stream converter emits reasoning
  deltas without an opening event, crashing Anthropic SDK clients):
  also a Bifrost-side bug affecting Anthropic-compat streaming.
- The filter **does not** inspect `content` for embedded XML
  reasoning tags — that is Open WebUI's own responsibility via its
  `reasoning_tags` configuration.
