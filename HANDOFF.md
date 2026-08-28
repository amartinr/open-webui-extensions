# HANDOFF — Bifrost/DeepSeek reasoning loss (session 3: ROOT CAUSE FOUND in Bifrost core v1.6.3)

> For the next agent picking this up. Read this whole file before touching
> anything. It supersedes the previous HANDOFFs.
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

## TL;DR (session 3 conclusion, updated session 4)

- User runs **Bifrost transport v1.6.3** (embeds core 1.6.3 — verified from
  `core/version` in tag `transports/v1.6.3`). Confirmed live:
  `GET http://bifrost.private/api/version` → `"v1.6.3"`.
- **ROOT CAUSE FOUND** — it is a Bifrost **core v1.6.3** bug, not an Open WebUI
  or extension bug. In `core/providers/openai/chat.go`, DeepSeek is handled in
  the same case as Cerebras/Wafer:
  ```go
  case schemas.Cerebras, schemas.DeepSeek:
      openaiReq.filterOpenAISpecificParameters(capModel)
      openaiReq.stripReasoningDetails()   // sets Reasoning = nil on EVERY assistant message
  ```
  `stripReasoningDetails()` nulls `reasoning_content` on **every** assistant in
  the history, **including tool-call turns**. That violates DeepSeek's own
  contract (reasoning_content must be replayed on tool-call turns). Result:
  on tool-call continuations DeepSeek intermittently refuses to reason and only
  emits the empty opening delta (`reasoning_deltas=1`).
- The fix exists upstream in **core v1.7.10 and v1.7.11** (verified by cloning
  both tags): DeepSeek gets its own case with
  `stripReasoningDetailsExceptToolCalls()`, which preserves reasoning_content on
  assistant tool-call turns (issue **#5887**). Code comment:
  > "DeepSeek is asymmetric: it rejects reasoning_content on ordinary assistant
  > turns, but *requires* it to be replayed on assistant tool_call turns and
  > 400s without it. Stripping both (as Cerebras/Wafer do) forced thinking off
  > for every tool-calling conversation — see issue #5887."
- Previous session's hypotheses H1 (empty reasoning seed) and H3 (broken OWUI
  reconstruction) are **REFUTED** by DB evidence + direct A/B tests. The R0
  flags in the pipe logs were faithful — DeepSeek really did not reason on
  those turns, and the DB stored the real reasoning text (3236 chars) intact.

## Repos / code involved

| Path | What |
|---|---|
| `../pi-bifrost-reasoning-fix` | pi extension (works), v0.2.2 |
| `../open-webui-extensions` | this repo: filter + pipe |
| `../open-webui` | Open WebUI source, tag **v0.11.1** (commit `d3e8bf3`) |
| `../bifrost-core` | Bifrost **core v1.6.3** (the buggy code) |
| `../bifrost-core-1710` | Bifrost core v1.7.10 (has the fix) |
| `../bifrost-core-1711` | Bifrost core v1.7.11 (has the fix) |
| `../bifrost-npx` | GitHub tag `npx/bifrost/v1.6.3` — **DO NOT USE as code ref** (contains core v1.5.11, no DeepSeek provider) |
| `filters/bifrost_reasoning_content_fix` | filter v3.5.0 |
| `pipes/agent_loop_guard` | pipe v2.15.0 (gateway proxy + loop guard + reasoning fix + R0/R{n} trace) |

Deployment: **Open WebUI v0.11.1** → **Bifrost transport 1.6.3**
(`http://bifrost.private/v1`) → **DeepSeek v4 flash/pro**. Workspace models
point at the pipe sub-model (`agent_loop_guard.deepseek/deepseek-v4-flash`);
`base_model_id` on the workspace model is correct.

## Bifrost version / upstream issue map (updated)

| Issue | Date | Version | What | Status |
|---|---|---|---|---|
| #3139 | 2026-04-29 | core 1.6.3 | non-standard `reasoning`/`reasoning_details` dialect for deepseek v4 | closed 07-03 |
| #3802 | 2026-05-27 | 1.5.4–1.5.5 | `reasoning_content` dropped on tool-call turns (`/anthropic`→Responses, Kimi); regression of #2093/#2284 | open |
| #4861 | 2026-06 | core 1.6.3 | convert thinking to disabled when tool_choice is required (DeepSeek) — only fires on `tool_choice:"required"`, not our case | fixed |
| #5325 | 2026-07-17 | — | reasoning exposed in Bifrost-specific fields (the "dialect") | open |
| #5887 | 2026-08 | core 1.7.10 | **DeepSeek asymmetric reasoning contract — `stripReasoningDetailsExceptToolCalls`** ← THE FIX | released |
| #6111 | 2026-08-13 | 1.6.10 | DeepSeek 400 "`reasoning_content` … must be passed back" (opencode path) | open |
| #6523 | 2026-08-25 | — | streaming drops opening role-only delta | open |

