# HANDOFF — Bifrost/DeepSeek reasoning loss (session 2: version archaeology + root-cause narrowing)

> For the next agent picking this up. Read this whole file before touching
> anything. It supersedes the previous HANDOFF for the Open WebUI side; the
> pi-side findings are unchanged.

## TL;DR

- The user is on **Bifrost 1.6.3** (downgraded from 1.6.11 for unrelated
  harness integration issues). Confirmed live: `GET http://bifrost.private/api/version` → `"v1.6.3"`.
- **1.6.3 is exactly the release that introduced the non-standard
  `reasoning`/`reasoning_details` dialect** (upstream #3139, closed
  2026-07-03). Before it, Bifrost emitted standard `reasoning_content` and
  "everything just worked".
- The total reasoning-drop **does NOT reproduce via direct API calls against
  1.6.3** (0 drops across ~27 A/B requests: streaming + non-streaming,
  `thinking+effort` vs `effort`-only, 1–2 tool-call history). It **does**
  reproduce in the user's live Open WebUI (raw stream shows
  `reasoning_deltas=1` on the failing turn).
- Therefore the drop is **not** a simple payload-format issue; it is something
  specific to the payload Open WebUI actually reconstructs, most likely an
  **empty `reasoning_content` (`""`) being replayed** (which the current
  `forced` counter cannot see), seeding a cascade where DeepSeek stops
  reasoning and the empty reasoning is re-replayed.
- Pi works because it replays the reasoning **verbatim** from Bifrost's own
  `reasoning` field. Open WebUI reconstructs it from OR-aligned `output` items
  and relies on a monkey-patch that can yield empty/absent text.
