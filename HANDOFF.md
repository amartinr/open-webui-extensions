# HANDOFF — Bifrost/DeepSeek reasoning loss (session 5: FINAL — root cause is Bifrost SSE, extensions are correct)

> For the next agent picking this up. Read this whole file before touching
> anything. It supersedes all previous HANDOFFs.
>
> **Versioning warning (IMPORTANT):** Bifrost is a monorepo where the
> **transport** (the deployable package, tag `transports/v*` — an old alias
> `npx/bifrost/v*` exists but is stale, what `/api/version` reports) and the
> **core** (the gateway engine, tag `core/v*`) are versioned independently. A
> transport release embeds a specific core version, declared in the file
> `core/version` inside that transport's tag (authoritative — do NOT infer it
> from the changelog page, which lists several core versions per transport
> page and is easy to misread). **Never assume the numbers match**: e.g. the
> GitHub tag `npx/bifrost/v1.6.3` contains core v1.5.11 (outdated, no DeepSeek
> provider) and is NOT representative of what runs in production; and the
> transport `transports/v1.6.11` embeds core **1.7.10** (NOT 1.6.11). The
> reliable code reference is the `core/v*` tags.

## TL;DR (final conclusion)

- User runs **Bifrost transport v1.6.11** (embeds core **1.7.10**) —
  re-deployed after this investigation. Confirmed live:
  `GET http://bifrost.private/api/version` → `"v1.6.11"`.
