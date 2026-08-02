# Implementation Plan — owui_meta (Open WebUI Meta-Tool)

**Branch:** `feat/owui_meta_tool`
**Date:** 2026-08-01
**Status:** In progress — Iterations 0, 1, 3, 6 and 7 **done**; Iteration 2 **deferred to a future version**; Iterations 4–5 pending
**Scope constraint:** all changes are confined to `tools/owui_meta/` — nothing else in the repository is touched.

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

## Iteration 4 — Status events (UX)

**Commit:** `feat(owui_meta): emit status events during execution`

- `__event_emitter__` statuses on start ("Querying your chats…"), completion ("N chats found") and failure — DESIGN §8.5, matching the `smart_fetch_url` UX pattern.
- `verbose` valve to toggle events.
- Events never contain the token.
- **Tests** (`test/test_events.py`): recording fake emitter asserts event sequence and absence of token data.

**Definition of done:** tool execution is visible in the UI; event payloads are token-free (tested).

## Iteration 5 — Live validation & isolation tests

**Commit:** `test(owui_meta): add live integration and isolation tests`

- Env-gated live suite (`test/test_live.py`, skipped unless `OWUI_META_LIVE_URL` / `OWUI_META_LIVE_TOKEN` are set) re-validating the §5 endpoint map: profile, models, chats + `search?text=`, files + content, knowledge, prompts, tools; blocked `/users` for a user role; SPA-HTML trap for a nonexistent route.
- **Isolation test** (`test/test_isolation.py`): two real users each see only their own data (§7.3).
- Finalize `README.md` (usage, valves, security model, validation status) and mark DESIGN.md status.

**Definition of done:** live suite passes against the instance (or documents concrete failures as follow-ups); README complete.

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
| 13 | Hardening (user question: “¿implementado?”) | **Output-boundary guards against accidental leaks** | Whitelists are per-method/manual; a future method or server field could leak. Added two structural guards: `_sanitize` (drops credential-named keys with string values; boolean flags like `api_keys` kept) applied in `_ok` for both formats, and `_run` redacting the raw token string from success and error output (`request=__request__` threaded through all 14 wrappers). Plus `test/test_security.py`: sanitizer unit, future-raw-pass-through simulation, token redaction inside a whitelisted field, redaction in the error path, static no-raw-body tripwire. | (this commit) |
| 9 | Live validation | **Manual live tests** against the instance using a real (non-persisted) token: confirmed the route map, `/users` blocked for user role, `search?text=` format, NDJSON for `chats/all` | The live suite in Iteration 5 is still pending; these were one-off curl checks whose findings were folded into the fixes above. | — (findings in commits 1–3) |

**Unchanged commitments:** scope confined to `tools/owui_meta/`; one commit per iteration; Conventional Commits; all docs/code in English; no credentials ever configured or stored.

---

## 7. Future versions (deferred work)

Work intentionally postponed to a future version of the tool (not part of the current branch scope):

- **Admin-only methods** (former Iteration 2, DESIGN §6.2): `list_users`, `get_user`, `list_all_chats`, `get_admin_config` — with the runtime role gate (`__user__.role == 'admin'` before any HTTP call). Deferred by user decision (2026-08-01).
- Anything else not explicitly in the current iterations (per DESIGN §2 / out-of-scope list below).

---

## 8. Out of scope (per DESIGN §2)

- RAG/retrieval (`/api/v1/retrieval*`, `rag*`, `embed*`, `rerank*`) — globally bypassed on the instance.
- Memories (`/api/v1/memories*`).
- Any write/delete operation (read-only in v1).
- Any export/import route — v1 is a **query-only interface** (user decision 2026-08-01), so even `GET` exports (`/skills/export`, `/tools/export`, `/functions/export`, `/models/export`, `/knowledge/{id}/export`, `/chats/stats/export`) and the `POST` imports (`/chats/import`, `/models/import`) are excluded.
- Any route not explicitly allowlisted.