**The core v1.6.3 bug**: `stripReasoningDetails()` (called for DeepSeek) wipes
`reasoning_content` from ALL assistant history messages, including tool-call
turns → DeepSeek intermittently stops reasoning on tool-call continuations.
**core v1.7.10+**: DeepSeek case split off; tool-call turns keep their
reasoning. The v1.7.10/1.7.11 "retry after unverifiable reasoning refusal"
changes from the changelog are a related hardening, but the mechanism that
matches our symptom exactly is the `stripReasoningDetailsExceptToolCalls` fix.

## Transport ↔ core version mapping (from `core/version` in each tag — authoritative)

| Transport tag (`/api/version` reports this) | Embedded core (`core/version`) | Notes |
|---|---|---|
| `transports/v1.6.3` (current) | **1.6.3** | has the bug |
| `transports/v1.6.11` (user's previous) | **1.7.10** | **has the fix** — this is the recommended re-upgrade |
| `transports/v2.0.0-prerelease1` | 1.7.0 | — |
| `transports/v2.0.0-prerelease2` | 1.7.2 | — |
| `transports/v2.0.0-prerelease3` | 1.7.11 | has the fix |
| `transports/v2.0.0` | 1.8.3 | has the fix (bigger jump, not needed for this bug) |

How to read the mapping (no Docker needed, repo is already cloned at
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
`high` effort. Pi does not exhibit it (pi sends reasoning verbatim and does
not go through the same tool-call replay path against Bifrost).

## Empirical evidence (session 3, direct A/B against bifrost.private/v1)

Scripts live in `/tmp/repro*.mjs` (repro.mjs: 3 replay shapes; repro2.mjs:
content/details/empty/absent; repro3.mjs: plain/toolhistory/toolsrequested;
repro4.mjs: replay forms ×8 rounds). Key results, `reasoning_deltas` counts:

- **plain** (no tools): 390–947 deltas, 0 drops in 6/6 → DeepSeek low ALWAYS reasons normally.
- **toolsrequested** (tools present, no prior tool call): 46–97 deltas, 0 drops in 6/6.
- **toolhistory** (history contains an assistant tool call): intermittent drops —
  `reasoning_deltas=1` in ~10–15% of rounds; even non-dropped rounds reason far
  less (10–33 deltas vs 400–900).
- The `reasoning_deltas=1` signature is the empty opening delta:
  `{"reasoning":"","reasoning_details":[{"text":""}]}`.
- Drops occur identically whether the replayed assistant carries
  `reasoning_content` (with text), `reasoning_details`, both, or neither →
  the extension's normalization cannot fix this; Bifrost wipes the field anyway.

Conclusion: the drop is **specific to tool-call continuation turns**, exactly
where core v1.6.3's `stripReasoningDetails()` violates DeepSeek's contract.

## DB evidence (user's live instance, chat "Hola")

- `history.messages` is a keyed object; assistants have no flat
  `reasoning_content` (v0.11.1 stores reasoning as `output` items of type
  `reasoning`).
- The two early assistants have `output: [message]` only — no reasoning item,
  faithful (DeepSeek didn't reason on those turns).
- The reasoning assistant (`3c00e72f…`) has 5 `reasoning` items totalling
  **3236 chars** of real text — stored intact.
- ⇒ H1 (empty reasoning seed) and H3 (broken reconstruction) refuted; the R0s
  in pipe logs were faithful.

## Why the extension's reasoning replay cannot fix this

| Concern | pi | Open WebUI + extensions |
|---|---|---|
| Replay reasoning | verbatim `reasoning` → `reasoning_content` | reconstructed from OR `output` + monkey-patch (`get_reasoning_format`) |
| Effect under Bifrost 1.6.3 | Bifrost wipes `reasoning_content` from history anyway (core bug) | same |
| Tool-call continuation | `before_provider_request` hook | filter `inlet` (user turns) + pipe `pipe()` (choke point) |
| Stream dialect | SDK parses `reasoning`/`reasoning_details` natively | must normalize deltas + filter SSE noise |
| `thinking` control | user maps `reasoning_effort` only | user's filters set `thinking:{type:enabled/disabled}` + `reasoning_effort` |

## Secondary bug found (still real, unrelated to root cause)

`_normalize_thinking_for_gateway` strips `thinking:{type:"disabled"}` on the
assumption that Open WebUI injects it on tool-call continuations. Verified in
v0.11.1 source: OWUI never emits `thinking` and does not drop
`reasoning_effort`. The `thinking:disabled` the pipe sees is the user's own
`deepseek_thinking_default_off` filter → with the reasoning chip OFF and a
tool call, the pipe re-enables reasoning against the user's intent. Fix:
remove the strip or gate behind an opt-in valve (default off).

## What was done this session (3)

- Reproduced the drop **directly against Bifrost 1.6.3** with tool-call
  continuation payloads (`/tmp/repro*.mjs`) — disproving the previous
  "doesn't reproduce via API" claim (previous tests didn't include the
  tool-call-continuation + thinking+effort combination).
- Cloned Bifrost core v1.6.3, v1.7.10, v1.7.11 and the (misleading) npx
  transport tag. Found the exact bug and the exact fix with line-level diff.
- Fetched all tags in the existing `/srv/pi/bifrost-npx` clone and read
  `core/version` per transport tag — established the authoritative
  transport↔core map (v1.6.11 → core 1.7.10, v2.0.0 → core 1.8.3).
- Confirmed via the user's DB that reasoning storage in OWUI is intact.
- Ran unit tests: filter 9/9; pipe 8/8 (via `python3 -m pytest`; in-repo
  `.venv` no longer exists).

## Next steps

1. **Re-upgrade to `transports/v1.6.11`** (embeds core 1.7.10, has the fix;
   user has already done this). Re-run `node /tmp/repro3.mjs 10 toolhistory`
   and the live trace — the drops should disappear on tool-call
   continuations. (The user's earlier downgrade from 1.6.11 to 1.6.3 was for
   harness-integration reasons; 1.6.11 also has the reasoning fix, so both
   concerns should now be re-tested together.)
2. **Revisit the downgrade reason** (1.6.11 integration issues with other
   harnesses) — now that 1.6.11 is re-deployed, confirm whether those issues
   persist; they are unrelated to the reasoning fix.
3. Keep the extensions as a safety net (dialect normalization + forcing are
   correct and harmless); they just cannot fix the core wipe.
4. Optional: fix the secondary chip-off bug (strip `thinking:disabled` only
   when opt-in).
5. The `forced` counter fix (flag `reasoning_content==""`) is **no longer
   needed** — H1 is refuted; the R0/R{n} trace already covers visibility.
6. Commit `patches/openwebui-29052-middleware.patch` (currently untracked)
   and this updated HANDOFF.

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
   and the stale `npx/bifrost/v*` tags are both misleading (the npx v1.6.3
   tag contains core v1.5.11).

## Useful commands

```bash
# Bifrost version (transport)
curl -s http://bifrost.private/api/version

# live A/B tool-call-continuation repro (root cause demo)
node /tmp/repro3.mjs 10 toolhistory   # expect intermittent reasoning_deltas=1 on 1.6.3
node /tmp/repro3.mjs 6                # plain vs toolhistory vs toolsrequested
node /tmp/repro4.mjs 8                # replay forms on tool-call continuation

# key Bifrost code
# bug:  /srv/pi/bifrost-core/core/providers/openai/chat.go:70-73 (case Cerebras,DeepSeek; stripReasoningDetails)
# fix:  /srv/pi/bifrost-core-1711/core/providers/openai/chat.go:78-84 + 232-244 (stripReasoningDetailsExceptToolCalls)
# issue #5887 comment embedded in the 1.7.10+ source

# tests
cd /srv/pi/open-webui-extensions
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q

# Open WebUI source (v0.11.1): backend/open_webui/utils/{middleware,misc,filter}.py,
# backend/open_webui/functions.py, backend/open_webui/models/chats.py
```

## Git / hook notes

- Hook: `.git/hooks/commit-msg` appends `Co-Authored-By: Pi <noreply@pi.dev>`
  unless already present (dedup via `grep -qF`).
- SSH key `~/.ssh/id_ed25519` authenticates as `amartinr` (host key in
  `known_hosts`). Remote is SSH, not HTTPS.