- **TWO separate Bifrost bugs were involved. Both are upstream (Bifrost), not
  Open WebUI, not the extensions, not DeepSeek itself.**
  1. **Request-side (FIXED in core 1.7.10, #5887):** core v1.6.3 routed
     DeepSeek through `stripReasoningDetails()`, nulling `reasoning_content`
     on EVERY assistant message including tool-call turns — violating
     DeepSeek's asymmetric contract. Fixed by
     `stripReasoningDetailsExceptToolCalls()` (preserves reasoning on
     tool-call turns). **1.6.11 has this fix.**
  2. **SSE-side (STILL OPEN, #6523 family):** Bifrost's stream can drop the
     reasoning deltas under load. The SAME request returns full `reasoning`
     in non-streaming mode but only the empty opening delta
     (`{"reasoning":"","reasoning_details":[{"text":""}]}`) in streaming mode.
     This is the residual drop still seen on 1.6.11.
- **The model ALWAYS reasons.** Non-stream responses consistently contain
  `reasoning` (89–300+ chars) even on tool-call turns — DeepSeek low is not
  "skipping reasoning". The reasoning disappears in Bifrost's SSE emission,
  not in the model.
- **The extensions (filter v3.5.0 + pipe v2.15.0) are correct, still
  necessary, and cannot fix either upstream bug.** They are a safety net for
  the dialect normalization + replay; nothing to change in this repo.
- **Nothing more can be done from this side** beyond reporting the SSE loss
  upstream to Maxim/Bifrost with the reproducible evidence (see
  `repro_bifrost_reasoning_loss.mjs`).

## Repos / code involved

| Path | What |
|---|---|
| `../pi-bifrost-reasoning-fix` | pi extension (works), v0.2.2 |
| `../open-webui-extensions` | this repo: filter + pipe |
| `../open-webui` | Open WebUI source, tag **v0.11.1** (commit `d3e8bf3`) |
| `../bifrost-core` | Bifrost **core v1.6.3** (bug #1 present) |
| `../bifrost-core-1710` | Bifrost core v1.7.10 (fix #1 present; SSE bug still present) |
| `../bifrost-core-1711` | Bifrost core v1.7.11 (fix #1 present) |
| `../bifrost-npx` | full monorepo clone with all `transports/v*` + `core/v*` tags (source of truth for version map) |
| `filters/bifrost_reasoning_content_fix` | filter v3.5.0 |
| `pipes/agent_loop_guard` | pipe v2.15.0 (gateway proxy + loop guard + reasoning fix + R0/R{n} trace) |
| `pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs` | **integration probe** (committed): detects SSE-vs-request-side reasoning loss against a live Bifrost |

Deployment: **Open WebUI v0.11.1** → **Bifrost transport 1.6.11** (core 1.7.10,
`http://bifrost.private/v1`) → **DeepSeek v4 flash/pro**. Workspace models
point at the pipe sub-model (`agent_loop_guard.deepseek/deepseek-v4-flash`);
`base_model_id` on the workspace model is correct.

## Bifrost version / upstream issue map (final)

| Issue | Date | Version | What | Status |
|---|---|---|---|---|
| #3139 | 2026-04-29 | core 1.6.3 | non-standard `reasoning`/`reasoning_details` dialect for deepseek v4 | closed 07-03 |
| #3802 | 2026-05-27 | 1.5.4–1.5.5 | `reasoning_content` dropped on tool-call turns (`/anthropic`→Responses, Kimi); regression of #2093/#2284 | open |
| #4861 | 2026-06 | core 1.6.3 | convert thinking to disabled when tool_choice is required (DeepSeek) — only fires on `tool_choice:"required"`, not our case | fixed |
| #5325 | 2026-07-17 | — | reasoning exposed in Bifrost-specific fields (the "dialect") | open |
| #5887 | 2026-08 | core 1.7.10 | **DeepSeek asymmetric reasoning contract — `stripReasoningDetailsExceptToolCalls`** (bug #1, FIXED) | released |
| #6111 | 2026-08-13 | 1.6.10 | DeepSeek 400 "`reasoning_content` … must be passed back" (opencode path) | open |
| #6523 | 2026-08-25 | — | **streaming drops opening role-only delta** (bug #2, the residual SSE loss — STILL OPEN) | open |

## Transport ↔ core version mapping (from `core/version` in each tag — authoritative)

| Transport tag (`/api/version` reports this) | Tag date | Embedded core (`core/version`) | Notes |
|---|---|---|---|
| `transports/v1.5.10` | 2026-06-07 | 1.5.18 | June 2026 state |
| `transports/v1.6.0` | 2026-06-25 | 1.6.0 | June 2026 state; DeepSeek not yet first-class |
| `transports/v1.6.3` | 2026-07-06 | **1.6.3** | bug #1 introduced (DeepSeek first-class, #4852) |
| `transports/v1.6.5` | 2026-07-21 | 1.7.3 | — |
| `transports/v1.6.8` | 2026-08-05 | 1.7.6 | — |
| `transports/v1.6.10` | 2026-08-12 | 1.7.9 | — |
| `transports/v1.6.11` (current) | 2026-08-15 | **1.7.10** | bug #1 fixed; bug #2 (SSE) still present |
| `transports/v2.0.0-prerelease3` | — | 1.7.11 | bug #1 fixed |
| `transports/v2.0.0` | — | 1.8.3 | bug #1 fixed; untested for bug #2 |

How to read the mapping (no Docker needed, repo already cloned at
`/srv/pi/bifrost-npx`):

```bash
cd /srv/pi/bifrost-npx
for tag in transports/v1.6.3 transports/v1.6.11 transports/v2.0.0; do
  echo "$tag -> $(git show $tag:core/version | head -1)"
done
```

When picking an upgrade, check `core/version` in the transport's tag — do not
trust the transport number alone.

## Symptom (live Open WebUI)

"With reasoning ON, sometimes — in some turns mid-conversation — the model
does not reason, then recovers on its own." Reproduced at both `low` and
`high` effort. Pi does not exhibit it (pi's OpenAI SDK parses
`reasoning`/`reasoning_details` natively and does not depend on Open WebUI's
SSE-delta reconstruction; also pi's traffic does not hammer the gateway in
the same burst pattern as the OWUI tool loop).

## Empirical evidence (final, sessions 3–5, direct A/B against bifrost.private/v1)

The committed probe (`pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs`)
runs both modes on the SAME payload and flags mismatches. Key results:

- **plain** (no tools): reasoning always present (stream), 0 drops.
- **toolsrequested** (tools present, no prior tool call): reasoning always present, 0 drops.
- **toolhistory** (tool-call continuation): reasoning_deltas fluctuate wildly by
  batch — 18/18 drops in a burst run (repro16), 0/8 in a relaxed run (repro22)
  → strongly correlated with gateway load / request rate, not with payload shape.
- **DECISIVE (repro18 #0):** the SAME tool-call-continuation payload returned
  `reasoning` = 128 chars in **non-streaming** mode but `reasoning_len=0`
  (only the empty opening delta) in **streaming** mode. The model reasoned;
  Bifrost's SSE did not forward it. This is bug #2.
- The `reasoning_deltas=1` signature seen in the user's pipe logs is exactly
  that empty opening delta `{"reasoning":"","reasoning_details":[{"text":""}]}`.
- The replay shape (reasoning_content with text / reasoning_details / empty /
  absent) does NOT change the outcome — the drop is in Bifrost's stream
  emission, before DeepSeek's behavior even matters.

## DB evidence (user's live instance, chat "Hola")

- `history.messages` is a keyed object; assistants have no flat
  `reasoning_content` (v0.11.1 stores reasoning as `output` items of type
  `reasoning`).
- The two early assistants have `output: [message]` only — no reasoning item,
  faithful (DeepSeek didn't reason on those turns).
- The reasoning assistant (`3c00e72f…`) has 5 `reasoning` items totalling
  **3236 chars** of real text — stored intact.
- ⇒ Hypotheses H1 (empty reasoning seed) and H3 (broken OWUI reconstruction)
  refuted; the R0s in pipe logs were faithful.

## Why the extensions are correct and still necessary (with core 1.7.10)

| Extension piece | Still needed on 1.6.11? | Why |
|---|---|---|
| Pipe `_clean_stream_delta` (reasoning/reasoning_details → reasoning_content + strip) | ✅ Yes | core 1.7.10 STILL emits the dialect in SSE deltas (`ChatStreamResponseChoiceDelta` carries `reasoning` + `reasoning_details`, verified in code). OWUI suppresses the frontend reasoning event when `reasoning_details` is present. |
| Pipe `_normalize_reasoning_for_gateway` (rename residue → reasoning_content) | ✅ Yes | OWUI-reconstructed history can still carry the dialect. |
| Pipe forcing `reasoning_content` on tool-call turns | ✅ Yes, and now effective | On 1.6.3 Bifrost wiped it regardless (forcing was futile); on 1.7.10 `stripReasoningDetailsExceptToolCalls` PRESERVES it, so the forcing now has real effect. |
| Pipe monkey-patch `_install_reasoning_replay_patch` (`get_reasoning_format`) | ✅ Yes | OWUI v0.11.1 still returns None for pipe models → `convert_output_to_messages` would drop reasoning when rebuilding history. The patch is what puts the REAL text into `reasoning_content` (vs the empty-string fallback). Now that Bifrost preserves it, the patch's output actually reaches DeepSeek. |
| Filter `_fix_delta` / `_fix_event` | ✅ Yes | Same dialect-on-SSE reason as the pipe. |
| Filter inlet forcing | ✅ Yes | Same reason. |

Nothing in this repo needs to change because of the 1.6.11 upgrade. The
extensions fix the dialect round-trip; they cannot fix Bifrost's SSE emission
bug (bug #2) — only upstream can.

## Secondary bug found (still real, unrelated to the Bifrost issues)

`_normalize_thinking_for_gateway` strips `thinking:{type:"disabled"}` on the
assumption that Open WebUI injects it on tool-call continuations. Verified in
v0.11.1 source: OWUI never emits `thinking` and does not drop
`reasoning_effort`. The `thinking:disabled` the pipe sees is the user's own
`deepseek_thinking_default_off` filter → with the reasoning chip OFF and a
tool call, the pipe re-enables reasoning against the user's intent. Fix:
remove the strip or gate behind an opt-in valve (default off). Not urgent,
unrelated to the current issue.

## What was done (sessions 3–5)

- Reproduced the drop directly against Bifrost (tool-call continuation
  payloads) — disproving the earlier "doesn't reproduce via API" claim.
- Cloned Bifrost core v1.6.3 / v1.7.10 / v1.7.11 + full monorepo tags. Found
  bug #1 with line-level diff and its exact fix (#5887).
- Established the authoritative transport↔core map from `core/version` in
  each tag (1.6.11 → core 1.7.10; the npx v1.6.3 GitHub tag is stale).
- Isolated bug #2: same-payload non-stream vs stream mismatch → Bifrost SSE
  drops reasoning deltas under load (#6523 family).
- Confirmed via the user's DB that OWUI reasoning storage is intact.
- User re-deployed transport 1.6.11 (core 1.7.10) — bug #1 is gone, bug #2
  remains intermittent.
- Committed `patches/openwebui-29052-middleware.patch` and the integration
  probe `pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs`.
- Unit tests pass: filter 9/9; pipe 47/47 (via `python3 -m pytest`; the
  in-repo `.venv` no longer exists).

## Next steps

1. **Report bug #2 upstream to Maxim/Bifrost** with the reproducible evidence:
   same tool-call-continuation payload → non-stream has `reasoning` (128+
   chars), stream emits only the empty opening delta. Reference #6523.
   Include the probe script invocation and the observed mismatch sample.
2. **Probe `transports/v2.0.0` (core 1.8.3) — now the primary candidate.**
   Session 6 found that core 1.8.0+ added a `MarshalJSON` to
   `ChatStreamResponseChoiceDelta` that emits the reasoning phase under BOTH
   `reasoning` and `reasoning_content` in streaming deltas (see below). This
   directly targets the SSE-side bug #2. User is testing 2.0.0 now — if the
   intermittent drop disappears, bug #2 is fixed upstream and the
   extension's `_clean_stream_delta` / monkey patch can be relaxed.
3. **Optional:** fix the secondary chip-off bug (strip `thinking:disabled`
   only when opt-in).
4. Re-check the original downgrade reason (1.6.11 harness integration issues)
   now that 1.6.11 is re-deployed — separate concern from reasoning.

## Session 6 finding: SSE fix landed in core 1.8.0 (relevant for bug #2)

Verified in the `transports/v2.0.0` tag (embeds core 1.8.3) vs the current
`transports/v1.6.11` (core 1.7.10):

- **NEW in core 1.8.0** — `core/schemas/chatcompletions.go`,
  `ChatStreamResponseChoiceDelta` now has a custom `MarshalJSON` that emits
  the reasoning phase under BOTH `reasoning` AND `reasoning_content` on
  outbound stream deltas (absent in 1.7.10, present in 1.8.0/1.8.3). Code
  comment: *"DeepSeek streams its thinking phase under `reasoning_content`,
  so a client written against that wire watched a Bifrost stream emit the
  entire reasoning phase under a key it never read."* This is exactly the
  #6523-family mismatch observed in session 5 (non-stream has reasoning,
  stream does not deliver it in a field clients read).
- Other relevant changes in 1.7.10→1.8.3: #5900 (omit `name` on streaming
  continuation deltas), #6293 (finer reasoning-with-tools param handling in
  `dropUnsupportedParams`), several streaming telemetry/heartbeat fixes.
- If 2.0.0 (core 1.8.3) resolves the intermittent drop, the extensions stay
  as a safety net but the reasoning replay patch and delta normalization
  become redundant for the stream side (still needed for the OWUI
  history-reconstruction path, which is independent of Bifrost).

## Lessons (all sessions)

1. **Do not leave `reasoning_details` in stream deltas.** OWUI suppresses the
   frontend `response.reasoning_text.delta` event when a delta carries
   `reasoning_details` (data=None, DB-only). Filter v3.5.0 / pipe
   `_clean_stream_delta` strip them.
2. **Never override the user's `reasoning_effort`.** Hard requirement.
3. **Open WebUI executes function code from its DB (Admin → Functions), not
   this repo.** Deploy = re-paste + restart if `stream()` changed.
4. **Do not commit the Bifrost API key** (lives in `models.json` /
   `BIFROST_*` / pi extension config).
5. **Bifrost transport and core versions are independent** — read `core/version`
   inside the transport's tag, never assume equal numbers; the changelog page
   and the stale `npx/bifrost/v*` tags are both misleading.
6. **Distinguish request-side from SSE-side reasoning loss:** run the same
   payload non-streaming and streaming. Non-stream reasoning present + stream
   empty = Bifrost SSE bug; both empty = request-side (replay/refusal).

## Useful commands

```bash
# Bifrost version (transport)
curl -s http://bifrost.private/api/version

# integration probe (needs live Bifrost; reads key from env or models.json)
cd /srv/pi/open-webui-extensions
node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs 8 roundtrip
#   exit 0 = no loss; 1 = SSE loss (bug #2); 2 = request-side drop
node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs 6 plain
node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs 6 tools

# key Bifrost code
# bug #1:  /srv/pi/bifrost-core/core/providers/openai/chat.go:70-73 (case Cerebras,DeepSeek; stripReasoningDetails)
# fix #1:  /srv/pi/bifrost-core-1711/core/providers/openai/chat.go:78-84 + 232-244 (stripReasoningDetailsExceptToolCalls)
# dialect still emitted in 1.7.10 SSE: core/schemas/chatcompletions.go (ChatStreamResponseChoiceDelta: reasoning + reasoning_details)

# version map (authoritative)
cd /srv/pi/bifrost-npx && git show transports/v1.6.11:core/version

# unit tests
cd /srv/pi/open-webui-extensions
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

## Git / hook notes

- Hook: `.git/hooks/commit-msg` appends `Co-Authored-By: Pi <noreply@pi.dev>`
  unless already present (dedup via `grep -qF`).
- SSH key `~/.ssh/id_ed25519` authenticates as `amartinr` (host key in
  `known_hosts`). Remote is SSH, not HTTPS.
