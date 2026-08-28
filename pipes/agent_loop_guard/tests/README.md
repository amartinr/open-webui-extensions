# agent_loop_guard — tests

Test suite of the `agent_loop_guard` pipe (Open WebUI function). Part of the
Bifrost/DeepSeek reasoning-loss investigation — read `HANDOFF.md` at the repo
root for the full context (root cause, instrumentation, how to read the logs).

Each test lives with its component (monorepo convention): the pipe's tests
stay in this directory; the filter (`bifrost_reasoning_content_fix`) and the
tools (`smart_fetch_url`, …) keep their own test directories.

## Inventory

| File | Kind | What it tests | Needs a live… |
|---|---|---|---|
| `test_bifrost_reasoning_normalization.py` | unit (pytest) | Outbound payload rewrite: `reasoning`/`reasoning_details` → `reasoning_content`, forcing `reasoning_content` once tool-calling is in scope | nothing (module-only) |
| `test_attached_files_cleanup.py` | unit (pytest) | Cache-safe `<attached_files>` cleanup: deterministic, idempotent, byte-stable history prefix | nothing (module-only) |
| `repro_bifrost_reasoning_loss.mjs` | integration (node) | Direct A/B against the Bifrost gateway: same payload in stream vs non-stream mode to pinpoint where reasoning disappears | Bifrost (`http://bifrost.private/v1`) |
| `sim_tool_call_owui.mjs` | integration (node) | Faithful tool-call round-trip through Open WebUI's OpenAI-compatible API (OWUI → pipe → Bifrost → DeepSeek), hunting the drop signature on continuation turns | Open WebUI + Bifrost |

## Unit tests (pytest, no network)

```bash
cd /srv/pi/open-webui-extensions
python3 -m pytest pipes/agent_loop_guard/tests/ -q
# whole repo battery:
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

Both modules import `agent_loop_guard` via a `sys.path` insert relative to the
test file — no Open WebUI install required (its imports are lazy inside the
pipe class only).

## Integration probes (node, need live services)

### `repro_bifrost_reasoning_loss.mjs` — gateway-side A/B

Sends the SAME payload twice per round (non-stream + stream) against
`/v1/chat/completions` and compares reasoning presence. Tells the two failure
modes apart:

- stream lost reasoning but non-stream has it → SSE loss inside Bifrost
  (the confirmed root cause, cf. issue #6523)
- both modes lack reasoning → request-side issue (replay shape / DeepSeek)

```bash
node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs [rounds] [mode]
#   mode: roundtrip (default, tool-call continuation) | plain | tools
#         sse  — streaming-only battery (no non-stream leg); payload shape
#         passed as a 4th arg:  ... <rounds> sse roundtrip|plain|tools
```

API key from `BIFROST_API_KEY` env or `providers.bifrost.apiKey` in
`/srv/pi/.pi/agent/models.json`. Exit: `0` no loss, `1` ≥1 SSE mismatch,
`2` request-side drop.

### `sim_tool_call_owui.mjs` — full-stack tool-call simulation

Reproduces, on the REAL stack, the tool-call continuation turns where the
handoff places the drop. Two modes:

- **`interleaved`** (default) — 3 INTERLEAVED tool calls per round, the
  faithful reproduction of real agent sessions: `get_current_timestamp` →
  `smart_fetch_url` → `search_web`, each executed for REAL (time replicated
  from the OWUI builtin; fetch via the repo copy with curl_cffi; search via
  `POST /api/v1/retrieval/process/web/search`), then replayed as one chained
  OpenAI-style continuation (`assistant` tool_calls + `tool` results). This
  is the turn that triggers the pipe's `_history_has_tool_calls()` path.
- **`single`** — one tool call (`smart_fetch_url`) per round: discovery
  request (no `tools` field — the harness injects the model's attached
  tools), real tool execution, OpenAI-style continuation.

Both report the drop signature `reasoning_deltas <= 1` with content
present — the same condition as the pipe's SUSPECT-DROP log.

```bash
OWUI_API_KEY=sk-... \
node pipes/agent_loop_guard/tests/sim_tool_call_owui.mjs [rounds] [mode] [url]
#   rounds: iterations (default 5), mode: interleaved|single (default interleaved),
#   url: page to fetch (default https://elpais.com)
```

Defaults: `OWUI_BASE_URL=http://open-webui.private`, model `deepseek-v4-flash`
(the pipe model; see `GET /api/v1/models`). Exit: `0` no drop signature,
`1` ≥1 drop signature, `2` setup error.

Both modes prepend a long, realistic, **English, OWUI-style system prompt**
(no user name, no personal data) — the factor the HANDOFF links to higher
drop rates.

## Notes

- **Tool inventory for `deepseek-v4-flash`** (what the harness injects; verified
  via `GET /api/v1/models` → `info.meta.toolIds` + OWUI builtin categories):
  custom `smart_fetch_url` + `image_generator_pro`; builtin `time` category
  (`get_current_timestamp`, `calculate_timestamp` — the date/time tool, cheap
  and deterministic); builtin `web_search` category (`search_web`, `fetch_url`).
  Per user instruction, the battery must use ONLY `web_search` and
  `smart_fetch_url` (plus `get_current_timestamp`) — not other custom tools.
- The drop is **intermittent**: low rates with trivial system prompts, higher
  with the user's real long OWUI prompt. A "clean" battery run does not prove
  the bug is gone — cross-check the pipe logs
  (`sudo docker-compose logs … | grep bf-rea`) as ground truth.
- Never commit credentials: `OWUI_API_KEY` is read from the environment only
  (no hardcoded fallback in the repo). The test payloads contain no personal
  data (system prompt is a generic English template, no user name).
