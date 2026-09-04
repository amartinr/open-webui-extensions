# Payload Inspector

Debug-only Open WebUI filter that dumps the gateway request payload as pretty
JSON to the server console and posts a truncated preview to the chat. The
request body is never modified (passthrough `inlet`/`outlet`).

## Tap Point

The `tap_point` valve selects where the payload is dumped.

| Value | Hook | When it runs | Payload seen |
|---|---|---|---|
| `inlet` (default) | `inlet()` | Request arrival, before Open WebUI injects memory, file contents or tools | Raw request as received |
| `request` | `request()` | After tools/files/RAG are merged, right before payload normalization and dispatch | Final tools list; closest supported point to the wire |

`request` requires **Open WebUI ≥ 0.11.2** (the function `request` filter
phase was introduced in 0.11.2; on 0.11.1 only `inlet` is available). It also
runs once per native function-calling continuation.

Neither hook captures the literal outbound HTTP body: the payload is dumped
before Pydantic validation, model-parameter application and model-id
resolution.

## Valves (admin)

| Valve | Type | Default | Description |
|---|---|---|---|
| `priority` | `int` | `999` | Execution order; runs last in the chain. |
| `tap_point` | select | `inlet` | Tap point: `inlet` or `request` (see above). |
| `preview_chars` | `int` | `80` | Max characters of `content` shown per user/assistant/tool message. System messages are always printed in full. |

## Output

- **Console**: full payload, one JSON document per request, via the standard
  stdlib logger. Visibility follows Open WebUI's `GLOBAL_LOG_LEVEL`
  (`INFO` or `DEBUG` required).
- **Chat**: truncated preview (capped at 4000 characters) posted as a `status`
  event. The status line is rendered by the UI as a single clamped line of
  plain text, so the console output is the canonical source.

## Setup

1. Import `payload_inspector.py` in **Admin Panel → Functions**.
2. Attach the filter to the target model(s).
3. Set `tap_point` to `request` to inspect the final tools/RAG payload.
4. Ensure `GLOBAL_LOG_LEVEL` is `INFO` or `DEBUG`.

## Limitations

- Message contents that are lists (multimodal parts) are not handled; the
  filter expects string `content`.
- The `status` preview renders as a single-line, non-markdown status entry in
  the UI.
