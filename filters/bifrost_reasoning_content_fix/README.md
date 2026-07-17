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

- `reasoning` → `reasoning_content` (OpenAI-compatible).
- `reasoning_details` removed.
- `usage.reasoning_tokens` stripped (not part of the standard schema).

## How it works

- **inlet**: scrubs historical `reasoning` / `reasoning_details` from
  previous assistant messages before they are re-sent to the upstream API.
- **outlet**: converts `reasoning` → `reasoning_content` on the fly
  for both streaming and non-streaming responses.
