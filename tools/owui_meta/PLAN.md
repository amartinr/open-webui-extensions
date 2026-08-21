# Implementation Plan — owui_meta (Open WebUI Meta-Tool)

**Branch:** `feat/owui_meta_tool`
**Date:** 2026-08-01
**Status:** Iterations 0, 1, 3, 4, 5, 6, 7 and 8 **done** (Iteration 5 completed 2026-08-20: live suite + isolation tests committed; Iteration 8 completed 2026-08-20: items 1–7); Iteration 2 **deferred to a future version**; **Iteration 9 — live + source research completed 2026-08-21 (Tasks 1 and 3 corrected: `tag:` is already a scope limiter server-side; the stats anomaly is fully root-caused — a backend bug in the assistant-length metric); tasks 9.1, 9.2, 9.3, 9.4, 9.7, 9.8, 9.9 and 9.10 DONE (v0.17.0–v0.23.0), 9.5 pending** (see the Iteration 9 section below; **task 9.6 chat date-range filter DEFERRED by user decision 2026-08-21** — see §7 Future versions) — design decisions 2026-08-21
**Notes merged (2026-08-21):** `NOTES.md` was absorbed into this plan — N1 (`tag` × scope) and N2 (per-scope `limit` semantics) are now design notes under task 9.9; N3 (residual `_my_` names) under task 9.10; N4 (Iteration 9 status snapshot) is the **Status** line above. The standalone file was replaced by a pointer stub.

This plan turns [DESIGN.md](./DESIGN.md) into a working Open WebUI tool one iteration at a time. The guiding rule: **every iteration ends with a working, testable, committable product** — never a half-wired feature.

---

## 1. Conventions

| Rule | Value |
|---|---|
| Commit format | Conventional Commits with the tool directory as scope: `type(owui_meta): subject` (e.g. `feat(owui_meta): add read-only MVP tool`) — same style as `smart_fetch_url` / `image_filter` commits in this repo |
| Language | Code, comments, tests and documentation are written in **English** |
| One commit per iteration | Each iteration ships exactly one commit (docs and tests ride along with the feature) |
| Module file | `owui_meta.py` (same naming rule as `smart_fetch_url.py`) |
| Versioning | `version:` field in the module header is bumped every iteration (0.1.0 → 1.0.0) |
| Dependencies | stdlib + `httpx` only (bundled with Open WebUI); tests use `pytest` + `pytest-asyncio` (`asyncio_mode = auto`) |
| Docstrings (Open WebUI contract) | reST `:param name:` directives, one per signature parameter, **each description on a single line** — OWUI v0.10.2 parses per line (`parse_docstring`), continuation lines are dropped from the param and leak into the function description. `__request__`/`__*` params are never documented (harness-injected). Enforced by `test/test_docstrings.py` |

## 2. Security invariants (enforced in every iteration, verified by tests)

