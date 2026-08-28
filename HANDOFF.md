# HANDOFF — Bifrost/DeepSeek reasoning loss (session 8: drop persists on Bifrost 2.0.0 — diagnostic instrumentation deployed)

> For the next agent picking this up. Read this whole file before touching
> anything. It supersedes all previous HANDOFFs.
>
> **Versioning warning (IMPORTANT):** Bifrost is a monorepo where the
> **transport** (the deployable package, tag `transports/v*` — what
> `/api/version` reports) and the **core** (the gateway engine, tag `core/v*`)
> are versioned independently. A transport embeds a specific core version,
> declared in `core/version` inside that transport's tag (authoritative).
> `transports/v1.6.11` embeds core **1.7.10** (NOT 1.6.11); `transports/v2.0.0`
> embeds core **1.8.3**. Read `core/version` in the tag — never trust the
> transport number alone. The GitHub tag `npx/bifrost/v1.6.3` is stale
> (contains core v1.5.11) — do not use it as a code reference.

## Current state (session 8)

- **Deployment**: Open WebUI **v0.11.1** → Bifrost **transport 2.0.0** (core
  **1.8.3**, `http://bifrost.private/v1`) → DeepSeek **v4 flash/pro**.
  Confirmed live: `GET http://bifrost.private/api/version` → `"v2.0.0"`.
- **Pipe**: `agent_loop_guard` **v2.16.2** (this branch) — includes:
  - reasoning replay monkey patch (`_install_reasoning_replay_patch`);
  - outbound normalization + forcing (`_normalize_reasoning_for_gateway`);
  - **Bifrost 2.0.0 delta-duplication fix** (v2.15.1+: reasoning_content
    already present from the gateway must NOT be re-appended);
  - all reasoning diagnostics gated behind `REASONING_DEBUG_LOG` valve
    (default off);
  - **NEW diagnostic**: logs the full OUTBOUND request (url/headers/payload)
    and, on suspect drops, the raw reasoning SSE events received.
- **Filter**: `bifrost_reasoning_content_fix` **v3.6.0** (this branch) —
  includes the same delta-duplication fix (v3.5.1+) and a `debug_log` valve
  (default off) gating the inlet logs.
- **The intermittent reasoning drop PERSISTS on 2.0.0.** The previous HANDOFF
  (v7) declared the investigation closed — that was WRONG. This session
  confirmed the drop still happens in the user's live Open WebUI.
- **ROOT CAUSE CONFIRMED (session 8, via SUSPECT-DROP raw events): the
  reasoning is lost INSIDE Bifrost's SSE emission, before the wire.** The raw
  reasoning-carrying events received on a failing turn contain ONLY the empty
  opening delta
  (`{"delta":{"role":"assistant","reasoning":"","reasoning_details":[{"text":""}],"reasoning_content":""}}`)
  and nothing else — Bifrost never emits the actual reasoning deltas. The
  core 1.8.0 `MarshalJSON` fix (adding `reasoning_content` to deltas) did NOT
  resolve this: the drop happens before serialization, so the alias never
  helps.

## The problem (unchanged symptom)

"With reasoning ON, sometimes — in some turns — the model does not reason,
then recovers on its own." Occurs at both `low` and `high` effort. The pipe
log signature on a failing turn: `reasoning_deltas=1` (only the empty opening
delta) with content present.

## Established facts (verified this session and before)

1. **The model ALWAYS reasons.** Non-stream responses consistently contain
   `reasoning` even on failing-looking turns — DeepSeek low is not "skipping
   reasoning". (Earlier confusion came from a broken non-stream parser reading
   only `message.reasoning`/`reasoning_content`; Bifrost returns the reasoning
   in other keys. Do not re-litigate this — user demonstrated it fails on
   `high` too, which rules out prompt/model decision theories.)
