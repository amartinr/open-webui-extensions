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

### External verification (docs + OWUI source)

Why a single space is the right value for `reasoning_content` on tool-call
continuations (multi-tool-call turns included), verified against:

1. **DeepSeek official docs** (api-docs.deepseek.com/guides/thinking_mode):
   requests carrying `tools` must pass `reasoning_content` back on EVERY
   assistant message of the history — missing field = HTTP 400. The
   validation is presence-only; no documented error for empty/whitespace
   content. The official multi-tool-call example replays the real text so
   the model can "continue its previous reasoning" — a quality goal, not a
   validation requirement.
2. **Community integrations** (spring-ai #5027, openai/codex #24500,
   openai-agents-js #791, openai-agents-python #2155, Roo-Code #10175,
   n8n, LangChain forum): all hit the same 400 "Missing reasoning_content
   field"; none report a 400 for empty/space content.
3. **Open WebUI source (master clone)**: `get_reasoning_format()` returns
   `'thinking'` only for ollama and `'reasoning_content'` only for
   llama.cpp — `None` for every OpenAI-compatible model (LiteLLM, Bifrost).
   With `None`, `convert_output_to_messages()` (called with `raw=True` on
   tool-call continuations, middleware.py ~5939) DISCARDS the real reasoning
   text; only native providers get `pending_reasoning`. So Open WebUI itself
   strips the real text before the pipe ever sees the payload — the pipe
   cannot replay it and a placeholder is the only option.
4. **Open WebUI docs**: no coverage of this detail (only the llama.cpp
   DeepSeek-R1 tutorial, which does not apply).

Consequence: forcing `" "` is byte-identical to what LiteLLM already sent
DeepSeek when the field was missing (its own injected placeholder), so it
satisfies DeepSeek's presence validation, silences LiteLLM's warning, and
costs nothing extra — the reasoning-text loss is Open WebUI's own behavior
for OpenAI-compatible providers regardless of the pipe.

### A/B probe: placeholder vs real reasoning replay (03_replay_ab.py)

8 rounds, deepseek-v4-flash, effort=high, 2-step tool task (get_date ->
get_weather). Continuation assistant replayed WITH real reasoning (what the
opt-in monkey patch does) vs single-space placeholder:

| leg | reasoned on continuation | avg reasoning len |
|---|---|---|
| A (placeholder " ") | 8/8 | 76.4 |
| B (real reasoning) | 8/8 | 91.0 |

Both always reason (placeholder does not block reasoning); with the real
text the continuation reasoning is ~19% richer and continues the previous
chain ("Today is 2026-09-01, so tomorrow is 2026-09-02...") instead of
re-deriving tersely. Correlates with the unit tests
(tests/test_reasoning_replay_ab.py) running the REAL
convert_output_to_messages from the cloned open-webui with
reasoning_format=None vs 'reasoning_content'. Decision: the pipe got an
REPLAY_REASONING_TEXT valve (now default on) that reinstalls the
get_reasoning_format monkey patch for pipe models; fails open to
placeholder forcing.
