# LiteLLM probes — separating Bifrost-specific from gateway-agnostic behavior

When `agent_loop_guard` was debugged against Bifrost 2.0.0 we could not tell
which behaviors were Bifrost's and which belonged to the DeepSeek API
contract (through any OpenAI-compatible gateway). These probes run against
the LiteLLM endpoint (http://litellm.private) to answer exactly that.

Endpoint facts (probed 2025-09-01):
- Models: deepseek/deepseek-v4-flash, deepseek/deepseek-v4-pro, anthropic/claude-haiku-4-5
- Non-stream response: message carries native `reasoning_content` (no Bifrost residue `reasoning`/`reasoning_details`)
- Stream deltas: only `reasoning_content`, `content`, `role` — standard OpenAI shape

## probes

1. `01_toolcall_ab.js` — A/B on tool-call continuations: assistant replayed
   WITH vs WITHOUT `reasoning_content` (the latter is how Open WebUI
   reconstructs assistant messages). Repeated rounds, counts drops
   (response `reasoning_content` empty or missing).

## verdict (confirmed)

- Bifrost residue renaming (`reasoning`/`reasoning_details` -> `reasoning_content`): **NOT needed** with LiteLLM (non-stream and stream both emit standard `reasoning_content`).
- Stream delta cleanup (`_clean_stream_delta`): **NOT needed** with LiteLLM (deltas already standard).
- Forcing `reasoning_content` on tool-call histories: **REQUIRED — DeepSeek contract, transport-independent.**

### The definitive evidence: LiteLLM's own warning (transformation.py)

```
DeepSeek thinking mode: assistant message is missing `reasoning_content` and none was
saved in `provider_specific_fields`. A single-space placeholder is being injected to
satisfy API validation, but the model will receive a blank reasoning chain for this
 turn, which may silently degrade multi-turn response quality. Preserve
`reasoning_content` from the original assistant response when building multi-turn
conversation history.
```

Fired whenever a tool-call continuation replays an assistant message without
`reasoning_content` — exactly the shape Open WebUI rebuilds. The A/B probe
correlates: with `high` effort the WITH-`reasoning_content` leg reasoned 2/4
rounds, the WITHOUT leg 0/4 (with `low` effort on a trivial task the model
simply often chooses not to reason at all, so the difference is washed out).

### Follow-up (live deployment trace, v2.17.0)

With the pipe forcing `reasoning_content: ""`, LiteLLM STILL emitted this
warning on every assistant of every request: its check treats an EMPTY
string as absent (`if not reasoning_content`), injects `" "` itself, and
warns. Fix: the pipe now forces `" "` (single space) — truthy for LiteLLM,
and byte-identical to the placeholder it would inject anyway. The warning
is silenced and DeepSeek receives exactly what it received before.

### What this means for `agent_loop_guard` on the revert branch

Keep:
- `_force_reasoning_content_on_tools` (rename to drop the Bifrost framing; it is a DeepSeek-contract fix).
- generic request diagnostics (stats, outbound log, verbose message summary) behind an opt-in valve.

Drop:
- `_install_reasoning_replay_patch` (OWUI `get_reasoning_format` monkey patch) + `patches/openwebui-29052-middleware.patch`.
- residue renaming (`_has_bifrost_residue`, `_normalize_reasoning_message`, `_extract_reasoning_text` on messages) — LiteLLM never emits `reasoning`/`reasoning_details`.
- `_clean_stream_delta` — stream deltas are already standard; nothing to normalize.
- the whole `bifrost_reasoning_content_fix` filter and the Bifrost integration tests.

## Open item (next session)

`pi-bifrost-reasoning-fix` (pi extension, `~/.pi/agent/extensions/`) was
DISABLED by the user after this session. It forces `reasoning_content` on
pi's own DeepSeek tool-call histories through the same LiteLLM endpoint —
same DeepSeek contract the pipe handles for Open WebUI. Revisit separately:
whether pi still needs it (its `models.json` routes deepseek-v4-flash/pro
through LiteLLM), and whether to rename it (the "bifrost" name is
misleading — it is transport-independent).