1. **No credentials configured or stored** — auth always comes from `__request__.state.token`, extracted by Open WebUI's `AuthTokenMiddleware`. A missing token yields a clear error, never a silent call.
2. **The token is never logged** and never appears in anything returned to the model. (Note: `GET /api/v1/auths/` echoes the request token in its body — the profile method field-whitelists it away; pinned by `test_profile_token_echo_never_reaches_model`.)
   **2026-08-01 audit of all allowlisted endpoints**: no other token echoes found (chats/models/files/prompts/tools/knowledge/skills carry no credentials). Defense in depth: `get_chat` and `get_skill` were also whitelisted (they were the only raw pass-throughs left in json mode — `get_skill` would have leaked the owner's email for shared skills). Secret-bearing GETs are explicitly never-allowed (DESIGN §6.3): `auths/api_key`, `tools/id/{id}/valves(+/user)`, `tools/id/{id}`, `knowledge/external/connections*`, `*/admin/*` configs.
   **Output-boundary guards (hardening, same day)**: whitelists are per-method/manual, so two guards run at the output boundary against *accidental* leaks (a future method, or a future server field echoing a credential): `_sanitize` (drops credential-named keys with string values; boolean `api_keys`-style flags kept) and `_run` redacting the raw token string from any output. Pinned by `test/test_security.py` (sanitizer, redaction, error path, static no-raw-body tripwire).
3. **No arbitrary URL calls** — methods resolve to an internal allowlist of typed routes; no URL-taking parameter exists (no SSRF).
4. **`Content-Type` validated** — the SPA HTML catch-all returns HTTP 200, so only `application/json` (or explicitly allowed binary types for file content) is trusted.
5. **Mapped errors** — 401/403/404/5xx become readable messages; 404 reads "does not exist or is not yours" (no existence leak).
6. **Role gate before HTTP** — admin-only methods check `__user__.role` *before* making any request; a non-admin call is refused without touching the network.
7. **Truncation** — responses are capped by `max_response_chars` before reaching the model.

## 3. Validation loop (each iteration)

1. `python3 -m pytest` — all tests green.
2. Import check — `python3 -c "import owui_meta"` succeeds *outside* Open WebUI (all `open_webui.*` imports are defensive/optional).
3. Schema check — `owui_meta.Tools().Valves()` builds (pydantic) and every method signature declares only allowlisted, typed parameters.
4. Confirm no token/URL/log leakage by inspection of the diff.

---

## Iteration 0 — Baseline (docs + scaffolding) ✅ DONE (commit `fd2d087`)

**Commit:** `docs(owui_meta): add design document, implementation plan and scaffolding`

- Track the existing `DESIGN.md` (currently untracked).
- Add `PLAN.md` (this document).
- Add `README.md` (brief skeleton; finalized in Iteration 5).
- Add `LICENSE` (MIT, same as the repository root).
- Add `pytest.ini` (`asyncio_mode = auto`, matching the `smart_fetch_url` suite).

**Definition of done:** the directory is self-contained and documented; no runtime code yet.

## Iteration 1 — MVP: core engine + read-only methods (user role) ✅ DONE (commit `e8da0d1`)

**Commit:** `feat(owui_meta): add read-only MVP tool with automatic token auth`

Implements the Phase 1 MVP from DESIGN §6.1 / §8.

- `owui_meta.py` with the standard Open WebUI tool header (`title`, `author`, `description`, `required_open_webui_version`, `requirements: httpx`, `version: 0.1.0`, `licence: MIT`).
- `Tools.Valves`: `fallback_base_url`, `timeout`, `max_response_chars` (DESIGN §8.2) — **no credential valves by design**.
- **Base URL resolution** (§4.2): `Config.get('webui.url')` (defensive import of `open_webui.models.config`) → `WEBUI_URL` env var → valve. On `RequestError` (DNS/connection/timeout) retry once against the internal fallback URL (§4.3); never retry on API 4xx/5xx.
- **Token extraction** (§3.1): read `__request__.state.token`; explicit error message if absent.
- **Core engine** `_api_get()`: `httpx.AsyncClient` GET with `Authorization: Bearer <token>`, timeout, `Content-Type` validation, error mapping, truncation, canonical route paths (correct trailing slashes — §9 lesson 8).
- **Read-only methods (§6.1, all user-role):**
  `get_my_profile`, `get_models`, `get_my_chats`, `get_chat`, `search_chats`, `get_shared_chats`, `get_pinned_chats`, `get_my_files`, `get_file_content`, `get_my_prompts`, `get_my_tools`, `get_knowledge_bases`.
- **Tests** (`test/test_engine.py`, `test/test_user_methods.py`, `test/helpers.py`): mocked `httpx.MockTransport` — token forwarding, missing-token error, `Content-Type` validation (SPA HTML trap), error mapping (401/403/404/5xx), truncation, RequestError fallback retry, and route/slash correctness per method.

**Definition of done:** the tool is installable and every listed method returns correct, truncated, token-authenticated data against the allowlist — proven by green tests with a mocked backend.

**Verified:** 31 tests pass (`test_engine.py`, `test_user_methods.py`); `import owui_meta` works outside Open WebUI (`Config` stays `None`); Valves build; all 12 method signatures expose only allowlisted typed parameters.

**Post-iteration fix (2026-08-01, `fix(owui_meta): read token from HTTPAuthorizationCredentials`):** live testing against a real authenticated session showed all methods failing with “No authentication token available”. Root cause verified in the v0.10.2 source: `AuthTokenMiddleware` stores an `HTTPAuthorizationCredentials` **object** (`scheme`/`credentials`) in `request.state.token`, not a string. `_require_token` now reads `.credentials` (accepting a plain string for robustness), DESIGN §3.1 corrected, regression test added (`test_token_from_http_authorization_credentials_object`), module version → 0.1.1.

**Second post-iteration fix (2026-08-01, `fix(owui_meta): await async Config.get`):** live test then failed with `AttributeError: 'coroutine' object has no attribute 'rstrip'` in `_resolve_base_url`. Root cause: `Config.get` is **async** in v0.10.2 (`async def get(key, default=None)` in `open_webui/models/config.py`) and was called without `await`. `_resolve_base_url` is now a coroutine that awaits it (handling non-string values and store failures by falling through to env var / valve), DESIGN §4.1 corrected, regression tests added (`test_base_url_from_admin_config*`), module version → 0.1.2.

**Pending follow-ups (tracked, not blocking):**
- `get_my_chats`/`search_chats`/`get_shared_chats`/`get_pinned_chats` responses are assumed to be a bare array (v0.10.2 curl evidence) — re-validate the exact shape when live-testing against the instance in Iteration 5, and adjust `_extract_items` if they return `{items, total}`.
- `get_chat()` returns the full history and relies on `max_response_chars` truncation — revisit summarization (top messages + total) in Iteration 3.
- Confirm `GET /api/v1/chats/search?text=` (verified as the parameter by 422; full response format pending confirmation).

**Third post-iteration fix (2026-08-01, `fix(owui_meta): use canonical trailing-slash routes (verified live)`):** live test failed with “Expected JSON … got 'text/html' (HTTP 200)” on `get_my_profile`. Root cause (verified against the v0.10.2 source + live curl): the **listing routes** (`/api/v1/auths/`, `/api/v1/chats/`, `/api/v1/files/`, `/api/v1/prompts/`, `/api/v1/tools/`, `/api/v1/knowledge/`, `/api/v1/users/`) require a **trailing slash** — without it the request falls into the SPA HTML catch-all (HTTP 200, `text/html`). Sub-resources (`/api/v1/chats/search`, `/pinned`, `/shared`, `/api/v1/chats/{id}`, `/api/v1/files/{id}/content`) and `/api/models` must **NOT** have a slash. The route constants were corrected to the canonical map, `_api_get_json` gained `allow_ndjson` (for `/api/v1/chats/all`, which returns `application/x-ndjson`), a new regression suite pins the full route map (`test/test_route_map.py`), DESIGN §5.1/§5.3/§9.8 updated, module version → 0.2.0.

## Iteration 2 — Admin-only methods with role gate ⏸️ DEFERRED to a future version (2026-08-01)

**Commit:** *(none — not implemented)*

**Decision:** the user decided **not to implement admin methods for now**; they are moved to a future version of the tool. This iteration is removed from the current scope.

- Methods from §6.2: `list_users`, `get_user`, `list_all_chats`, `get_admin_config`.
- Role check `__user__.get('role') == 'admin'` **before any HTTP call**; non-admin (or missing `__user__`) → explicit refusal, no request issued, no information leak.
- **Tests** (`test/test_admin_methods.py`): user role blocked with transport asserting zero requests; admin role proceeds; missing `__user__` refused.

**Definition of done (when picked up again):** regular users get a clean refusal; admins get data; both proven by tests.

**Tracked as future work:** see “Future versions” in this document.

## Iteration 3 — Pagination, sorting and typed filters ✅ DONE (commit `33af0d0`)

**Commit:** `feat(owui_meta): add pagination, sorting and typed filters`

Implements DESIGN §8.6 across the list methods:

- **Transparent page iteration** (`_fetch_all_pages`): iterates pages up to `MAX_PAGES` (5) until the server's declared `total` is reached or a short page (fewer items than `page_size`) is returned; `page_size = min(max(limit, 20), 50)`.
- **Client-side sorting** (the API does not expose sort params):
  - Files: `size`, `created_at`, `filename` (`sort_by`, default `created_at`).
  - Chats: `updated_at`, `created_at` (`sort_by`, default `updated_at`).
  - `sort_order`: `asc` / `desc` (default `desc`). Invalid `sort_by` falls back to the default.
- **Client-side typed filters** (`_filter_files`, conjunctive, all optional):
  - Files: `content_type` (exact or `image/*` wildcard), `min_size` / `max_size` (bytes), `filename` fragment (case-insensitive).
- **Summarized output**: lists returned to the model are summarized (top `limit` + counts) and bounded by `max_response_chars`; the Markdown header reports `matched` vs `total on server` and `(showing top N)` when truncated.
- **Tests** (`test/test_pagination.py`): multi-page iteration (104 → 3 pages), short-page stop, MAX_PAGES cap, file sorting (size/name), chat sorting, wildcard/filter/range filters, matched count, invalid sort fallback, markdown header.

**Definition of done:** a 100+ item dataset is queryable with filtering and sorting in bounded output — proven by tests (73 total green).

## Iteration 4 — Status events (UX) ✅ DONE (commit `3a04a52`)

**Commit:** `feat(owui_meta): emit status events during execution`

- `__event_emitter__` statuses on start ("Reading your profile…", "Querying your chats…", "Listing your files…"…), completion (same action, `done=True`) and failure — DESIGN §8.5, matching the `smart_fetch_url` UX pattern.
- `verbose` valve (admin + per-user, default `True`; per-user overrides admin) to toggle progress events.
- **Errors are `chat:message:error`** (the error block rendered in the message), **always shown regardless of `verbose`**, and **consolidated**: at most one error event per tool call — a batch `delete_files` with several failures emits a single "N of M file(s) could not be deleted" summary, never one toast per file.
- Events never contain the token.
- **Tests** (`test/test_events.py`, 8): start+done sequence, no-emitter no-op, `verbose=False` suppresses status (but not errors), per-user `verbose` override, failure emits single error, batch failures emit single consolidated error, no token in any event.

**Definition of done:** tool execution is visible in the UI; error events are consolidated (never flooding); event payloads are token-free (tested).

## Iteration 5 — Live validation & isolation tests ✅ DONE (2026-08-20)

**Commit:** `test(owui_meta): add live integration and isolation tests`

- Env-gated live suite (`test/test_live.py`, 18 tests; skipped unless `OWUI_META_LIVE_URL` / `OWUI_META_LIVE_TOKEN` are set, e.g. `source /tmp/owui_live.env`) re-validating the §5 endpoint map against the real instance: profile (the `/auths/` token echo never reaches the model), models, chats list + `search?text=` with UI prefixes (`tag:`, `pinned:`, `archived:`, `tag:none`), chat metadata/summary (no message content in metadata), tags (no `user_id` leak), archived chats, folders (or readable 403), `stats/usage` (the pageSize-ignored quirk asserted on live data), files + content snippet, workspace resources (prompts, tools, knowledge, skills), shared/pinned. Traps only live data exposes: SPA-HTML catch-all (no-slash variants never return JSON — today nginx answers 200 text/html or 301), and `/api/v1/users/` blocked for a user role (401/403, isolation). Output-boundary guard on real data: the token never appears in any method's output.
- **Isolation tests** (`test/test_isolation.py`): mock-level — each request sends its own token (no caching across calls), two interleaved `Tools` instances share no state; live-level — every file returned for a token belongs to that token's `user_id` (single user), and with a second real token (`OWUI_META_LIVE_TOKEN2`) the two users' file sets are disjoint (skipped when the second token is absent).
- **Personal data hygiene:** all mock profile data now uses fictitious values (`John Doe` / `john.doe@example.com`); the real token lives only in the environment, never in the repo.
- **Infrastructure hygiene:** the internal hostname (and its DNS evidence) is not referenced in the repo anymore — live tests resolve the instance exclusively from `OWUI_META_LIVE_URL`; mock tests use a fictitious placeholder base URL (`http://webui.example.test`).

**Findings from the first live run (fixed in the suite, not the tool):**
- `/api/v1/files` (no slash) no longer returns SPA 200 but **301 from nginx** — the invariant pinned is “never JSON”, not “always 200 HTML”.
- `search_chats("archived:true")` returns 0 results → markdown renders `(none)` without a table; the suite tolerates empty results.
- `tag:none` returns ~60 chats → the response exceeds `max_response_chars` and is truncated; the suite uses markdown mode and tolerates truncation (expected behavior).

**Definition of done:** live suite passes against the instance — 148 passed / 1 skipped (second-user test needs `OWUI_META_LIVE_TOKEN2`); without the env the suite is cleanly skipped (131 passed / 18 skipped).

## Iteration 6 — Markdown-first output (pulled forward per user request) ✅ DONE (commits `3b27b22`, `e5a10ab`, `436ead7`, `2823c35`)

**Commit:** `feat(owui_meta): return markdown-first output for the model (DESIGN §8.8)`

Implements DESIGN §8.8 (decision 2026-08-01), pulled ahead of Iterations 2–5 at the user's request because the live agent output revealed the tool was still returning JSON.

- New valve `output_format: "markdown" | "json"` — implemented as a **per-user valve** (dropdown Markdown/JSON, default `markdown`; there is **no admin valve** for the format). Each user chooses the format for their own chats.
- Markdown renderers per resource: lists → tables, profile → bullets, chat → heading + per-message blocks, file text → fenced block with language hint, file binary → metadata note, errors → plain-text `Error: …` one-liners.
- **Raw numeric values** (byte sizes passed through unformatted — `8796`, no `KB` prefix), readable UTC dates, **IDs always present** in tables for follow-up calls.
- `_error` returns `Error: <message>` in markdown / `{"error": …}` in json; `_ok` dispatches by kind.
- Nested objects (profile `permissions`, multimodal chat content) render as **indented bullet hierarchies** with humanized keys — never embedded JSON (research-backed JSON→MD strategy).
- **Bold restricted to headings** (token saving): keys in hierarchies/tables are plain.
- Tests: `test/test_output_format.py` (markdown rendering, raw bytes, no token, json mode still works); existing suites updated to opt into `output_format="json"` where they parse structured output.

**Follow-up refinements within this iteration:**
- `e5a10ab` — `feat(owui_meta): render nested objects as hierarchical markdown, no embedded JSON`: nested objects (profile `permissions`, multimodal chat content) were still emitted as raw JSON inside a bullet; added `_md_hierarchy` (research-backed JSON→MD strategies, llm-md) — bullets for shallow objects, tables for uniform arrays, indented hierarchy for deep nesting, humanized keys, **never raw JSON**.
- `436ead7` — `perf(owui_meta): restrict markdown bold to headings to save tokens`: removed `**…**` from hierarchy keys and table cells; bold kept only on headings/sections.
- `2823c35` — `feat(owui_meta): per-user output_format valve (markdown/json dropdown)`: `output_format` became a **per-user valve** (dropdown Markdown/JSON, default `markdown`; no admin valve, no “inherit” option — the tool's built-in default is Markdown).
- DESIGN §8.8 and README updated to document all refinements.

**Definition of done:** the tool returns readable Markdown by default (tables/bullets), JSON remains available via valve — 56 tests green.

## Iteration 7 — Skills endpoints (query-only) ✅ DONE (commit `1deaa15`)

**Commit:** `feat(owui_meta): add skills endpoints (get_my_skills, get_skill)`

Implements DESIGN §6.1 for skills — the workspace resource family (tools/prompts) was missing it.

- `_ROUTE_SKILLS = "/api/v1/skills/"` (listing, **trailing slash** — verified live 2026-08-01: `/api/v1/skills/` → 401 JSON, `/api/v1/skills` → 200 SPA HTML) and `_ROUTE_SKILL = "/api/v1/skills/id/{skill_id}"` (sub-resource, **no** trailing slash).
- `get_my_skills()` — lists the user's skills (own + shared via access grants): name, description, active state, id. The skill's `content` (its instructions) is **not** dumped in the listing (kept light; use `get_skill()` for the full detail).
- `get_skill(skill_id)` — full detail of one skill including `content` (fenced block in markdown) and `meta` (hierarchy), with the standard `_ID_RE` validation (no path smuggling).
- **Query-only scope (user decision 2026-08-01):** `/api/v1/skills/export` is a GET but is **not** exposed — v1 has no export/import (DESIGN §2/§6.3, PLAN §8).
- **Tests:** route map (slash map for skills), user methods (summarization without content, full detail, invalid id rejected without request), output format (skills table, single skill detail, raw bools, readable dates).
- **Docs:** DESIGN §5.1/§6.1, README, this plan.

**Definition of done:** skills are queryable like any other workspace resource — 78 tests green (73 → 78).

---

## 6. Modifications made on the fly (deviations from the original plan)

These changes were not in the initial plan; they emerged during development / live validation and were folded in as they happened.

| # | When | Change | Why | Commit(s) |
|---|---|---|---|---|
| 1 | After Iter 1 (live test) | **Token is an `HTTPAuthorizationCredentials` object, not a str** — `_require_token` reads `.credentials` | v0.10.2 `AuthTokenMiddleware` stores the object in `request.state.token`; the original DESIGN §3.1 assumed a string. Live session returned “No authentication token available”. | `07306c8` |
| 2 | After Iter 1 (live test) | **`Config.get` is async** — `_resolve_base_url` became a coroutine that awaits it | v0.10.2 `Config.get(key, default=None)` is `async`; calling it without `await` raised `'coroutine' object has no attribute 'rstrip'`. | `db8c1cd` |
| 3 | After Iter 1 (live test) | **Canonical trailing-slash route map** — listings need `/`, sub-resources and `/api/models` must not | v0.10.2 registers listing routes with a trailing slash; without it the SPA HTML catch-all returns 200. The original plan had several routes with the wrong slash. Also added `allow_ndjson` for `/api/v1/chats/all`. | `27b5ffc` |
| 4 | Design decision (user) | **Markdown-first output** — new `output_format` valve, renderers per resource; pulled forward as Iteration 6 ahead of Iterations 2–5 | The user observed agents read plain text / tables better than JSON; the live agent output revealed the tool still returned JSON. DESIGN §8.8 added. | `f6ff876` (design), `3b27b22` |
| 5 | Design decision (user) | **No embedded JSON in Markdown** — nested objects render as indented bullet hierarchies (research-backed, llm-md) | `permissions` was dumped as raw JSON inside a bullet; user asked for hierarchical formatting. | `e5a10ab` |
| 6 | Design decision (user) | **Bold restricted to headings** — keys/cells plain | Save tokens; `**…**` in every key wasted ~160 tokens per `permissions` dump. | `436ead7` |
| 7 | During Iter 1 | **Docstring contract** — reST `:param` single-line, `__*` never documented, enforced by `test/test_docstrings.py` (replicates the v0.10.2 parsers verbatim) | User asked whether docstrings were formatted as Open WebUI expects; verified against source and pinned with a regression test. | `f34b4e4`, `9c91a3f` |
| 8 | Design decision (user) | **`output_format` became a per-user valve** (dropdown Markdown/JSON, default `markdown`; **no admin valve**) — user chooses the format per session | The agent's research showed no universal format winner (depends on model/task); letting each user choose is more pragmatic than an admin global. Initial “inherit/System default” option removed since there is no admin valve to inherit from. | `3b27b22` (initial), `2823c35` (per-user) |
| 10 | Scope decision (user) | **Admin-only methods deferred** (Iteration 2 removed from current scope) | The user decided not to implement admin methods for now; moved to a future version. Tracked in “Future versions”. | — |
| 11 | Security review (user question) | **Profile endpoint echoes the token — field whitelist added** | `GET /api/v1/auths/` (`get_session_user`, v0.10.2) returns `token`/`token_type`/`expires_at` in its body (frontend session refresh). `get_my_profile` previously passed the raw body to `_ok`, so **json mode dumped the user's session credential into the model context**. Now `_get_my_profile` builds an explicit field whitelist; token fields never serialize, in either format. Regression test: `test_profile_token_echo_never_reaches_model`. | `49727a5` |
| 12 | Security audit (user request) | **Whitelisted the last raw pass-throughs + documented secret-bearing GETs as never-allowed** | Audited every allowlisted endpoint against v0.10.2 source: no other token echoes. `get_chat` (json) dumped bookkeeping noise (`user_id`, `meta`, `tasks`, `summary`, `folder_id`) and `get_skill` (json) embedded the owner's `UserResponse` (another user's email for shared skills) — both now whitelisted. Also pinned in DESIGN §6.3: `auths/api_key`, `tools/id/{id}/valves(+/user)`, `tools/id/{id}`, `knowledge/external/connections*`, `*/admin/*` configs are **never** to be added to the allowlist (they return credentials). Tests: `test_get_skill_strips_owner_and_bookkeeping`, `test_get_chat_strips_bookkeeping_fields`. | `1ce2da9` |
| 13 | Hardening (user question: “¿implementado?”) | **Output-boundary guards against accidental leaks** | Whitelists are per-method/manual; a future method or server field could leak. Added two structural guards: `_sanitize` (drops credential-named keys with string values; boolean flags like `api_keys` kept) applied in `_ok` for both formats, and `_run` redacting the raw token string from success and error output (`request=__request__` threaded through all 14 wrappers). Plus `test/test_security.py`: sanitizer unit, future-raw-pass-through simulation, token redaction inside a whitelisted field, redaction in the error path, static no-raw-body tripwire. | `9ae1506` |
| 9 | Live validation | **Manual live tests** against the instance using a real (non-persisted) token: confirmed the route map, `/users` blocked for user role, `search?text=` format, NDJSON for `chats/all` | The live suite is now committed as Iteration 5 (2026-08-20): `test/test_live.py` + `test/test_isolation.py`; these were the original one-off curl checks whose findings were folded into the fixes above. | — (findings in commits 1–3) |
| 14 | Design decision (user) | **`get_file_content` attaches files to the UI via the native `files` event** — image → inline preview + download, text → 100-char snippet (no dump), binary → note. Best-effort metadata call to `GET /api/v1/files/{id}` (added to the allowlist) for the attachment display name; best-effort `_emit_files` so a dead UI socket never breaks the tool call. Read opt-in (`max_chars`/`offset`) deferred — see §7. | Verified against Open WebUI source: the `files` event is persisted by `socket/main.py` and rendered by `ResponseMessage.svelte`/`FileItem.svelte`/`Image.svelte`; the item schema is pinned by `test/test_file_attachment.py`. See §7 design note. | `5865848` |
| 15 | Design decision (user) | **File cleanup: `delete_files(file_ids)`** — the first write operation, an explicit exception to read-only v1. Accepts a list of ids (whole list validated up front; dedup; capped by `MAX_DELETE_FILES`); per file: GET metadata first (report what disappears; a 404 never reaches the DELETE), then `DELETE /api/v1/files/{id}` (backend re-verifies owner/admin/write, cleans KB + vector index). Per-file failures are reported by id without aborting the rest. A dedicated orphan-list method was considered then dropped by user decision (the model derives orphans from `get_my_files()` `origin_chat_id` + `get_my_chats()`). HTTP engine generalized (`_fetch`/`_fetch_with_retry` take a method; new `_api_delete_json`). | Open WebUI keeps chat-attached files when a chat is deleted (verified in v0.10.2 `chats.py`: `delete_chat_by_id` never touches `Files`) — the orphan-cleanup case. See §7 design note. | `02395a5` |
| 16 | Design decision (user) | **Iteration 4 status events + error consolidation** — `status` events (start/done) gated by a `verbose` valve (admin + per-user, per-user overrides); errors are `chat:message:error`, always shown, and consolidated to ONE per tool call (batch delete failures → single "N of M failed" summary). Logs explicitly parked by user decision (2026-08-03). | smart_fetch_url UX pattern (DESIGN §8.5); the user asked to reserve error visibility for real errors and never flood the user with repeated toasts. See §7 design note. | `3a04a52` |
| 17 | Design decision (user, "Opción B") | **Images embedded via `embeds` (HTML) instead of `files`** — `get_file_content` emits `{"type":"embeds",...}` with an HTML `<img>` fragment for images (rendered inline in the message by `FullHeightIframe` srcdoc, like a snippet); text/binaries keep the `files` attachment. The returned note is anti-markdown (mirrors `generate_image`: the model must not embed/display the image again). `_emit_embeds` + `_image_embed_html` helpers added. | The user reported the image appeared "as markdown" (the model wrote it from our note); generate_image (files event) works, but the user chose the HTML-embed strategy over the anti-MD note alone. Verified `embeds` is a native persisted event in v0.10.2 (socket/main.py + Chat.svelte + ResponseMessage.svelte). See §7 design note. | `95ff280` |
| 18 | Docs (2026-08-03) | **Multi-embed rendering note** — record that N embeds stack vertically (each `my-2 w-full`), so a future gallery must emit ONE embed wrapping several `<img>`. | Verified in v0.10.2 `ResponseMessage.svelte` (flex-wrap container, full-width children). See §7 design note. | `a482b51` |
| 19 | Design decision (user, 2026-08-20) | **`get_my_chats` gains a `tag` parameter** — filtering chats by tag is a *list filter*, not a text search, so it moved out of the `search_chats("tag:…")` string prefix into a typed `tag="…"` argument. Internally it calls `POST /api/v1/chats/tags` `{name, skip, limit}` (verified live: identical results to the `search?text=tag:` prefix) — a **query-only POST** (no side effects) added to the allowlist, paginated by `skip`/`limit` (bounded by `MAX_PAGES`), sorted client-side like the plain list. `search_chats` keeps its documented prefixes for text + filter combos. | v0.16.0 |

**Unchanged commitments:** scope confined to `tools/owui_meta/`; one commit per iteration; Conventional Commits; all docs/code in English; no credentials ever configured or stored.

---

## 7. Future versions (deferred work)

Work intentionally postponed to a future version of the tool (not part of the current branch scope):

- **Admin-only methods** (former Iteration 2, DESIGN §6.2): `list_users`, `get_user`, `list_all_chats`, `get_admin_config` — with the runtime role gate (`__user__.role == 'admin'` before any HTTP call). Deferred by user decision (2026-08-01).
- **`get_chats(scope="all")` date-range filter** (former Iteration 9 task 9.6, DESIGN §8.10): `created_after`/`created_before`/`updated_after`/`updated_before` params (epoch or ISO values, half-open `[after, before)` ranges, applied client-side before sort/slice, composable with `tag`/`sort_by`/`sort_order`). Design complete and recorded; **deferred by user decision (2026-08-21)** — the manual workaround (sort by `created_at` asc + pick the range) remains the current path.
- Anything else not explicitly in the current iterations (per DESIGN §2 / out-of-scope list below).

---

### Design note (2026-08-03): file attachments in the UI + `get_file_content` read opt-in

Reviewed against Open WebUI source (`main` and the instance's v0.10.2) — not just intuition:

**Implemented (2026-08-03), later refined to Option B for images:** `get_file_content` shows the requested file in the assistant message while the returned text stays clean:

- **Image → `embeds` event (Option B, user decision):** the image is embedded inline in the message via `{"type": "embeds", "data": {"embeds": [<html>]}}` — an HTML `<img>` fragment (contained, rounded, capped at 320px) rendered by the frontend's `FullHeightIframe` srcdoc (verified in v0.10.2 `ResponseMessage.svelte` / `FullHeightIframe.svelte`). This is the non-markdown, non-artifact embed mechanism; the image appears inside the message flow like a snippet. The srcdoc iframe inherits the parent document's base URL, so the relative `/api/v1/files/{id}/content` resolves against the app origin and loads with the session cookie (no token in the URL). **Caveat:** the srcdoc iframe is sandboxed without `allow-same-origin` unless the admin enables `iframeSandboxAllowSameOrigin` — same-origin `<img>` subrequests generally still load, but if a deployment blocks them, that setting is the lever. The returned note tells the model the image is already visible and must NOT embed/display it again as markdown (mirrors `generate_image`'s contract, which is why the builtin never double-renders).

**Multi-embed rendering (verified in v0.10.2 source, 2026-08-03):** `ResponseMessage.svelte` draws the embeds block as a `flex flex-wrap` container where **each embed is a `<div class="my-2 w-full">`** — i.e. every embed is full-width and they **stack vertically**, one `FullHeightIframe` box below the other (each with its own rounded border/spacing), NOT a side-by-side grid. Consequences: (1) emitting N embeds renders N stacked previews, each capped at 320px by our HTML — readable but tall; (2) the `embeds` event alone cannot place images side by side. **For a gallery (future, not implemented):** emit a SINGLE embed whose HTML wraps several `<img>` in its own flex/grid container, instead of one embed per image — one compact card instead of N stacked boxes. `_image_embed_html` would need to accept a list of (id, name). This is recorded as a design option, not yet built.
- **Text → `files` event** (attachment + 100-char snippet in the returned text; no full dump).
- **Generic binary → `files` event** (attachment + note; no content in context).
- **`files` mechanism** → `{"type": "files", "data": {"files": [...]}}` via `__event_emitter__` (native event, verified in `backend/open_webui/socket/main.py`): the backend re-broadcasts it live AND persists it into the assistant message's `files` field automatically (`touch=False`) — no extra persistence code needed. Images intentionally do NOT use `files` (avoids double rendering with the embed).
- **Security** → no token in the URL (the frontend builds the download URL itself: `FileItem.svelte` opens `/files/{id}/content` with the session cookie; `Image.svelte` prefixes `WEBUI_BASE_URL` for paths starting with `/`); `_sanitize`/`_redact` unchanged; no bytes in the context.
- **Filename** → one extra best-effort call to `GET /api/v1/files/{id}` (added to the allowlist) for the display name; on failure the file id doubles as name and the content fetch is never blocked.

**Event item schema pitfall (verified in the frontend, pinned by `test/test_file_attachment.py`):** `ResponseMessage.svelte` renders `file.url`/`name`/`type`/`size`/`content_type` and `FileItemModal.svelte` reads `item.meta.content_type`. For images use `type: "image"` + `url: /api/v1/files/{id}/content` (path with `/`, mirroring `generate_image`); for everything else use `type: "file"` + `url: <id>` + `meta: {content_type}`. A bare id as `url` with `type: "file"` works for download but breaks inline image preview.

**Declared side effect:** the `files` event writes to the assistant message in chat history. This is a benign message-level mutation for a tool advertised as read-only — DESIGN/README wording acknowledges it.

**Read opt-in (deferred by user decision, 2026-08-03):** the 100-char snippet alone would degrade `get_file_content` for user-library files that nothing else covers in v0.10.2 (no `view_file` for chat files until `main`; no RAG for them). Follow the `view_file` pattern (verified in `main` and v0.10.2 `backend/open_webui/tools/builtin.py`): `max_chars` (default 100) + optional `offset` for paged reading, so the model can read more on demand while the default stays clean. The model decides with the snippet whether it needs more.

**Interaction with `file_context` (verified in `middleware.py`):** the capability only gates chat-attached files (`metadata.files` → `chat_completion_files_handler`): with it ON, attached files are injected into context (full or RAG chunks) — full-context files make a `get_file_content` dump redundant; with it OFF, nothing is injected and `main` injects `list_chat_files`/`query_chat_files`/`grep_chat_files`/`view_file` instead. KB/model-attached knowledge is NOT gated by `file_context`. `owui_meta` remains complementary in both modes (user-library files are never covered by either path).

---

### Design note (2026-08-03): file cleanup — `delete_files`

**Problem (user report, verified in v0.10.2 source):** deleting a chat does NOT delete the files attached to it. `chats.py::delete_chat_by_id` never touches `Files`, so the file stays in the user's library and in storage, forever.

**First write operation — an explicit exception to read-only v1 (user decision 2026-08-03):**

- `delete_files(file_ids)` — accepts a **list** of ids for one-pass cleanup. The whole list is validated up front (one invalid id rejects the call before any request, so nothing is partially deleted by a bad call); ids are deduplicated and capped at `MAX_DELETE_FILES` (50). Per file: `GET /api/v1/files/{id}` first (report filename/type/size that will disappear; a 404 on the GET means the DELETE would fail too, so nothing is touched for that id), then `DELETE /api/v1/files/{id}`. The backend (v0.10.2 `files.py::delete_file_by_id`) re-verifies authorization (`file.user_id == user.id` or admin or write access, else 404), removes KB associations + embeddings, deletes the object from storage and the vector collection, and returns `{"message": "File deleted successfully"}`. Deletion is **irreversible** — the tool call itself is the confirmation. A per-file failure (missing / not yours / backend error) is reported by id without aborting the rest.
- **Orphan detection is NOT a tool method (user decision):** it was prototyped as `list_orphan_files` (files whose `meta.data.chat_id` is absent from the user's live chats) and then dropped — the model already derives it from `get_my_files()` (which exposes `origin_chat_id`) + `get_my_chats()` (live chat ids), so a dedicated method would duplicate existing capability with extra surface. Documented cleanup flow in README.

**HTTP engine:** `_fetch`/`_fetch_with_retry` now take a `method` argument (default `GET`); new `_api_delete_json` applies the same content-type validation and non-leaking error mapping as GET (SPA HTML catch-all protection intact). The only write-capable method is `delete_files`; the allowlist gains no other write route (PLAN §8).

**Safety posture unchanged:** token never in logs/URLs, `_sanitize`/`_redact`/tripwire intact, 404 never reveals existence, DELETE inherits `_validate_status` mapping. Pinned by `test/test_file_deletion.py` (9 tests: batch per-file GET+DELETE, partial failure, markdown summary, 404-on-GET never calls DELETE, 403 mapping, whole-list validation, non-list/empty rejection, dedup).

**Future (not in scope now):** admin sweep of all users, deletion of files orphaned by deleted *messages* inside live chats.

---

### Design note (2026-08-03): status events (Iteration 4) + error consolidation

**Scope (user decision):** progress info goes only through `__event_emitter__` — no application logs added (explicitly parked).

- **Progress = `status` events** (start `done=False`, completion `done=True` with the same action label — "Reading your profile…", "Querying your chats…", "Listing your files…"…), the `smart_fetch_url` UX pattern (DESIGN §8.5). Gated by a new `verbose` valve (admin + per-user; per-user overrides admin; default `True`) so quiet users can turn them off. Wired through `_run(emitter=…, action=…, verbose=…)` — every public wrapper passes `__event_emitter__`.
- **Errors = `chat:message:error`** (the message error block, verified in the frontend `Chat.svelte`/`Error.svelte`), **never gated by `verbose`**, and **consolidated to at most one per tool call**: `_run` emits a single error on failure; a batch `delete_files` with per-file failures emits one "N of M file(s) could not be deleted" summary instead of one toast per file — the per-id detail stays in the returned text. This directly addresses the user's requirement: error visibility is reserved for real errors, and repeated failures never flood the user with toasts.
- **Events never contain the token** (pinned by `test/test_events.py`: start+done, no-emitter no-op, `verbose=False` suppresses status but NOT errors, per-user override, single error on failure, single consolidated error on batch failures, no token in any event).

## Iteration 8 — Chat organization metadata: tags, folders, archived & usage stats ✅ DONE (2026-08-20)

**Commit:** `feat(owui_meta): surface chat tags, folders, archived chats and usage stats`

Implements the **P1–P5 proposals** (extension brief 2026-08-20), re-validated against the v0.10.2 source **and** live probes against the instance (2026-08-20). Several brief claims were **corrected** after verification — see “Backend facts verified live” below.

### Backend facts verified live (2026-08-20)

All probes ran with a user-role API key against the internal instance (same instance DESIGN was validated against):

| Claim (brief) | Verified reality |
|---|---|
| “List items expose `folder_id`/`pinned`/`archived`” | ❌ **False.** `GET /api/v1/chats/` returns `ChatTitleIdResponse` = `id, title, created_at, updated_at, last_read_at, snippet` only. `include_folders`/`include_pinned` change **which rows** the SQL returns — they add **no fields** |
| “Default list shows everything” | ❌ **False.** The default list **excludes** chats inside folders and pinned chats. Live: p.1 default 60 vs `include_folders+include_pinned` 60 (**+43 delta**); p.2 default 19 vs 60 (**+60 delta**); `stats/usage` reports **147 total**. So `get_my_chats` was silently hiding ~⅓ of the user's chats — fixed in this iteration |
| “Filter by tag via `/api/v1/chats/tags?tag=…`” | ❌ It is **`POST /api/v1/chats/tags`** with JSON body `{name, skip, limit}` (no body → 422; `GET` on the path → 401). Returns `ChatTitleIdResponse` |
| “Backend has a tag-filtered endpoint” | ✅ Yes (`POST /chats/tags`). **But** `search?text=tag:<name>` returns the **same results** with zero new surface — preferred |
| Tags are stored inline in `meta.tags` + a per-user `tag` catalog | ✅ Confirmed. `GET /api/v1/chats/all/tags` (no trailing slash) returns `TagModel` (`id, name, user_id, meta`); 19 tags live |
| “UI search supports filter prefixes” | ✅ Confirmed and **already server-side**: `tag:`, `folder:`, `pinned:true/false`, `archived:true/false`, `shared:true/false`, plus `tag:none` (= chats with no tags). Live-verified each |
| Search returns a `snippet` | ✅ Confirmed: `search?text=` populates `snippet` per result. The tool was **dropping it** in `_summarize_chats` — surfaced now |
| “Items expose pinned/archived flags” | ❌ Only `ChatResponse` (detail by id) carries `pinned`, `archived`, `folder_id`, `meta.tags`. List items never do |
| Chat usage stats | ✅ `GET /api/v1/chats/stats/usage` (`page`, `pageSize`) → `{items, total}`; each item: `tags`, `message_count`, `models`, `history_*` counts, averages, `last_message_at`. **EXPERIMENTAL** (may be removed in future releases). **Pagination quirk (verified live): `pageSize` is IGNORED** — always ≤ 50 rows/page in irregular sizes (live: 50/49/49 then an empty page with declared total 149), so the tool iterates until an empty page or the declared total (`short_page_stops=False`), never stopping on a short page |
| Folders | ✅ `GET /api/v1/folders/` (**with** trailing slash) → `FolderNameIdResponse` (`id, name, meta, parent_id, is_expanded, created_at, updated_at`); gated by `folders.enable` + `features.folders` permission → may 403 depending on the instance (not gated on this one: 2 folders live) |

**Slash map verified live** (new routes): `folders/` WITH slash; `chats/all/tags`, `chats/archived`, `chats/stats/usage` WITHOUT slash; `POST chats/tags` without slash.

### Changes

1. ✅ **`get_my_chats`** (DONE, commit `e65161d` v0.11.0): adds `include_folders` / `include_pinned` query params to `GET /api/v1/chats/` (they only filter rows server-side; item fields stay the same). This makes the tool see folder + pinned chats it was silently missing (~⅓ of the user's chats).
2. ✅ **Tags surfaced** (commit pending, v0.15.0):
   - `get_my_tags()` → `GET /api/v1/chats/all/tags` (TagModel: id, name; user_id/meta not exposed to the model). Lets the model answer “which tags do you use?”.
   - `_summarize_chats` keeps `tags` and `snippet` when present (search results carry `snippet`; list items never carry either, but `stats/usage` items carry `tags`).
3. ✅ **`search_chats` align with UI prefixes (P3)**: no new endpoint needed for search — the backend already parses `tag:`, `folder:`, `pinned:`, `archived:`, `shared:`. The tool:
   - passes `text` through unmodified (it already does),
   - surfaces the per-result `snippet` field (was dropped) — rendered as an extra table column only for search results that have one,
   - documents the prefixes in the method docstring so the model can use them.
   **Revised (user decision 2026-08-20, v0.16.0):** filtering chats *by tag* is semantically a **list filter**, so it moved into `get_my_chats(tag="…")` (typed parameter → `POST /chats/tags`, query-only, pure tag filter — see row 19 in §6). `search_chats` keeps its prefixes for *text* searches and for combining filters with text.
4. ✅ **`get_archived_chats(limit)`** → `GET /api/v1/chats/archived` (no slash) — `ChatTitleIdResponse` list (no server-side pagination), same summarization as `get_my_chats`, sliced by `limit`, header “Archived chats”.
5. ✅ **`get_chat_stats(chat_id)`** (P4) → `GET /api/v1/chats/stats/usage` iterated client-side (see the pageSize quirk above) and filtered by id: `tags`, `message_count`, `models`, `history_*` counts, averages, `last_message_at`, dates. Route marked EXPERIMENTAL in the docstring; not-found and route failure → clean error, never crashes other methods. **Not** the export route (`/stats/export` is excluded by the query-only rule).
6. ✅ **`get_my_folders()`** (P4) → `GET /api/v1/folders/` (trailing slash): id, name, parent_id, is_expanded, dates (`meta`/icon not exposed). A 403 (folders disabled on the instance) maps to a readable error.
7. ✅ **`get_chat` → `get_chat_summary` + `get_chat_metadata`** (DONE, commits `a3f1e07` v0.12.0 / `b813603` v0.13.0, user decisions 2026-08-20): `get_chat` was renamed (it never returns full content anymore) and **split into two distinct methods**:
   - **`get_chat_metadata(chat_id)`** — organization metadata only (`id`, `title`, `message_count`, `models`, `tags`, `folder_id`/`folder_name`, `pinned`, `archived`, `share_id`, dates). **No message content in any format** — the light "chat data" query.
   - **`get_chat_summary(chat_id)`** — the same metadata plus a markdown snippet of the **main branch** (walked from `currentId` back through `parentId` — the chat is a tree, not a list): the first and last `DEFAULT_SNIPPET_HEAD`/`DEFAULT_SNIPPET_TAIL` messages (**fixed at 3 each** by user decision — the model never passes head/tail, no parameters in the signature). Each message is collapsed to a single markdown-safe line: newlines → ` ⏎ `, every backtick escaped (a code fence in a message can never open a fence in the tool output), truncated to `MAX_SNIPPET_MESSAGE_CHARS`. Middle messages are replaced by an ellipsis line (`… ( N messages skipped ) …`); small chats (≤ 6 messages) show all without the ellipsis. The head/tail snippet is included in JSON too (it is the summary). Fixes the v0.10.2 shape bugs of the old renderer: assistant text lives in `output[].content[].text` (plain `content` is usually empty) and multimodal parts (images) are dropped. Constants `DEFAULT_SNIPPET_HEAD/TAIL`, `MAX_SNIPPET_MESSAGE_CHARS` — no magic numbers. Shared metadata extraction lives in `_chat_metadata_payload` (used by both methods).
8. **Docs**: DESIGN §5.1/§6.1/§9 updated with the verified endpoints and lessons; README lists the new methods; this plan.

### Security & correctness (unchanged invariants)
- All new endpoints **read-only**, allowlisted, typed params only; no URL-taking param.
- Content-Type validation + slash map honored (`folders/` has the slash; the rest don't).
- `_sanitize`/`_redact` at the output boundary; tripwire test still passes.
- Pagination caps (`MAX_PAGES`, `DEFAULT_PAGE_SIZE`) respected for `stats/usage`; the pageSize-ignored quirk is handled by `short_page_stops=False` (only an empty page or the declared total ends the iteration).
- `delete_files` remains the only write operation; untouched.
- The EXPERIMENTAL `stats/usage` route is optional/failure-tolerant: if it 4xx/5xxs or the chat has no entry, the method reports a clean error instead of breaking the tool.

### Tests
- `test_route_map.py`: new cases — `get_my_tags` → `/api/v1/chats/all/tags`, `get_archived_chats` → `/api/v1/chats/archived`, `get_chat_stats` → `/api/v1/chats/stats/usage`, `get_my_folders` → `/api/v1/folders/` (slash asserted), and `get_my_chats` now sends `include_folders`/`include_pinned`.
- `test/test_iteration8.py` (new): tags summarization (no user_id/meta leak), search snippet surfaced (JSON + markdown snippet column), archived list (label + limit), chat stats across the irregular 50/49/49/empty pagination (4 pages fetched), stats not-found clean error, invalid id rejected without request, folders field whitelist + 403 mapping.
- Full suite: **129 passed**; version bumped to v0.15.0. Live smoke (2026-08-20) against the instance: tags (19), archived (0), folders (2), chat stats (52-message chat found), search `tag:tool` (3) and snippet column — all working.

### Progress log (2026-08-20)

| Item | Status | Commit / version |
|---|---|---|
| 1. `get_my_chats` include_folders/include_pinned | ✅ DONE (tested live: the folder + pinned chats now appear) | `e65161d` → v0.11.0 |
| 2. Tags surfaced (`get_my_tags`, `_summarize_chats` keeps tags/snippet) | ✅ DONE (tested live: 19 tags) | v0.15.0 |
| 3. `search_chats` UI prefixes + snippet | ✅ DONE (tested live: `tag:tool` → 3, snippet column rendered) | v0.15.0 |
| 4. `get_archived_chats` | ✅ DONE (tested live: 0 archived) | v0.15.0 |
| 5. `get_chat_stats` (stats/usage, EXPERIMENTAL, pageSize-ignored pagination) | ✅ DONE (tested live: 52-message chat found) | v0.15.0 |
| 6. `get_my_folders` | ✅ DONE (tested live: 2 folders) | v0.15.0 |
| 7. Chat detail → **`get_chat_metadata`** (metadata only, no content) + **`get_chat_summary`** (metadata + head/tail snippet markdown-safe, fixed 3+3 constants) | ✅ DONE (tested live) | `a3f1e07`, `b813603`, `78d92de` → v0.14.0 |
| — | Frontmatter version aligned to actual (was stuck at 0.12.0) | `fe00f43` |
| — | Frontmatter version bumped to 0.15.0 (Iteration 8 complete) | v0.15.0 |

Iteration 8 is complete.

## Iteration 9 — Improvement pass: `tag:` semantics verified, image metadata, stats metrics, credential guard, chat date-range filter (2026-08-21)

**Status:** ⏳ **PLANNED — live + source research COMPLETED 2026-08-21, implementation pending.** Written from the consolidated improvement brief (2026-08-21, incl. the date-range filter and the live-verified delimiter cases added after the previous deliverable). **Two brief claims were corrected after verification against the v0.10.2 source and live probes** (see “Backend facts verified live” below): Task 1 (`tag:` is ALREADY a scope limiter server-side) and Task 3 (root-caused: a backend bug in the assistant-length metric + distinct documented semantics for the other two). **Task 9.7 added 2026-08-21** (user report): the `folder:` prefix is broken for real (multi-word) folder names — name resolution to the backend's underscore-normalized form; see §9.7. Implementation pending.

**Backend facts verified live (2026-08-21)** — probes against the internal instance (`http://open-webui.private`, user-role key) + v0.10.2 source (`backend/open_webui/routers/chats.py`, `backend/open_webui/models/chats.py`, `backend/open_webui/models/folders.py`):

| Brief claim | Verified reality |
|---|---|
| “`pinned:`/`folder:` scope-limit; `tag:` does not (standalone filter that relaxes the text)” | ❌ **`tag:` is ALSO a scope limiter** (AND with free text). `"Open WebUI"` → 37; `"Open WebUI tag:comfyui"` → 1 (the one of the tag's 3 chats that contains “Open WebUI”); `"manchego tag:comfyui"` → 0 and `"zzz_nonexistent_xyz tag:comfyui"` → 0 (a standalone filter would have returned the tag's 3 chats). Source: `get_chats_by_user_id_and_search_text` strips all prefixes, then ANDs `title/content LIKE %text%` with `EXISTS(meta.tags = tag)`. **⚠️ The brief's `folder:` evidence was MISREAD (see task 9.7):** `"Open WebUI folder:Open WebUI meta"` → 0 is the BROKEN multi-word folder parsing, not working scope limiting — the words after the first (`WebUI meta`) leak into the free text and the first word (`Open`) matches no folder exactly |
| “Multi-tag behavior undefined (recommend AND)” | ✅ Backend is **AND** already: `and_(*[EXISTS(tag_i) for tag_i in tag_ids])` |
| “`tag:` with zero matches” | ✅ **Intended orphan-tag cleanup** (not a bug): a tag query with zero results deletes the **catalog entry** (`Tags.delete_tag_by_name_and_user_id` — only the `tag` row: id/name/meta; per-chat inline `meta.tags` are untouched and recreate the entry on the next chat update). A typo'd/nonexistent tag deletes nothing (the lookup filters on the entry existing). Lazy GC — the UI removes tags through the same routes. **Tool-relevant nuance:** `search` scopes to non-archived chats (`Chat.archived == False`), so a tag living **only on archived chats** returns 0 there and the entry is cleaned, while `get_my_chats(tag=)` (`POST /chats/tags`) sees it — documented upstream asymmetry, no tool change |
| “Snippets reflect the matched text” | ✅ `chat_search_content_text` strips prefixes before building the snippet. Caveat: snippets search only the **plain `content`** string, which is empty for v0.10.2 assistant messages (text lives in `output[].content[].text`) — assistant-matched snippets are often absent |
| “`get_chat_stats` anomaly: 52 vs 50, assistant avg 0.0, last_message_at ≠ updated_at” | ✅ **Fully root-caused** (see 9.3): `message_count` = `len(get_message_list(messages_map, currentId))` counts ALL branch steps (52, incl. 2 assistant `reasoning` steps with empty `content`); the tool counts only text-bearing messages (50). Assistant avg is a **backend bug**: `len(message.get('content',''))` over the plain `content` — empty for every assistant message in v0.10.2 → `0.0` always. `last_message_at` = timestamp of the last branch message; `updated_at` = chat row (moves on renames/edits) — different semantics, both legitimate |
| “`history_message_count` == `message_count`?” | ❌ Not always: `history_message_count = len(messages_map)` counts the WHOLE tree (all branches); `message_count` counts only the main branch. Equal here (52/52) only because the chat has no alternate branches |

Five independent tasks (9.6 deferred by user decision 2026-08-21), all preserving the read-only + allowlist security model (§2). Ships as up to five commits (one per task), frontmatter `version:` bumped per commit and aligned at the end (→ v0.17.0+); DESIGN/README updated per task; the live matrix re-run at the end (see Delivery).

### 9.1 `search_chats` — `tag:` scope-limiter semantics: VERIFY + PIN (the backend already implements it) ✅ DONE (v0.18.0)

**Context (verified live 2026-08-21):** `pinned:` scope-limits free text server-side, and `tag:` too (corrected above):
- `"Open WebUI"` → 37 chats; `"Open WebUI pinned:true"` → 0.
- `"Sulion"` → 1 chat (the pinned one); `"Sulion pinned:true"` → the same chat.
- `"manchego"` → 0 everywhere (term absent).
- **`folder:` is NOT a working scope limiter for real folder names** — the brief's `"Open WebUI folder:Open WebUI meta"` → 0 was the broken multi-word parsing (leak + no match), see task 9.7.

**Brief claim corrected:** the brief stated `tag:` does **not** scope-limit and acts as a standalone tag filter. **Verified false on this backend:** v0.10.2 `get_chats_by_user_id_and_search_text` strips every prefix, then **ANDs** the text search with the tag filter (`and_(*[EXISTS(json_each(meta.tags) = tag_i)])`). Live: `"manchego tag:comfyui"` → 0 and `"zzz_nonexistent_xyz tag:comfyui"` → 0 (a standalone filter would return the tag's 3 chats); `"Open WebUI tag:comfyui"` → 1 = the only tag chat containing “Open WebUI”. `tag:none` also works (`NOT EXISTS`). So **no scope-limiter code change is needed in `_search_chats`**.

**What remains (the real work):**
1. **Pin the semantics with tests** — mock + env-gated live cases proving text+tag is AND, multi-tag is AND, `tag:none` is untouched, and results match `get_my_chats(tag="…")`.
2. **Document the orphan-tag cleanup (no code — it is intended behavior):** a `tag:` query with zero matches triggers the backend's **deliberate lazy GC**: the catalog entry (`tag` row: id/name/meta) is deleted; per-chat inline `meta.tags` are untouched (they recreate the entry when the chat is updated); a typo'd/nonexistent tag deletes nothing. The tool must **not** guard or block it (blocking would break intended behavior). Two documentation notes only: (a) `search_chats` / `get_my_chats(tag=)` are read-only *queries* but can carry this write side effect — worth a docstring line so it is never a surprise; (b) **archived asymmetry:** `search` excludes archived chats (`Chat.archived == False`), `POST /chats/tags` does not — a tag used only on archived chats is cleaned via `search_chats("tag:X")` yet still visible via `get_my_chats(tag=)`. Upstream semantics; documented, not fixed.
3. **Snippet caveat (document):** the backend builds the snippet from the plain `content` string only — for v0.10.2 assistant messages (text in `output[].content[].text`) the snippet is usually absent even when the match is in the assistant text. Not a tool bug; do not compensate.

**Acceptance:** tests pin the AND semantics; the orphan-tag cleanup and the archived asymmetry are documented in the docstrings (no behavioral guard added).

**Acceptance:** tests pin that `search_chats("foo tag:bar")` returns only chats matching `foo` **and** carrying `bar`; multi-tag is AND; `tag:none` unchanged; a lone unknown `tag:` query does not delete the tag (per chosen mitigation); the side effect is documented in the docstring.

**Tests** (`test/test_iteration9.py`): mock — text+tag intersection, tag with no text, multi-tag AND, `tag:none` passthrough, unknown lone `tag:` no-delete; live (env-gated) — the delimiter matrix above plus `"Open WebUI tag:comfyui"`, `"manchego tag:comfyui"`, and a `tag:`-catalog-preservation check.

### 9.2 `get_file_content` — image header metadata ✅ DONE (v0.20.0)

**Commit:** `feat(owui_meta): enrich image files with resolution and color depth via Pillow`

**Context:** for images the tool previously returned only name/MIME/size/id (+ inline embed). The model could not answer "what resolution/color depth is this image?".

**Design (revised 2026-08-21 — user direction: use an existing library, don't hand-roll parsers):** **Pillow** — already bundled with Open WebUI (12.2.0) and used internally for image handling. `Image.open(io.BytesIO(body))` is **lazy**: it parses the header only and never decodes pixel data, so the cost is O(1) regardless of file size. The body is already in memory from `_api_get_raw`; `_image_header_info` closes the image right after reading `width`/`height`/`mode`. No `Image.load()`, no pixel data anywhere.

- Defensive import (`from PIL import Image`, degraded to `None`) — outside Open WebUI the tool still works, just without the extra fields.
- `requirements: httpx, Pillow` in the module header.

**Output fields** (all optional; a bad/truncated file or missing Pillow → `{}`, the fields are omitted and the call never errors):
- `width`, `height` — resolution in pixels.
- `color_mode` — the Pillow mode (RGB, RGBA, L, P, CMYK, …).
- `bit_depth` — bits per channel, derived from the mode via a small map (`img.bits` is only exposed by Pillow for some modes).

**Rendering:** the markdown binary renderer gains one line `Image: 1024×768 px, RGB (8-bit)`; json carries the same fields; the "embedded in the conversation / do not re-embed" note stays. Non-image files are untouched.

**Tests** (`test/test_iteration9.py`, Pillow-gated with `skipif` when absent): real images generated via Pillow (PNG RGB/RGBA, JPEG, GIF, WebP, BMP, TIFF) → correct width/height/mode/bits; garbage/truncated bytes → `{}`, no error; Pillow absent → enrichment skipped (no fields, no error); non-image binary → no new fields; markdown + json rendering.

**Verified:** 161 passed / 21 skipped (live env-gated); the instance account currently has no files, so the live file case is covered by the mock suite with real Pillow-generated images.

### 9.3 `get_chat_stats` — root-cause the metric divergence ✅ DONE (v0.19.0)

**Status: RESOLVED (2026-08-21)** — divergence fully root-caused against the v0.10.2 source (`routers/chats.py::get_session_user_chat_usage_stats`) and live probes of the anomaly chat.

**Verified root cause (live chat `cc7caaa6-fc56-4117-a685-c2e7955fb2ac` — 52 branch steps: 26 user + 26 assistant, of which 24 assistant carry readable text, 2 are pure `reasoning` steps with `content=''`):**

| Metric | Stats value | Source in v0.10.2 | Explanation | Action |
|---|---|---|---|---|
| `message_count` | 52 | `len(get_message_list(messages_map, currentId))` — the **main branch**, including every step | The tool's summary/metadata count (50) counts only text-bearing messages (`_message_text`) and thus excludes the 2 `reasoning` steps. Both are internally consistent; the stats number counts *steps*, the tool counts *readable messages* | **Document** the semantics (steps vs readable messages); do NOT change the tool's count — it is intentional |
| `average_assistant_message_content_length` | `0.0` | `sum(len(message.get('content','')) for assistant msgs) / n` | **Backend bug:** v0.10.2 assistant messages carry the text in `output[].content[].text`; plain `content` is empty (`''`) for every assistant message → the average is `0.0` for ANY chat with assistant messages (live: 24/26 have real text, still 0.0). Same bug distorts user averages for multimodal `content` lists (`len()` of a list, not of its text). The export route (`_process_chat_for_export::get_message_content_length`) already handles strings+lists correctly — the usage route does not | **Fix in the tool:** recompute both length averages from the ChatResponse (the tool already parses `output[].content[].text` via `_message_text`); keep the raw backend fields too, marked `(backend)` |
| `last_message_at` | 2026-08-18 05:43 | `message_list[-1].get('timestamp')` — timestamp of the **last message** on the main branch | `updated_at` (2026-08-19 14:34) is the **chat row** timestamp, moved by renames/edits/any row update — here ~1.4 days after the last message (chat was touched after its last message). Different sources, **both legitimate**; not a bug | **Document** the semantics (last-message time vs row-update time) |
| `history_message_count` | 52 | `len(messages_map)` — the **whole tree** (all branches) | Equal to `message_count` only because this chat has no alternate branches; on chats with alternate branches the two diverge by design | **Document**; already surfaced with the `history_*` fields |

**Decision (per metric):**
- **Fix (code):** recompute `average_user_message_content_length` / `average_assistant_message_content_length` in `_get_chat_stats` from the ChatResponse (same `_chat_metadata_payload` fetch used by summary/metadata — no extra request, or one shared fetch), using `_message_text`-style real text lengths. Backend values remain available under `…_backend` keys so nothing is lost.
- **Document (no code):** `message_count` = steps on the main branch; `last_message_at` = last-message timestamp vs `updated_at` = row-update; `history_message_count` = whole-tree count.
- **Docstring** of `get_chat_stats` gains a semantics note.

**Acceptance:** the divergence is explained from verified evidence (this table); the two length averages are correct (non-zero when assistant text exists); the other three metrics are documented in the docstring; tests pin the recompute path (mock) and the semantics note (docstring test).

### 9.4 Credential non-exposure — fail-loud guard + allowlist tripwire ✅ DONE (v0.17.0)

**Context:** the audit already guarantees no credential values are serialized (`_sanitize` + `_redact` + per-method whitelists + no-raw-body tripwire; DESIGN §7.2). The brief asks for a *defensive* guarantee covering **future** endpoints too.

**Design:**
1. **Fail-loud sanitizer:** `_ok` logs `logger.warning` (key name **only**, never the value) whenever `_sanitize` drops a credential-named key with a non-empty string value — a future leaking method becomes visible in the server log instead of being silently cleaned. Output behavior unchanged.
2. **Allowlist tripwire (static test):** extract every `_ROUTE_* = "…"` assignment from `owui_meta.py` and fail if any matches a secret-bearing pattern (the DESIGN §6.3 list: `auths/api_key`, `tools/id/{id}/valves(+/user)`, `tools/id/{id}`, `knowledge/external/connections*`, `*/admin/*` configs). A future developer adding a credential route is blocked at test/review time — "blocked by default".
3. **Documented guarantee:** DESIGN §7.2 gains a "credential non-exposure guarantee" paragraph enumerating the full control stack.

**Acceptance:** no meta endpoint can return credential values (documented); the static tripwire fails the suite if a secret-bearing route is ever added to the allowlist; a leaked credential-like field is logged (name only) and still stripped.

**Tests** (`test/test_security.py`): route extraction + pattern tripwire (positive control: a fake secret route constant makes the test fail); fail-loud log emission (caplog) with no value in the message; existing suites unchanged.

### 9.5 `delete_files` — destructive test (optional, sandbox only)

**Context:** `delete_files` is live but its destructive path has only mock coverage. The brief asks for an optional, sandbox-only live test.

**Design (env-gated, skipped by default):** behind `OWUI_META_DESTRUCTIVE_TESTS=1` (plus `OWUI_META_LIVE_URL` / `OWUI_META_LIVE_TOKEN`): upload a disposable file (`POST /api/v1/files/`, unique random content), call `delete_files([id])`, assert success + subsequent 404; foreign-file rejection uses `OWUI_META_LIVE_TOKEN2` when present (delete attempt on the other user's file → clean per-id failure, rest of the batch unaffected); cleanup any residue. Never runs against production by construction (opt-in env var + documented sandbox requirement).

**Acceptance:** own-file deletion confirmed against a sandbox instance; foreign files rejected per id; suite stays green and skipped without the opt-in env.

### 9.6 `get_chats(scope="all")` — date-range filter ⏸️ DEFERRED (user decision 2026-08-21)

**Commit:** *(none — not implemented; design kept for reference in this section and DESIGN §8.10)*

**Decision:** the user decided to **postpone the date-range filter** to a future version of the tool. It is removed from the current Iteration 9 scope; the design below stays recorded so it can be picked up unchanged later.

**Context (why it was proposed):** no native date filter; "list chats from June" currently requires `sort_by=created_at&sort_order=asc` + manual selection (the account's earliest chat is 2026-06-29, so June chats were the first 5).

**Design (backward-compatible, all new params optional):**

```python
get_chats(scope="all", limit=10, sort_by="updated_at", sort_order="desc", tag=None,
          created_after=None, created_before=None,
          updated_after=None, updated_before=None)
```

- **Value acceptance:** epoch int/float **or** ISO date/datetime strings (`"2026-06-01"`, `"2026-06-01 12:00"`, `"2026-06-01T12:00:00Z"`); a tolerant `_parse_ts` converts to epoch UTC (partial dates → midnight UTC).
- **Range semantics:** **half-open `[after, before)`** — `created_after` inclusive, `created_before` exclusive (June = `created_after="2026-06-01", created_before="2026-07-01"`); same for `updated_*`.
- **Application:** client-side, after the tag fetch / page iteration and **before** sort + slice (the API exposes no date filter). Composable with `tag`, `sort_by`, `sort_order`.
- `_render_chats` unchanged; the summary header can note the applied range (e.g. `June 2026`).

**Acceptance (when picked up again):** range filters return only chats created/updated within the range; composable with `sort_by`/`sort_order`/`tag`; existing calls unchanged.

**Tests (when picked up again)** (`test/test_iteration9.py`): epoch + ISO inputs, half-open boundaries (midnight cases), partial-date ISO, month case (`2026-06-01` → `2026-07-01`), composition with tag and sort, invalid dates → clean error / ignored, backward compatibility (no new required params).

**Tracked as future work:** see §7 “Future versions” below.

### 9.7 `search_chats` — fix the `folder:` prefix (folder-name resolution) ✅ DONE (v0.23.0)

**Commit:** *(this iteration's commit)*

**User report (2026-08-21):** searching chats by folder does not work when the **folder name** is used instead of the id — impractical, terrible UX.

**Root cause (verified live + v0.10.2 source `models/chats.py::get_chats_by_user_id_and_search_text` / `models/folders.py::search_folders_by_names`):**

| Step | Behavior | Consequence |
|---|---|---|
| 1. Prefix parsing | the search text is split on **spaces**; only words starting with `folder:` become folder queries | `folder:Open WebUI meta` → folder query = **"Open"** only |
| 2. Name matching | `search_folders_by_names` requires an **exact normalized match** of the full name (`[\s_]+`→space, lowercase) | "Open" ≠ "Open WebUI meta" (normalized "open webui meta") → **no folder match** |
| 3. No match ⇒ no filter | `folder_ids = []` → the `folder_id.in_(...)` clause is skipped | the folder filter is silently NOT applied — the query behaves like a plain text search |
| 4. Leak | the non-prefix words after `folder:` are **not** stripped from the free text | `folder:Open WebUI meta` searches the text "WebUI meta" (live: identical result to searching "WebUI meta" alone) |
| 5. The id never works | the backend matches folder **names**, not ids | `folder:<uuid>` → no folder named like a uuid → no filter (live: 60 chats = full page, unfiltered) |

**Live evidence (instance, 2026-08-21, folders "Open WebUI meta" and "IA generativa y formatos de cuantización de modelos"):**
- `folder:Open WebUI meta` → 1 chat — the **same** chat as the plain text search "WebUI meta" (a leak artifact, NOT the folder's contents).
- `folder:Open` / `folder:WebUI` / `folder:<uuid>` → 60 chats each (= full page, **no filter applied**).
- `folder:open_webui_meta` (underscore-joined, normalized) → **works**: the backend normalizes `_`≡space, so the single token matches exactly. `folder:ia_generativa_y_formatos_de_cuantización_de_modelos` → 38 chats.
- `GET /api/v1/chats/folder/{folder_id}` (the obvious id-based route) → **401** for the user role on this instance — cannot be used as the fix path.

**Design (implemented in `_search_chats` via `_resolve_folder_filters`, zero new routes — reuses the existing `folder:` filter and the already-allowlisted `GET /api/v1/folders/`):**
1. Tokenize the input; find `folder:` tokens. **Greedily match the longest phrase** against the user's folders (fetch `GET /api/v1/folders/` once per call when a `folder:` token is present; names normalized with the backend's own semantics via `_normalize_folder` — `[\s_]+`→space, lowercase) so multi-word names resolve: "folder:Open WebUI meta" → folder "Open WebUI meta".
2. **Rewrite the query** to the single-token form (`folder:<name with spaces→underscores>`, case-insensitive) — the backend's exact-normalized match then succeeds. **Strip the consumed words** from the free text (fixes the leak: "folder:Open WebUI meta" must NOT search "WebUI meta").
3. **Unknown folder → clean error** listing the user's folder names from step 1 ("Unknown folder 'X'; valid folders: 'a', 'b'") instead of the current silent no-filter — the model/user learns the valid names (mirrors how tags are surfaced). A folders-route failure (e.g. 403, folders disabled) propagates as the mapped readable error.
4. Mixed text works as AND: `"foo folder:Open WebUI meta"` → `foo folder:Open_WebUI_meta` (text AND folder, server-side).
5. **9.8 interplay:** a resolved valid `folder:` counts as a legitimate scope (it returns exactly that folder's chats), so `search_chats("folder:Open WebUI meta")` is NOT rejected by the 9.8 text-term rule; unknown folder names and non-folder pure prefixes still error.

**Acceptance (verified by tests, 2026-08-21):** `search_chats("folder:<any folder name with spaces>")` returns exactly that folder's chats (no text leak, no silent no-filter); `folder:<unknown>` → readable error listing valid folders; single-word and underscore names keep working (`folder:open_webui_meta` → rewritten to the real name form); combinable with free text as AND; no new routes.

**Tests** (`test/test_iteration9.py`): mock — multi-word folder resolution + leak stripping (asserts the exact `text` param sent), underscore single-token rewrite, single-word folder, unknown-folder error listing names (search endpoint never hit), text+folder AND, folders-route failure → clean error. Live (env-gated, `test_live.py::test_live_search_folder_name_resolution`): a real folder name (with spaces) resolves and returns that folder's chats; a folder UUID → clean "Unknown folder" error.

### 9.8 `search_chats` — require a search term ✅ DONE (v0.22.0)

**Commit:** *(this iteration's commit)*

**Decision (implemented):** `search_chats(text)` must require a **textual search term**. A call whose tokens are ONLY UI filter prefixes (`pinned:true`, `tag:meta`, `folder:<name>`, `tag:none`, …) must **error** — never return a full listing. The prefixes remain available as **optional refinements** that narrow an actual text search. Pure filtered listing belongs to the dedicated list tools (`get_chats(scope=…)`, `get_folders`, `get_tags`) — never to `search_chats`.

**Why (the mis-use that prompted it, live 2026-08-21):**
- `search_chats("pinned:true")` and `search_chats("tag:none")` returned **full listings** — search silently doubling as listing, with the `tag:none` → dozens-of-chats surprise.
- `search_chats("folder:Open WebUI meta")` (name) returned **1 chat** instead of the folder's chats — the worst of both worlds: neither a listing nor a search (the 9.7 bug).

**Rationale:** separation of concerns (searching = text matching; listing = filtered collections); predictability ("nothing searched" must be distinguishable from "nothing found"); correct API usage (list concerns are already covered by explicit list tools).

**Implementation (done in `_search_chats`):**
1. Tokenize `text` by whitespace; a token is a UI prefix if it starts with `_SEARCH_UI_PREFIXES` (`tag:`, `folder:`, `pinned:`, `archived:`, `shared:` — case-insensitive), otherwise it is a text token.
2. **No text token → `ToolError`** with a pointer to the list tools: `search_chats requires a text term; use get_chats(scope="pinned"|"shared"|"archived") or get_folders for filtered listings.` (raised before any request — no network hit, no orphan-tag side effect).
3. Otherwise proceed as before — 9.1 (tag AND passthrough) applies; 9.7 (folder-name resolution) remains for text+folder combos. Signature unchanged (`text` stays the only required param).

**Synergies realized:** the **lone-`tag:` orphan-tag cleanup is now unreachable via `search_chats`** (pure-prefix calls error before the backend is hit); `get_chats(tag=)` remains the listing path and keeps its documented cleanup side effect; the 9.7 folder fix is still needed for text+folder combos (`"ventilador folder:Open WebUI meta"`).

**Alternatives considered:**
- *Return an empty result instead of an error* — **rejected**: "nothing searched" would be indistinguishable from "nothing found" (the exact predictability problem this decision fixes); an error teaches the model the correct tool.
- *Keep `search_chats` as a hybrid search+listing* — rejected: the current state and the source of the confusion.
- *Add a flag (e.g. `list_only`) to `search_chats`* — rejected: extra surface; the dedicated list tools already exist.

**Acceptance (verified by tests, 2026-08-21):**
- `search_chats("pinned:true")` → **error** (never a listing of pinned chats). ✓
- `search_chats("tag:comfyui")` / `search_chats("tag:none")` → **error**. ✓ (`folder:MyFolder` also errors when it is a single token; multi-word `folder:` names are the 9.7 case — the trailing words are text and the call proceeds)
- `search_chats("ventilador pinned:true")` → only pinned chats matching "ventilador" (prefix still narrows). ✓
- `search_chats("Open WebUI folder:Open WebUI meta")` → a real search (text AND folder) — depends on 9.7 (still planned).
- Listing all chats / by tag / by folder / pinned → via `get_chats(scope=…)` (9.9) and `get_folders` (9.10), never via `search_chats`.

**Tests updated** (`test/test_iteration9.py`, `test/test_live.py`):
- `test_search_chats_tag_none_passthrough` (mock) → now expects the error (no request issued).
- `test_search_chats_pure_prefix_errors_for_each_prefix` (new): every UI prefix alone → error, zero network.
- `test_search_chats_text_plus_prefix_still_works` (new): text+prefix passes through unchanged (AND).
- `test_live_chats_tag_filter_matches_search_prefix` → reworked: a real term (a title from the tag's own chats) + `tag:` must be a subset of `get_chats(tag=)`.
- `test_live_search_tag_consistent_with_get_chats_tag` → reworked: same subset check.
- `test_live_search_text_and_prefixes` → split: text(+prefix) terms succeed; pure-prefix terms assert the error.
- `test_tag_semantics_documented_in_docstrings` → + "real search term" / "get_folders" needles.

### 9.9 Unify the chat list methods into `get_chats(scope=…)` ✅ DONE (v0.21.0)

**Commit:** *(included in the 9.9+9.10 commit)*

**Decision (implemented):** replace `get_my_chats`, `get_pinned_chats`, `get_shared_chats` and `get_archived_chats` with a **single `get_chats(scope=…)`**:

```python
get_chats(scope="all", limit=10, sort_by="updated_at", sort_order="desc", tag=None, ...)
```

- `scope` is a `Literal["all", "pinned", "shared", "archived"]`. **Omitted → `"all"`** (user decision 2026-08-21): `get_chats()` behaves exactly like today's `get_my_chats()` (plain list with `include_folders`/`include_pinned` + optional `tag`).
- **Why:** the four methods are the same resource (chats), the same result shape (`ChatTitleIdResponse`) and nearly the same params (`limit`/`sort_by`/`sort_order`/`tag`) — four near-identical tools made the model guess (e.g. `get_pinned` vs `get_shared`); one documented `Literal` scope removes the ambiguity.
- **Naming:** no `_my_` prefix (task 9.10) — the tool only ever sees the requesting user's data.
- **Backend unchanged:** same allowlisted routes per scope — `GET /api/v1/chats/` (+ `POST /chats/tags` for `tag=`), `/api/v1/chats/pinned`, `/api/v1/chats/shared`, `/api/v1/chats/archived`; same `_summarize_chats`/sorting/pagination/`max_response_chars`; the `"Archived chats"` label is kept for `scope="archived"`.
- **Implementation (done):** one public `get_chats(scope=…)`; internal `_get_chats` dispatches on `scope` to the existing per-route logic (all four routes now handled in one dispatcher; the private `_get_shared_chats`/`_get_pinned_chats`/`_get_archived_chats` helpers were removed); the three separate public methods are removed (breaking rename — the model picks up the new signature from the docstring; README notes old calls in stored history show as unresolved). Progress label per scope via `_chats_action`.

**Design notes (merged from NOTES.md N1/N2, 2026-08-21):**

- **N1 — `tag` × `scope != "all"` does not exist in the backend.** There is **no** backend route combining a scope with a tag filter: `POST /chats/tags` filters by tag + user with **no scope**; `/chats/pinned`, `/chats/shared`, `/chats/archived` take **no tag parameter**. So "pinned + tag" has no direct route. **Decision (v1, user + design review 2026-08-21): Option B** — restrict `tag` to `scope="all"` only; `tag` with any other scope → clean `ToolError` ("tag filter only applies to scope='all'"). Zero new requests, simplest contract, predictable for the model. Option A (client-side intersection: fetch the scope list + the tag set via `POST /chats/tags`, cross ids) remains a **follow-up** if the model actually needs scoped+tagged listings.
- **N2 — `limit` semantics differ per scope (document in the `get_chats` docstring; no code change):** `scope="all"` iterates pages of 50 (`MAX_PAGES`) then slices to `limit` ("top-N after iteration"); `pinned`/`shared` accept `pageSize` then slice; `archived` has **no server-side pagination** (whole list returned, sliced by `limit` — "top-N of the returned list"). The docstring must state the per-scope semantics so the behavior is documented rather than discovered.

**Acceptance:**
- `get_chats()` ≡ today's `get_my_chats()` (default `scope="all"`).
- `get_chats(scope="pinned")` ≡ `get_pinned_chats()`; `scope="shared"` ≡ `get_shared_chats()`; `scope="archived"` ≡ `get_archived_chats()` (label kept).
- `tag=` is accepted only with `scope="all"` (**Option B, per N1**): any other scope + `tag` → clean `ToolError` ("tag filter only applies to scope='all'").
- Invalid `scope` → clean `ToolError` listing the valid values.

**Tests updated:** `test_route_map.py` (route per scope), `test_user_methods.py`, `test_iteration8.py` (archived), `test_iteration9.py` (tag filter), live suite — re-point to `get_chats(scope=…)`; add default-`all` and invalid-scope cases. The deferred date-range filter (9.6) applies to `get_chats(scope="all", …)` when implemented.

### 9.10 Drop the `_my_` prefix from all method names ✅ DONE (v0.21.0)

**Commit:** *(included in the 9.9+9.10 commit)*

**Decision (implemented):** remove `_my_` from every public method name — the tool only ever operates on the requesting user's data (token-scoped), so `my` is redundant noise:

| Old | New |
|---|---|
| `get_my_profile` | `get_profile` |
| `get_my_chats` | absorbed into `get_chats` (9.9) |
| `get_my_files` | `get_files` (`get_file_content` unchanged) |
| `get_my_prompts` | `get_prompts` |
| `get_my_tools` | `get_tools` |
| `get_my_skills` | `get_skills` |
| `get_my_folders` | `get_folders` |
| `get_my_tags` | `get_tags` |
| `get_knowledge_bases` | **unchanged** — `get_knowledge_bases` (N3 resolved 2026-08-21: “knowledge bases” is Open WebUI's own nomenclature — the KB route, the UI section and the tool's own renderer header all use “knowledge bases”) |

- **Compatibility note:** this is a breaking rename of the tool surface. Open WebUI tools have no server-side alias; stored chat history referencing the old names will render those tool calls as unresolved, and the model re-learns the new names from the updated docstrings. README documents this.
- **Scope:** public API is the contract. **Per N3, the private `_get_my_*` implementations are renamed in the same commit** — no `my` residue remains anywhere in the codebase.

**Design notes (merged from NOTES.md N3, 2026-08-21):**

1. **`get_knowledge_bases` keeps its name — RESOLVED (2026-08-21).** The initial suggestion was `get_knowledge` for symmetry, but *knowledge bases* is Open WebUI's own nomenclature: the API route is `/api/v1/knowledge/`, the UI section is “Knowledge Bases”, and the tool's own renderer header already says `**Knowledge bases**` (`_render_knowledge`). Keeping `get_knowledge_bases` matches the platform vocabulary the model already knows — renaming to `get_knowledge` would break that association. No change; the DESIGN §6.1 allowlist row stays as-is.
2. **Rename the private `_get_my_*` implementations in the same commit** as the public names — no `my` residue remains (see **Scope** above).
3. **`get_file_content` is the deliberate exception** (it is *content*, not a "my" list) — no change; confirmed.
4. **`get_models` is already `_my_`-free** — no change.
5. **Update `test_docstrings.py`-adjacent references** — names must match signatures (the docstring contract test replicates the v0.10.2 parsers verbatim).

- **Tests:** global find/replace of the public names across the suite (route-map, user methods, iteration8/9, live); docstring + output-format suites unaffected beyond the rename; `test_docstrings.py` references updated (per N3.5).

### Delivery

**Tasks 9.9 + 9.10 delivered together (v0.21.0, one commit — they are coupled by `get_my_chats` → `get_chats`):**

- Renamed all `get_my_*` public methods to `get_*` and the private `_get_my_*` to `_get_*` in the same commit (N3: no `my` residue). `get_knowledge_bases` unchanged (N3 resolved: OWUI's own nomenclature).
- Unified the four chat list methods into `get_chats(scope="all"|"pinned"|"shared"|"archived")`; N1 decided (tag only with scope="all" → clean ToolError); N2 documented in the docstring (per-scope `limit` semantics).
- Tests: route-map updated + new cases (default scope=all with include_folders/include_pinned, per-scope routes, invalid scope clean error, tag×scope restriction); full suite green — **167 passed / 21 skipped** (live env-gated) on 2026-08-21.
- README updated: new method names + breaking-change note; DESIGN §6.1/§8.9.9/§8.9.10/Status updated.
- Frontmatter `version:` → v0.21.0; import + Valves checks pass.

**Task 9.8 delivered (v0.22.0, one commit):**

- `search_chats` now requires a real text term: a call whose tokens are ONLY UI filter prefixes (`pinned:true`, `tag:meta`, `tag:none`, `folder:MyFolder`, …) → clean `ToolError` ("search_chats requires a text term; use get_chats(scope=\"pinned\"|\"shared\"|\"archived\") or get_folders for filtered listings.") — raised BEFORE any request (no network, no orphan-tag side effect). Prefixes remain valid as refinements of a text term ("ventilador pinned:true" works).
- New `_SEARCH_UI_PREFIXES` constant (`tag:`/`folder:`/`pinned:`/`archived:`/`shared:`, case-insensitive match).
- Tests: 3 new/changed mock tests (pure-prefix error per prefix with zero network, tag:none error, text+prefix AND) + live rework (subset checks for text+tag, pure-prefix errors). Full suite green — **169 passed / 21 skipped** (live env-gated) on 2026-08-21.
- Docstrings + README updated (search term requirement, pointer to list tools).
- Frontmatter `version:` → v0.22.0.

**Task 9.7 delivered (v0.23.0, one commit):**

- `search_chats` now resolves `folder:` prefixes client-side (`_resolve_folder_filters`): greedy longest-phrase match against the user's folders (fetched once via the already-allowlisted `GET /api/v1/folders/`), rewritten to the single canonical token `folder:<name with spaces→underscores>` (the backend treats `_`≡space), with the consumed words stripped from the free text (no more leak) and unknown folder names → clean error listing the valid names (no more silent no-filter).
- New `_normalize_folder` (backend semantics `[\s_]+`→space, lowercase) and `_is_ui_prefix` helpers.
- 9.8 interplay: a resolved valid `folder:` is a legitimate scope, so `search_chats("folder:Open WebUI meta")` returns exactly that folder's chats; unknown folder names and non-folder pure prefixes still error.
- Tests: 6 new mock tests (multi-word resolution + leak stripping, underscore single-token, single-word, unknown-folder error listing names, text+folder AND, folders-route failure → clean error) + 1 new live test (`test_live_search_folder_name_resolution`, env-gated). Full suite green — **175 passed / 22 skipped** (live env-gated) on 2026-08-21.
- Docstrings + README updated (folder: name resolution documented).
- Frontmatter `version:` → v0.23.0.

**Still pending for Iteration 9 (v0.23.0+):** 9.5 (`delete_files` destructive test). 9.6 DEFERRED (see §7).

## 8. Out of scope (per DESIGN §2)

- RAG/retrieval (`/api/v1/retrieval*`, `rag*`, `embed*`, `rerank*`) — globally bypassed on the instance.
- Memories (`/api/v1/memories*`).
- **Any write/delete operation** — except the single explicit exception decided 2026-08-03: `delete_files(file_ids)` for file cleanup (see §7 design note). No other write/delete route is allowed.
- Any export/import route — v1 is a **query-only interface** (user decision 2026-08-01), so even `GET` exports (`/skills/export`, `/tools/export`, `/functions/export`, `/models/export`, `/knowledge/{id}/export`, `/chats/stats/export`) and the `POST` imports (`/chats/import`, `/models/import`) are excluded.
- Any route not explicitly allowlisted.
