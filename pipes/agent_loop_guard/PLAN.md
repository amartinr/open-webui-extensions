# Refactoring Plan: Agent Loop Guard

**Project**: `amartinr/open-webui-extensions`
**File**: `pipes/agent_loop_guard/agent_loop_guard.py` (manifold pipe → LiteLLM gateway → DeepSeek, v2.17.6)
**Status**: the `thinking:disabled` strip bug is **fixed** (v2.17.6, commit b96da58). This document covers the **refactoring only**.

---

## Current state (single file, ~1,300 lines, 4 responsibilities)

| # | Responsibility | Nature |
|---|---|---|
| 1 | **Manifold** | Model discovery (`GET /models`) + HTTP proxy to gateway (SSE streaming, connection pool, header templates) |
| 2 | **Tool guard** | Detects tool-call loops/runaway, replaces tool results, UI notifications, tool blocklist |
| 3 | **DeepSeek Reasoning Chain Fix** | Forces `reasoning_content` on assistant messages; replays real reasoning text; owns the replay patch, its watchdog and the reasoning DEBUG_LOG diagnostics |
| 4 | **Image cleanup** | Dedups/normalizes `<attached_files>` image tags per user message (UUID + content-hash) |

## Key architectural constraint (verified in v0.11.1 source)

- **Inlet filters run once per user turn, NOT on tool-call continuations** — `process_chat_payload` runs once; the tool loop (L5527) reuses the processed `form_data` and rebuilds messages. Only the pipe sees every outbound request, including continuations. This is why all four responsibilities currently live in the pipe.
- **Since v0.11.2**, the filter `request` hook runs on *every* provider call, including tool-call continuations (commit `2daa610`: invoked once in `process_chat_payload` and at both tool-loop sites, receiving `extra_params` incl. `__request__`, `__metadata__`, `__model__`). This enables moving the pure transformations out of the pipe.

## Target architecture (requires Open WebUI ≥ v0.11.2 / target 0.11.3)

```
Pipe (unchanged entry point):
  └─ Manifold          (discovery + transport proxy)
  └─ Tool guard        (loop detection, result replacement, blocklist)

Filter A — request hook:  DeepSeek Reasoning Chain Fix
Filter B — request hook:  Image cleanup
```

| Component | Lives in | Why |
|---|---|---|
| Manifold | **Pipe** | It IS the provider/transport |
| Tool guard | **Pipe** | Needs pipe-only powers: *definitively remove tools from body* and *skip the LLM call / force-terminate* (documented in the pipe's README "Why a Pipe instead of a Filter?") |
| Reasoning fix | **Filter** (`request`) | Pure payload transformation; the `request` hook sees the final payload on every call incl. continuations |
| Image cleanup | **Filter** (`request`) | Pure transformation of user messages; same coverage requirement |

**Ordering** — request filters run BEFORE the pipe (the pipe is the provider), so the effective chain is:

```
image cleanup → reasoning fix (request filters) → tool guard → transport (pipe)
```

The three transformations are pairwise **order-independent**: cleanup touches user messages only, the reasoning fix touches assistant `reasoning_content` only, the guard touches tool-result content only. The move therefore does not change behavior.

## Component ownership after the split

- The **ReasoningChainFix component** (filter) must own, together with the forcing logic:
  - `REPLAY_REASONING_TEXT` valve (currently `Pipe.Valves`, default True)
  - `_install_reasoning_replay_patch()` (idempotent monkey-patch of `middleware.get_reasoning_format`)
  - The replay-effectiveness watchdog (rate-limited warning on silent degradation)
  - The `reasoning_content` DEBUG_LOG diagnostics
- The **ImageCleanup component** (filter) needs `__request__` (base_url for the canonical tag format) and `Files` DB access for content-hash resolution — both available in the `request` hook's `extra_params`.
- **Model scoping**: today the reasoning fix only covers models behind the pipe's manifold. As global `request` filters the components would apply to every model — each needs a `model_pattern` valve (same convention as the other DeepSeek filters in this repo) so non-DeepSeek providers are never touched.

## Phased execution

| Phase | When | Work |
|---|---|---|
| ~~1. Bug fix~~ | ~~DONE (v2.17.6)~~ | ~~Remove the `thinking:disabled` strip (false premise)~~ |
| **2. In-code modularization** | Now (any version) | Split the single file into 4 internal modules (`transport.py`, `tool_guard.py`, `reasoning.py`, `image_cleanup.py`) orchestrated by the `Pipe` class. Version-independent; makes Phase 3 a mechanical move. Its main value: groups the patch + forcing + watchdog under one owner |
| **3. Pipe + filters split** | After upgrading to ≥ v0.11.3 | Move `ReasoningChainFix` and `ImageCleanup` to Filters with the `request` hook; keep Manifold + Tool guard in the pipe |

## Risks / notes

- **Version dependency**: Phase 3 must NOT land before the target Open WebUI version. Verify the `request` hook exists in the target and behaves as documented (runs on every provider call, sees the final outbound payload). Target **0.11.3**: 0.11.2 had a regression (DB-upgrade failure, fixed in 0.11.3 — #29280).
- **0.11.2 changed reasoning internals** ("Thinking stays in Thoughts" fix) — the exact area the replay patch touches. Re-verify the patch against the target version before Phase 3; the watchdog covers silent degradation in the meantime.
- **Regression coverage**: split the pipe's unit tests (`pipes/agent_loop_guard/tests/`) across the components; add tests that reasoning fix and image cleanup run on tool-call continuations once they live in filters.
- **`request` hook ordering**: the two filters touch different message roles, so their relative execution order is irrelevant — keep them explicitly order-independent.
- **Transition / double-run**: while pipe and filters coexist, forcing and cleanup run twice (idempotent, but content-hash DB lookups run twice per request). Cut over in a single release: remove the pipe-side copies in the same change that adds the filters.
- **The tool guard's "skip LLM call" remains pipe-only**: do not attempt to move it to a filter.

## What must NOT be touched (either phase)

| Piece | Reason |
|---|---|
| `_force_reasoning_on_gateway_payload()` / `_force_reasoning_content_on_assistant()` | Real DeepSeek contract: *if the request carries `tools`, the `reasoning_content` must be fully passed back… the API will return a 400 error* |
| `_install_reasoning_replay_patch()` | Replays the real reasoning text ("must be *fully* passed back") |
| Replay-effectiveness watchdog | Depends on the replay |
| `_stream()` SSE filtering | Prevents reasoning-delta corruption in the UI |
| `thinking` passthrough (v2.17.6) | `thinking` is a user control; do not reintroduce any stripping |

## Verification steps

1. ✅ **DONE (v2.17.6)**: `thinking: {"type": "disabled"}` passes through unchanged — covered by `test_payload_forcing_preserves_thinking_control`; gateway verified working with the param (direct curl, streaming and non-streaming, with tools and tool-call history).
2. **Phase 2**: `python3 -m pytest pipes/agent_loop_guard/tests/ -q` — all green after the module split.
3. **Phase 3**: tool-call continuations still get reasoning forcing + image cleanup (now via `request` filters) — integration against LiteLLM + DeepSeek.
4. **Phase 3 regression**: thinking disabled in UI → gateway receives `thinking: disabled` → response has NO `reasoning_content`; thinking enabled → forcing + replay still work (assistant history carries the field).