2. **Direct A/B against Bifrost 2.0.0 with the user's REAL payload (system
   prompt + "Hola" + the 4 tools) reproduces the drop**: 4/12 and 1/8 rounds
   with `rd=1, len=0` (stream, status 200, content present). Same payload with
   a trivial system prompt ("You are a helpful assistant.") → 0 drops. So the
   **system prompt content influences the drop rate** but does not cause it —
   the model still always reasons per fact #1.
3. **Payload/headers are NOT the cause.** The pipe's OUTBOUND log (see
   below) shows the exact request: `[system(user's long OWUI prompt), user
   "Hola"]`, `reasoning_effort: low`, `thinking: {type: enabled}`,
   `stream_options: {include_usage: true}`, 4 tools, headers
   `x-bf-vk`, `x-bf-dim-host: open-webui`, `x-bf-dim-username: Abel`. Two
   identical consecutive requests can differ (one reasons, one doesn't) —
   byte-identical payloads, different outcomes → the drop is NOT payload
   driven.
4. **The pipe/filter are no-ops on the failing first turn** (`renamed=0
   forced=0`), so they cannot cause it.
5. **Bifrost's object-`user` quirk**: OWUI injects `payload['user']` as an
   OBJECT for pipes; Bifrost 2.0.0 declares `user` as `*string` and returns
   HTTP 400 "Invalid request payload" for it. NOT the current issue — the
   failing turns return 200 with content (the pipe's params log shows no
   `user` field reaching Bifrost, so OWUI/pipe strip it somewhere).
6. **The delta-duplication bug (2.0.0-specific) IS fixed** in pipe 2.15.1+ /
   filter 3.5.1+: Bifrost core ≥ 1.8.0 emits each reasoning fragment in THREE
   fields (`reasoning`, `reasoning_content`, `reasoning_details` — identical
   text). The old code appended `reasoning` to the existing
   `reasoning_content`, doubling per layer and quadrupling across pipe +
   filter (visible as "LetLetLetLet me me me me" in the reasoning panel).
   Fixed: only synthesize when the gateway did not provide the field.

## What was ruled out

- ❌ Payload shape (identical requests differ)
- ❌ Headers (identical in failing/succeeding turns)
- ❌ The pipe/filter (no-ops on the failing turn)
- ❌ Open WebUI history reconstruction (fails on turn 1 with empty history)
- ❌ The empty-reasoning "seed" (H1 — turn 2 with replayed `""` reasons fine)
- ❌ Model decision theory (fails on `high` too; non-stream always reasons)
- ❌ Duplication bug (fixed; not related to the drop)

## Root cause (CONFIRMED via SUSPECT-DROP raw events, session 8)

**The reasoning is lost inside Bifrost's SSE emission, before the wire.** The
pipe's SUSPECT-DROP log captured the raw reasoning-carrying events on failing
turns (multiple occurrences, always with tool-call history present):

```
SUSPECT DROP reasoning_deltas=1 content_deltas=32 — raw reasoning events:
{"delta":{"role":"assistant","reasoning":"","reasoning_details":[{"text":""}],"reasoning_content":""}}
```

The raw events contain ONLY the empty opening delta (all three fields empty)
and nothing else. Bifrost emits the opening ceremony and never emits the
actual reasoning deltas. This is upstream #6523-family behavior, NOT resolved
by the core 1.8.0 MarshalJSON fix (which only aliases `reasoning_content` on
deltas that ARE emitted — the drop happens before emission).

The core 1.8.0 fix DID resolve the delta-duplication issue (triple-field) and
the earlier request-side wipe (#5887, core 1.7.10) — those are fixed. This
remaining drop is a separate, still-open SSE-emission defect in Bifrost.

Observed drop pattern in one real session (11 pipe turns): drops at tool-call
continuations and final turns, 3/11 turns affected, each with the identical
opening-only raw signature.

## Diagnostic instrumentation deployed (pipe v2.16.2)

Both are gated behind `REASONING_DEBUG_LOG` (the user has it ON):

1. **OUTBOUND log** — before sending to the gateway:
   ```
   bf-reasoning: OUTBOUND url=... headers=... payload=...
   ```
2. **SUSPECT DROP log** — in `_stream`, when the response ends with
   `reasoning_deltas <= 1` AND `content_deltas > 0`, it logs the raw
   reasoning-carrying SSE events received (up to 12, truncated):
   ```
   bf-reasoning: SUSPECT DROP reasoning_deltas=1 content_deltas=46 — raw reasoning events: <events or "<none emitted>">
   ```

**Interpretation (already exercised)**:
- `<none emitted>` → Bifrost sent no reasoning-carrying event at all →
  gateway-side loss BEFORE the pipe.
- opening-only event then nothing → gateway emitted only the ceremony →
  gateway-side. ← **This is what was observed.**
- (If raw events show real reasoning but `reasoning_deltas=1`, that is
  impossible — the pipe counts everything it receives; that case would mean a
  pipe counting bug, which does not exist.)

## Simulating a tool call through Open WebUI's OpenAI-compatible API (session 8.5 — HOWTO)

**Goal:** reproduce the tool-call continuation turns (the drop scenario) end-to-end
through the REAL stack (Open WebUI → pipe `agent_loop_guard` → filter → Bifrost),
without the UI. Verified working; the continuation turn below completed with
`reasoning_deltas=53, len=250` (no drop that round — see battery notes).

### Endpoint & auth (user-provided)

- `POST http://open-webui.private/api/chat/completions` (also
  `/api/v1/chat/completions`; NOT `/openai/chat/completions` — that is the direct
  external passthrough and 404s on pipe model ids).
- API key: user-provided, passed via `OWUI_API_KEY` env (do not commit).
- Models: `GET /api/v1/models` → pipe models `deepseek-v4-flash` (base
  `agent_loop_guard.deepseek/deepseek-v4-flash`), `deepseek-v4-assistant`, …
  (6 pipe models + `open-webui-meta-agent` + `virtual-fashion-stylist`), plus
  raw `deepseek/deepseek-v4-flash` / `deepseek/deepseek-v4-pro` passthrough.

### Who exposes the tools (confirmed)

The HARNESS does: the OWUI model config (`meta.toolIds`) attaches tools
(`smart_fetch_url`, `youtube_search`, …) and OWUI injects their schemas into the
request payload as `tools`; the pipe merely forwards `body["tools"]` to Bifrost.
Proof: a request with NO `tools` field still made the model call `smart_fetch_url`.
Tool schema exposed to the model: `GET /api/v1/tools/id/smart_fetch_url` → `specs`.

**CRITICAL schema detail:** the real params are `urls` (array, required),
`format` (enum skimmd|markdown|html|txt|json|raw), `max_chars`, `include_replies`.
The model HALLUCINATED `url`/`prompt`/`max_char` in its emitted call — the test
must use the real schema, never the model's emission.

**Tool inventory for `deepseek-v4-flash` (verified via
`GET /api/v1/models` → `info.meta.toolIds` + OWUI builtin categories):**
- Custom attached: `smart_fetch_url` (fetch — USE THIS), `image_generator_pro`
  (image gen — NOT relevant for the drop test).
- Builtin `time` category → `get_current_timestamp` (no args; Unix ts + ISO UTC
  + user local time) and `calculate_timestamp` (days/weeks/months/years_ago).
  The date/time tool — cheap and deterministic, ideal for hammering the drop
  signature without burning network fetches.
- Builtin `web_search` category → `search_web(query, count)` and `fetch_url`,
  gated by `web.search.enable` + model capability `web_search` (true here) +
  user permission.
- Per user instruction: the faithful battery must use ONLY `web_search` and
  `smart_fetch_url` (plus `get_current_timestamp` for date/time) — NOT
  `image_generator_pro` or other custom tools.

### The 3-step recipe (exactly what was done)

**Step 1 — discovery (stream, no `tools` in payload):**
```json
{"model": "deepseek-v4-flash", "stream": true,
 "messages": [{"role": "user", "content": "Haz fetch de https://elpais.com y cuéntame qué contiene"}]}
```
The tool call comes back as **markdown inside `content`** (OWUI pipe convention),
NOT structured `tool_calls` deltas:
```
Voy a hacer el fetch de El País para ti.

<tool_calls>
<invoke name="smart_fetch_url">
<parameter name="url">https://elpais.com</parameter>
<parameter name="prompt">Resume el contenido principal…</parameter>
<parameter name="max_char">4096</parameter>
</invoke>
</tool_calls>
```
Without a frontend nothing executes it → the client must do step 2+3.

**Step 2 — execute the real tool** (repo copy; deps curl_cffi/trafilatura/selectolax
installed with `pip install --break-system-packages`, no venv available):
```python
sys.path.insert(0, "/srv/pi/open-webui-extensions/tools/smart_fetch_url")
from smart_fetch_url import Tools
async def main():
    t = Tools()
    try: print(await t.smart_fetch_url(urls=["https://elpais.com"], format="skimmd", max_chars=4096))
    finally: await t._aclose()
asyncio.run(main())
```
Result: HTTP 200 + extracted front page (4 KB). NOTE: my `web_fetch` got HTTP 403
on elpais.com; the tool's curl_cffi fingerprinting succeeds where it fails.

**Step 3 — the continuation (the faithful tool-call round-trip):** OpenAI-style
structured `tool_calls` in the assistant message + `tool` role result. This is what
triggers the pipe's `_history_has_tool_calls()` → ships `tools` to Bifrost and runs
`_force_reasoning_content_on_tools` — the exact code path the drop lives on:
```json
{"model": "deepseek-v4-flash", "stream": true, "messages": [
  {"role": "user", "content": "Haz fetch de https://elpais.com y cuéntame qué contiene"},
  {"role": "assistant", "content": null, "reasoning_content": "",
   "tool_calls": [{"id": "call_sim_001", "type": "function",
     "function": {"name": "smart_fetch_url",
       "arguments": "{\"urls\": [\"https://elpais.com\"], \"format\": \"skimmd\", \"max_chars\": 4096}"}}]},
  {"role": "tool", "tool_call_id": "call_sim_001", "name": "smart_fetch_url",
   "content": "<full tool output, 4.3 KB>"},
  {"role": "user", "content": "Resume ahora lo que contiene la portada de El País según el resultado de la herramienta"}
]}
```
Observed result: HTTP 200, 618 SSE events, `reasoning_deltas=53`, `reasoning_len=250`,
`content_len=1869`, no tool_call events, `finish=stop`; model summarized the fetched
front page correctly (Ceuta crisis, Marlaska vs PP-Vox, Villena museum heist, Nepal
flood, Leavitt resignation). Harness quirks visible: OWUI injects the user's real
name into the system prompt (responses address the user by first name).

**Faithfulness checklist for a drop-repro battery:**
- Use `deepseek-v4-flash` and the REAL tool schema (not the model's hallucinated
  params) in the continuation.
- Keep `reasoning_content: ""` in the assistant tool-call message (the empty
  seed — ruled out as cause in H1, but matches real OWUI history).
- The drop is intermittent; low rate with trivial system prompts (see §facts).
  For a faithful rate, the system prompt matters — consider shipping a long real
  OWUI-style system prompt and/or run many rounds (20+) and watch the pipe's
  SUSPECT-DROP logs (`sudo docker-compose logs … | grep bf-rea`) as ground truth.
- SSE-only: measure `reasoning_deltas <= 1 && content present` on the
  continuation turn — same signature as the pipe's SUSPECT-DROP.

## Next steps (for the next agent)

1. **The drop is CONFIRMED gateway-side (Bifrost).** No further local
   diagnosis is needed — the SUSPECT-DROP raw events already prove the
   reasoning never reaches the wire. The options are:
   a. **Report upstream to Maxim/Bifrost** with the SUSPECT-DROP evidence
      (opening-only raw events on tool-call continuations, #6523 family).
      This is the real fix path.
   b. **Mitigation in the pipe (last resort):** re-request once when the
      stream ends with `reasoning_deltas <= 1 && content > 0` AND
      tool-call history is present. Burns tokens (double request); only if
      the user accepts the cost.
2. Optional pending items (unrelated): fix the secondary chip-off bug
   (`_normalize_thinking_for_gateway` strips `thinking:disabled` — re-enables
   reasoning against user intent when the chip is OFF); re-check the original
   1.6.11 downgrade reason on 2.0.0.

## Repos / code involved

| Path | What |
|---|---|
| `/srv/pi/open-webui-extensions` | this repo (branch `debug/reasoning-content-trace` = current work) |
| `/srv/pi/open-webui` | Open WebUI source, tag v0.11.1 (commit `d3e8bf3`) |
| `/srv/pi/bifrost-core` | Bifrost core v1.6.3 (buggy) |
| `/srv/pi/bifrost-core-1710` / `-1711` | core v1.7.10 / v1.7.11 (fix #5887) |
| `/srv/pi/bifrost-npx` | full monorepo clone, all `transports/*` + `core/*` tags (version map source) |
| `pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs` | integration probe, direct to Bifrost (in repo) |
| `pipes/agent_loop_guard/tests/sim_tool_call_owui.mjs` | integration probe, full stack via OWUI OpenAI API — `single` / `interleaved` (3 real tool calls chained) |
| `/tmp/repro_*.mjs` | ad-hoc probes from this investigation |

## Branch / git state

- **`master`**: pipe v2.15.0 + filter v3.5.0 + READMEs (no duplication fix,
  no valves). Production baseline.
- **`debug/reasoning-content-trace`** (current, pushed): pipe v2.16.2 + filter
  v3.6.0 — duplication fix + valve-gated logs + OUTBOUND/SUSPECT-DROP
  diagnostics. HANDOFF lives here.
- **`fix/bifrost-2-reasoning-duplication`** (pushed): the duplication fix
  alone (pipe 2.15.1 / filter 3.5.1) — superseded by debug branch content.
- Remote: `git@github.com:amartinr/open-webui-extensions.git` (SSH).
- Commit hook appends `Co-Authored-By: Pi <noreply@pi.dev>`.
- **Do not commit the Bifrost API key** (lives in
  `/srv/pi/.pi/agent/models.json` → `providers.bifrost.apiKey`; probes read it
  from there or `BIFROST_API_KEY`).

## Useful commands

```bash
curl -s http://bifrost.private/api/version                      # transport version
sudo docker-compose logs -f --tail 100 open-webui | grep bf-rea # pipe/filter logs
sudo docker-compose logs -f --tail 100 bifrost | grep -iE "deepseek|error|reasoning"  # gateway side

# probe (needs live Bifrost)
node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs 8 roundtrip

# full-stack tool-call simulation (needs live Open WebUI + Bifrost)
# interleaved = 3 real chained tool calls (get_current_timestamp → smart_fetch_url → search_web)
OWUI_API_KEY=sk-... node pipes/agent_loop_guard/tests/sim_tool_call_owui.mjs 10 interleaved
OWUI_API_KEY=sk-... node pipes/agent_loop_guard/tests/sim_tool_call_owui.mjs 5 single

# version map
cd /srv/pi/bifrost-npx && git show transports/v2.0.0:core/version

# tests
cd /srv/pi/open-webui-extensions && python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

## Deployment note

Open WebUI executes function code from its DB (Admin → Functions), not from
this repo. Deploy = re-paste + save; restart only if `stream()` changed.