- Strong lead: DeepSeek's **"unverifiable reasoning refusal"**, fixed upstream
  in Bifrost **core v1.7.10 / v1.7.11** ("retry after an unverifiable
  reasoning refusal … replayed reasoning on `reasoning_details`"). 1.6.3 has
  no such mitigation.

## Repos / code involved

| Path | What |
|---|---|
| `../pi-bifrost-reasoning-fix` | pi extension (WORKS in production), v0.2.2 |
| `../open-webui-extensions` | this repo: filter + pipe |
| `../open-webui` | Open WebUI source, tag **v0.11.1** (commit `d3e8bf3`) |
| `filters/bifrost_reasoning_content_fix` | filter v3.5.0 |
| `pipes/agent_loop_guard` | pipe v2.14.0 (gateway proxy + loop guard + reasoning fix) |

Deployment: **Open WebUI v0.11.1** → **Bifrost 1.6.3** (`http://bifrost.private/v1`)
→ **DeepSeek v4 flash/pro**. Workspace models point at the pipe sub-model
(`agent_loop_guard.deepseek/deepseek-v4-flash`). `base_model_id` on the
workspace model is correct (points at the pipe sub-model).

## Bifrost version / upstream issue map (the big picture)

| Issue | Date | Version | What | Status |
|---|---|---|---|---|
| #2093 | 2026-03-15 | — | `reasoning_content` stripped when forwarding to thinking models (Kimi, DeepSeek-R1) | closed (fix #2284) |
| #3139 | 2026-04-29 | **1.6.3** | **[Feature] adds non-standard `reasoning`/`reasoning_details` for deepseek v4** | closed 07-03 |
| #3802 | 2026-05-27 | 1.5.4–1.5.5 | `reasoning_content` dropped on tool-call turns (`/anthropic`→Responses, Kimi); regression of #2093/#2284 | open |
| #5325 | 2026-07-17 | — | reasoning exposed in Bifrost-specific fields (the "dialect") | open |
| #6111 | 2026-08-13 | **1.6.10** | DeepSeek 400 "`reasoning_content` … must be passed back" (opencode path) | open |
| #6523 | 2026-08-25 | — | streaming drops opening role-only delta | open |
| core v1.7.10 / v1.7.11 | — | 1.7.x | **fix: retry after "unverifiable reasoning refusal"** | released |

Conclusion (high confidence): the dialect was born in 1.6.3; the reasoning
drop/refusal the extensions fight is a **later regression** (family
#2093 → #2284 → #3802 → #6111). The user's downgrade to 1.6.3 therefore has
the dialect but, per direct testing, not the reproducible total drop.

## Symptom (live Open WebUI, this session)

"With reasoning ON, sometimes — in some turns mid-conversation — the model
does not reason, then recovers on its own." Reproduced at **both** `low` and
`high` effort. Pi does not exhibit it with the same model + Bifrost.

## Critical trace evidence (from the user's pipe logs)

The pipe's `bf-reasoning:` lines on a failing 3-turn run:

```
renamed=0 forced=0 thinking_stripped=no … messages: [system- user- assistantR user- …]
response events=… reasoning_deltas=1 content_deltas=33 …   <-- DROP (reasoning_deltas=1)
```

and the `forced` counter across the run went `0 → 0 → 1 → 1 → 3 → 0`, with
`reasoning_deltas` tracking `… → 1 → 1 → 1 → 326` (self-recovery).

What this means:

1. `renamed=0 forced=0` + all assistants flagged `R` → the payload **looks**
   conformant. `thinking`/`reasoning_effort`/`tools` are all present and
   correct. Display-suppression (the `reasoning_details → data=None` bug) is
   ruled out because the drop is visible in the **raw** stream
   (`reasoning_deltas=1` is the empty-reasoning signature of Bifrost).
2. The `forced` climb `0 → 1 → 3` shows assistants starting to arrive
   **without** `reasoning_content`, so the pipe injects `""`. That is the
   cascade: once a turn drops reasoning, the empty reasoning is stored and
   replayed, so subsequent continuations stay dropped until history/randomness
   lets it recover.

### The gap in the current `forced` counter

```python
if not isinstance(msg.get("reasoning_content"), str):
    msg["reasoning_content"] = ""
    forced += 1
```

`forced` only counts **absent** `reasoning_content`, not **present-but-empty
(`""`)**. So a replayed `reasoning_content=""` (the dangerous case) passes
silently with `forced=0` while DeepSeek sees empty reasoning and stops.

## Hypotheses (ordered by likelihood)

- **H1 (primary): empty `reasoning_content` seed.** Open WebUI reconstructs an
  assistant with `reasoning_content=""` (or the monkey-patch yields empty),
  the pipe leaves it (not counted by `forced`), DeepSeek sees empty reasoning
  → refuses to reason → empty reasoning is stored → cascade. The new `R0`/`R{n}`
  debug flag (below) will confirm this in one trace.
- **H2: "unverifiable reasoning refusal".** DeepSeek can't verify the replayed
  reasoning text (fragment/newline/order mismatch from reconstruction) and
  silently refuses (empty reasoning, no error). 1.6.3 lacks the v1.7.10/1.7.11
  retry. Mitigation is upstream (Bifrost ≥ 1.7.10).
- H3: Open WebUI's `convert_output_to_messages` rebuilds reasoning text that
  differs byte-wise from what DeepSeek emitted (H1/H2 overlap).

## Why pi works and Open WebUI does not

| Concern | pi | Open WebUI |
|---|---|---|
| Replay reasoning | verbatim `reasoning` → `reasoning_content` | reconstructed from OR `output` items + monkey-patch (`get_reasoning_format`) |
| Hook point | single `before_provider_request` (sees every request) | split: filter `inlet` (user turns only) + pipe `pipe()` (choke point for tool-call continuations) |
| Stream dialect | OpenAI SDK parses `reasoning`/`reasoning_details` natively; `after_provider_response` is a no-op | must actively normalize deltas + filter SSE noise (OWUI renders non-`data:` lines as chat content) |
| `thinking` control | user maps `reasoning_effort` only (no `thinking` object) | user's own filters set `thinking:{type:enabled/disabled}` + `reasoning_effort` |

## Secondary bug found (not the core issue, but real)

`_normalize_thinking_for_gateway` strips `thinking:{type:"disabled"}` on the
assumption that Open WebUI injects it on tool-call continuations. **That
assumption is wrong on both counts** — verified in v0.11.1 source:

- Open WebUI does **not** emit `thinking` anywhere (`grep` across
  `chat.py`/`routers/*` is empty).
- It does **not** drop `reasoning_effort` (both propagate via
  `new_form_data = {**form_data, ...}`).

The `thinking: disabled` the pipe sees is the user's **own**
`deepseek_thinking_default_off` filter. Net effect: with the reasoning chip
OFF and a tool call, the pipe **re-enables reasoning against the user's
intent**. Fix: remove the strip, or gate it behind an opt-in valve (default
off).

## What was done this session

- Cloned `pi-bifrost-reasoning-fix`, `open-webui-extensions`, and
  `open-webui` (tag v0.11.1). Read the full filter/pipe/middleware paths.
- Confirmed Bifrost 1.6.3 via `/api/version`; confirmed the dialect in live
  responses (`reasoning`/`reasoning_details`, no `reasoning_content`).
- Ran the author's `verify-fix.mjs` (with dim headers) → the strict
  "without-fix = 0 reasoning chunks" expectation did **not** hold on 1.6.3
  (got 52 chunks). Confirmed 0 drops in all direct A/B tests (streaming and
  non-streaming, `thinking+effort` vs `effort`, 1–2 tool-call history).
- Ran unit tests: pi extension 10/10; filter 9/9; pipe 8/8.
- Created branch `debug/reasoning-content-trace` with:
  - `_messages_summary`: flag `R0` (empty) vs `R{n}` (text of length n).
  - new log `bf-reasoning: last assistant reasoning_content len=… empty=… preview=…`.
- Git repo config: remote → `git@github.com:amartinr/open-webui-extensions.git`
  (SSH), user `A. Martin <abel.martin.ruiz@gmail.com>`, `commit-msg` hook adds
  `Co-Authored-By: Pi <noreply@pi.dev>` with dedup (skips if present).

## Next steps (debugging)

1. **Reproduce with the debug branch** (`debug/reasoning-content-trace`, already
   pushed). Look at the failing turn's:
   - `messages:` flags — `R0` = empty reasoning replayed (H1 confirmed);
     `R{n}` = real text (→ H2 refusal).
   - `last assistant reasoning_content len=… empty=…` log line.
2. **If R0 confirmed**: find where Open WebUI stores empty reasoning. Check the
   `reasoning` item's `content` vs `summary` in the DB (`convert_output_to_messages`
   uses `summary` first, then `content`). Bifrost never sends
   `reasoning_summary_part` events, so `summary` stays `None` — verify the
   fallback path actually reconstructs text, and whether the monkey-patch is
   being bypassed on some path.
3. **If R{n} confirmed (real text)**: the refusal is upstream. Re-test against a
   Bifrost ≥ 1.7.10 and confirm the drop disappears (this is the decisive check).
4. Fix the `forced` counter to also flag `reasoning_content == ""` (or at least
   log it) — the current `isinstance(str)` check is a blind spot.
5. Fix the secondary chip-off bug (strip `thinking:disabled` only when opt-in).

## Next steps (Bifrost versions)

- **On 1.6.3**: keep the fix as a no-op safety net; it normalizes the dialect
  correctly and is correctly scoped. It cannot fix the refusal (no upstream
  retry).
- **Candidate upgrade**: **≥ 1.7.10/1.7.11** (has "unverifiable reasoning
  refusal" retry). Re-run `scripts/verify-fix.mjs` (adapted with dim headers)
  and the live trace against it.
- The user's **downgrade reason** (1.6.11 integration issues with other
  harnesses) should be revisited once the reasoning refusal is confirmed, so
  the version choice can be a single decision rather than a trade-off.

## Lessons (carried over from session 1 — still valid)

1. **Do not leave `reasoning_details` in stream deltas.** Open WebUI suppresses
   the frontend `response.reasoning_text.delta` event when a delta carries
   `reasoning_details` (sets `data=None`, DB-only). Filter v3.5.0 / pipe
   `_clean_stream_delta` strip them.
2. **Never override the user's `reasoning_effort`.** Hard requirement.
3. **Open WebUI executes function code from its DB (Admin → Functions), not
   this repo.** Deploy = re-paste + restart if `stream()` changed.
4. **Do not commit the Bifrost API key.** It lives in `models.json` / session
   env (`BIFROST_*`) and the pi extension config.

## Useful commands

```bash
# Bifrost version
curl -s http://bifrost.private/api/version

# live A/B (see this session's scripts in /tmp or re-derive from README):
#   payload = [system, user, assistant(reasoning_content), tool, user], tools,
#   thinking/reasoning_effort, stream; count reasoning deltas in SSE

# tests (venv created in-repo: .venv, via --without-pip + get-pip.py)
cd /srv/pi/open-webui-extensions
.venv/bin/python -m pytest filters/ pipes/agent_loop_guard/tests/ -q

# Open WebUI source (v0.11.1)
# key files: backend/open_webui/utils/middleware.py (streaming handler, tool loop,
#            get_reasoning_format, process_chat_payload)
#            backend/open_webui/utils/misc.py (convert_output_to_messages)
#            backend/open_webui/utils/filter.py (global filter resolution)
#            backend/open_webui/functions.py (pipe dispatch)

# git (this repo)
git branch --show-current          # debug/reasoning-content-trace
git log --oneline -5
```

## Git / hook notes

- Hook: `.git/hooks/commit-msg` appends `Co-Authored-By: Pi <noreply@pi.dev>`
  unless already present (dedup via `grep -qF`).
- SSH key `~/.ssh/id_ed25519` authenticates as `amartinr` (host key added to
  `known_hosts`). Remote is SSH, not HTTPS.
