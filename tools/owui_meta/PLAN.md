# Implementation Plan — owui_meta (Open WebUI Meta-Tool)

**Branch:** `feat/owui_meta_tool`
**Date:** 2026-08-01
**Status:** In progress
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

## 2. Security invariants (enforced in every iteration, verified by tests)

1. **No credentials configured or stored** — auth always comes from `__request__.state.token`, extracted by Open WebUI's `AuthTokenMiddleware`. A missing token yields a clear error, never a silent call.
2. **The token is never logged** and never appears in anything returned to the model.
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

## Iteration 0 — Baseline (docs + scaffolding)

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

**Pending follow-ups (tracked, not blocking):**
- `get_my_chats`/`search_chats`/`get_shared_chats`/`get_pinned_chats` responses are assumed to be a bare array (v0.10.2 curl evidence) — re-validate the exact shape when live-testing against the instance in Iteration 5, and adjust `_extract_items` if they return `{items, total}`.
- `get_chat()` returns the full history and relies on `max_response_chars` truncation — revisit summarization (top messages + total) in Iteration 3.
- Confirm `GET /api/v1/chats/search?text=` (verified as the parameter by 422; full response format pending confirmation).

## Iteration 2 — Admin-only methods with role gate

**Commit:** `feat(owui_meta): add admin-only methods with role gate`

- Methods from §6.2: `list_users`, `get_user`, `list_all_chats`, `get_admin_config`.
- Role check `__user__.get('role') == 'admin'` **before any HTTP call**; non-admin (or missing `__user__`) → explicit refusal, no request issued, no information leak.
- **Tests** (`test/test_admin_methods.py`): user role blocked with transport asserting zero requests; admin role proceeds; missing `__user__` refused.

**Definition of done:** regular users get a clean refusal; admins get data; both proven by tests.

## Iteration 3 — Pagination, sorting and typed filters

**Commit:** `feat(owui_meta): add pagination, sorting and typed filters`

Implements DESIGN §8.6 across the list/search methods:

- `page` / `page_size` parameters with sensible defaults; transparent page iteration up to a `MAX_PAGES` cap whenever the response declares `total > returned`.
- Per-resource sorting: chats by `updated_at`/`created_at`; files by `size`/`created_at`/`filename`.
- Typed filters, server-side first (only local filtering when the API lacks the criterion): files by `content_type`, `min_size`/`max_size` (bytes), filename fragment; chats by `text` and status (`pinned`/`archived`/`shared`).
- Summarized output — top N items + `total` — so large datasets never saturate the context (also bounded by `max_response_chars`).
- **Tests** (`test/test_pagination.py`): multi-page mock (total > pageSize), cap enforcement, filter application, summary shape.

**Definition of done:** a 100+ item dataset is queryable with filtering and sorting in bounded output — proven by tests.

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

---

## 4. Out of scope (per DESIGN §2)

- RAG/retrieval (`/api/v1/retrieval*`, `rag*`, `embed*`, `rerank*`) — globally bypassed on the instance.
- Memories (`/api/v1/memories*`).
- Any write/delete operation (read-only in v1).
- Any route not explicitly allowlisted.
