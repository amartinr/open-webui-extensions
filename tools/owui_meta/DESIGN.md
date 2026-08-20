# Design Document — Meta-tool for Querying the Open WebUI Internal API

**Version:** 1.0
**Date:** 2026-07-31
**Author:** (with technical assistance)
**Status:** Design validated through real-world tests against the internal Open WebUI instance (v0.10.2); implementation complete through Iteration 8 (chat organization metadata); automated live validation committed as Iteration 5 (see PLAN.md progress log 2026-08-20). **Iteration 9: tasks 8.9.1–8.9.4 DONE (v0.17.0–v0.20.0); 8.9.5 pending; 8.9.7–8.9.10 planned** (`folder:` search fix, mandatory search term, `get_chats(scope=…)` unification, `_my_`-prefix drop — design decisions 2026-08-21); **the `get_my_chats` date-range filter (8.9.6) is DEFERRED by user decision 2026-08-21** (applies to `get_chats(scope="all")` when 8.9.9 lands)

---

## 1. Executive summary

This document designs an **Open WebUI tool** (a *Workspace → Tools* extension) that lets the model query **Open WebUI's own internal API** to answer questions about the user's data: their chats, files, prompts, tools, models, and so on.

The central, differentiating feature: **the tool authenticates with the credentials of the user making the request, with nobody having to supply them through configuration**. This works because the tool runs in the same backend process, within the context of the user's request, where the framework has already extracted the user's token (`request.state.token`). No global service keys, no hardcoded API keys, no secret storage.

---

## 2. Goals and non-goals

### Goals
1. Let the model answer with **real user data** (e.g. "summarize my recent chats", "what files do I have?", "show me my prompts").
2. **Zero credential configuration**: transparent authentication with the token of each request's user.
3. Reuse **all existing Open WebUI authorization** (roles, groups, feature permissions, resource access).
4. Maximum isolation between users: each user only sees their own data.
5. Be **secure by design**: endpoint allowlist, no SSRF, no dangerous endpoints.

### Non-goals (out of scope, decided by the instance owner)
- **RAG / retrieval** (`/api/v1/retrieval*`, `/api/v1/rag*`, `embed*`, `rerank*`): the instance has RAG **globally bypassed** (`BYPASS_EMBEDDING_AND_RETRIEVAL`), there are no collections to query.
- **Memories** (`/api/v1/memories*`): the user does not use them.
- **Writing/deleting** user data: the tool is **read-only** in its first version, **with a single explicit exception added 2026-08-03**: `delete_files(file_ids)` — user-authorized batch file deletion for cleanup (see §6.1/§8.5). Everything else remains query-only; no other write/delete endpoint is allowed.
- **Export/import**: v1 is a **query-only interface** (decision 2026-08-01). No exports or imports are exposed, even when the endpoint is a `GET` (e.g. `/api/v1/skills/export`, `/api/v1/tools/export`, `/api/v1/functions/export`, `/api/v1/models/export`, `/api/v1/knowledge/{id}/export`, `/api/v1/chats/stats/export`); the `/import` endpoints are `POST` and are already excluded by the read-only rule.
- **Administration endpoints** except for users with the `admin` role (see §7).

---

## 3. Architecture: why automatic authentication works

### 3.1 The key mechanism (credentials without configuration)

```
User's browser (JWT session)  or  API client (API key)
        │  HTTP request with credential
        ▼
AuthTokenMiddleware (pure ASGI middleware)
        │  extracts token → request.state.token
        ▼
Chat pipeline (utils/middleware.py → process_chat_payload)
        │  executes the tool with context: __request__, __user__, __model__, __metadata__
        ▼
THE TOOL reads request.state.token
        │  uses it as: Authorization: Bearer <token>
        ▼
Calls the internal API → the server treats it as that user
```

**Details verified in the v0.10.2 codebase:**

1. **`AuthTokenMiddleware`** (`backend/open_webui/utils/asgi_middleware.py`) extracts the user's token in this order:
   - `Authorization: Bearer ...` header
   - `token` cookie (browser web session)
   - Configurable `x-api-key` header (default; renameable via `CUSTOM_API_KEY_HEADER`)

   In all three cases the credential ends up as `request.state.token`. **Important (verified in the v0.10.2 source): `request.state.token` is NOT a plain string — it is an `HTTPAuthorizationCredentials` object (`scheme`/`credentials`)**, or `None` when no credential was provided (`get_http_authorization_cred` returns the object; the cookie/API-key branches wrap the value in `HTTPAuthorizationCredentials(scheme='Bearer', credentials=...)`). The tool must read `.credentials` (see §8.4/§7.2).

2. **The chat pipeline** (`backend/open_webui/utils/middleware.py`) builds the `extra_params` injected into every tool:
   ```python
   extra_params = {
       '__event_emitter__': ...,
       '__event_call__': ...,
       '__user__': user.model_dump(),      # id, name, email, role, permissions
       '__metadata__': metadata,
       '__oauth_token__': ...,
       '__request__': request,              # ← request.state.token lives here
       '__model__': model,
       '__chat_id__': ...,
       '__message_id__': ...,
   }
   ```

3. **Tools receive `__request__` as an execution parameter** (Open WebUI server-side tool pattern, documented in the official Tools documentation). Each tool method signature can declare `__request__` and the runtime injects it.

### 3.2 Why retrieving a user API key is neither needed nor possible

- Open WebUI API keys are **stored hashed only**; after creation they **cannot be viewed again** (official API Keys documentation).
- Therefore, no tool can "read" a stored `sk-...`.
- The natural path is `request.state.token`, which covers **both authentication cases**:
  - User browsing the UI → session JWT (`token` cookie).
  - User/script using an API key → the `sk-...` itself travels in the request and lands in `request.state.token`.

### 3.3 Choice: Option A (internal HTTP) over Option B (direct import)

| Criterion | A: Internal HTTP (`http://…/api/v1/…`) | B: Importing services (`open_webui.models.*`) |
|---|---|---|
| Reuses full authorization (roles, groups, permissions, resource access) | ✅ Yes, the entire FastAPI chain | ⚠️ Partial, must replicate checks manually |
| Product fidelity | High (exactly what the UI does) | Medium (internal code) |
| Version coupling | Low (stable API) | High (internal imports, fragile) |
| User isolation | ✅ Automatic (token-scoped) | ⚠️ Manual (filter by user.id) |
| Performance | One local HTTP hop (milliseconds) | Minimal (no hop) |
| **Decision** | ✅ **Chosen** | Discarded (only for edge cases) |

---

## 4. Configuration: base URL from the global admin UI configuration

### 4.1 The canonical URL

The tool **must not** hardcode the base URL in valves. It should read the **global admin UI configuration**:

- **Internal key:** `webui.url`
- **Source:** `Config.get('webui.url')` — model `open_webui.models.config.Config` (table `config`, persistent per-key storage). **Important: `Config.get` is `async` in v0.10.2** (`async def get(key: str, default: Any = None)`); the tool must `await` it (see §8.4).
- **Fallback environment variable:** `WEBUI_URL` (the default used to seed the key)
- **Admin UI:** *Admin Settings → General → "WebUI URL"* (field `adminConfig.WEBUI_URL` in `src/lib/components/admin/Settings/General.svelte`). Official description: *"Enter the public URL of your WebUI. This URL will be used to generate links in the notifications."*

Open WebUI's own code already reads this key to generate notification URLs (`publish_chat_finished_event` → `Config.get('webui.url')`).

### 4.2 Base URL resolution hierarchy

```
1. Config.get('webui.url')            ← canonical (set by admin in the UI)
2. os.getenv('WEBUI_URL')             ← fallback if empty (the key's default)
3. "fallback_base_url" valve          ← last resort (default http://localhost:8080)
   (only used if the global URL is empty or unreachable)
```

### 4.3 Operational caveat verified during design

`webui.url` is the site's **public URL**. Behind a reverse proxy or with TLS, the container may not resolve its own domain. Therefore:
- **Retry with the internal URL** if the first call fails with a `RequestError` (no DNS resolution / connection refused / timeout).
- **Never** retry if the response is a 4xx/5xx from the API (there the problem is different: auth, permission, route).

---

## 5. Verified endpoint map (instance v0.10.2)

Sources: **real curl tests with a `user`-role API key** against the internal instance, and **source-code extraction of the v0.10.2 tag** (routers + `main.py` prefixes).

### 5.1 ✅ Verified working (role `user`)

| Area | Endpoint | Observed response |
|---|---|---|
| Profile | `GET /api/v1/auths/` (trailing slash) | Full profile: id, name, email, role, permissions. **⚠️ The body ALSO echoes the request token** (`token`/`token_type`/`expires_at` — `get_session_user` supports the frontend's session refresh); the tool **field-whitelists** the profile and the token never reaches the model (§7.2) |
| Models | `GET /api/models` | OpenAI-compatible: `{"data":[{id, name, owned_by, info…}]}` |
| Chats | `GET /api/v1/chats/` (trailing slash) | Array of `ChatTitleIdResponse` — **only** `id, title, created_at, updated_at, last_read_at, snippet`. **No `meta`/tags/folder/pinned/archived in the items.** Default listing **excludes chats inside folders and pinned chats** (verified live 2026-08-20: p.1 default 60 vs `include_folders+include_pinned` 60, delta +43; p.2 19 vs 60, delta +60; total 147 per `stats/usage`). `include_folders`/`include_pinned` change which rows are returned, never the fields |
| Chats | `GET /api/v1/chats/{id}` | Full chat with message history (`ChatResponse`: also `pinned`, `archived`, `folder_id`, `meta.tags`) |
| Chats | `GET /api/v1/chats/search?text=…` (no trailing slash) | Search (parameter confirmed: `text`, not `q`). Populates a `snippet` per result. **Filter prefixes work server-side**: `tag:<name>`, `tag:none` (chats without tags), `folder:<name>`, `pinned:true/false`, `archived:true/false`, `shared:true/false` (verified live 2026-08-20) |

**Delimiter semantics — live-verified 2026-08-21 (§8.9.1):** `pinned:` and **`tag:`** act as **scope limiters** over the free text (`"Open WebUI"` → 37 chats, `"Open WebUI pinned:true"` → 0, `"manchego tag:comfyui"` → 0); the brief's claim that `tag:` relaxes the text was **verified false** (see §8.9.1). **`folder:` is NOT a usable scope limiter for real folder names** (verified live + source, §8.9.7): the prefix splits on spaces and requires an exact normalized name match, so multi-word folder names never filter (the leftover words leak into the text) and the folder id never works. A zero-match lone `tag:` query triggers the backend's **orphan-tag cleanup** (documented in §8.9.1 — not a bug).
| Chats | `GET /api/v1/chats/archived` (no trailing slash) | Archived chats, `ChatTitleIdResponse` list (verified live 2026-08-20) |
| Chats | `GET /api/v1/chats/all/tags` (no trailing slash) | User's tag catalog: `TagModel` list `{id, name, user_id, meta}` (id = name lowercased, spaces→underscores; verified live 2026-08-20: 19 tags) |
| Chats | `POST /api/v1/chats/tags` (no trailing slash, JSON body `{name, skip, limit}`) | Chats filtered by tag, `ChatTitleIdResponse` list. **POST** — GET on the path returns 401. Prefer `search?text=tag:<name>` (same results, zero new surface) |
| Chats | `GET /api/v1/chats/stats/usage?page=&pageSize=` (no trailing slash) | **EXPERIMENTAL** (may be removed in future releases). `{items, total}`; each item: `tags`, `message_count`, `models`, `history_*` counts, averages, `last_message_at` (verified live 2026-08-20: total 149). **Pagination quirk:** `pageSize` is IGNORED (always ≤ 50 rows/page, irregular sizes — live 50/49/49 then an empty page) so the tool iterates until an empty page or the declared total (`short_page_stops=False`), never stopping on a short page |
| Folders | `GET /api/v1/folders/` (**trailing slash**) | `FolderNameIdResponse` `{id, name, meta, parent_id, is_expanded, created_at, updated_at}`. Gated by `folders.enable` + `features.folders` permission → may 403 per instance (not gated on this one; 2 folders live) |
| Files | `GET /api/v1/files/` | `{"items":[{id, filename, meta, …}], "total": N}` |
| Files | `GET /api/v1/files/{id}/content` | File binary (e.g. `image/png`) |
| Workspace | `GET /api/v1/knowledge/` (trailing slash) | `{"items":[], "total":0}` |
| Workspace | `GET /api/v1/prompts/` (trailing slash) | Array of prompts `{id, command, name, content}` |
| Workspace | `GET /api/v1/tools/` (trailing slash) | Array of tools `{id, name, meta, access_grants…}` |
| Workspace | `GET /api/v1/skills/` (trailing slash) | Array of skills `{id, name, description, content, meta, is_active, …}` |
| Workspace | `GET /api/v1/skills/id/{id}` (no trailing slash) | Full skill incl. `content` |

### 5.2 🔒 Blocked by role (correct behavior)

| Endpoint | Result with role `user` |
|---|---|
| `GET /api/v1/users` | **401** "You do not have permission to access this resource…" |

### 5.3 🟡 Nonexistent routes or wrong method — key finding

| Case | Behavior |
|---|---|
| Nonexistent backend route OR wrong trailing slash (e.g. `GET /api/v1/auths` without slash, or `GET /api/v1/models/` with slash) | **SPA HTML with HTTP 200** (frontend catch-all). The catch-all is what absorbs a *miss*, not a redirect — so both a nonexistent route and a wrong-slash route produce HTML |
| Registered route but method not allowed (e.g. `POST /api/v1/retrieval/query`) | **405 JSON** `{"detail":"Method Not Allowed"}` |
| OpenAPI docs (`/openapi.json`, `/docs`, `/redoc`) | **Disabled** on this instance → returns SPA HTML |

**Design consequence:** the tool must **validate the response `Content-Type`** (expect `application/json`) and **not trust HTTP 200** to assume success.

### 5.4 ❌ Out of scope (excluded by owner decision)

- `retrieval*`, `rag*`, `embed*`, `rerank*` — RAG globally bypassed.
- `memories*` — the user does not use memories.

---

## 6. Endpoint allowlist (proposed, evidence-based)

The tool exposes **typed methods** (not a generic "call this URL"), and each method internally resolves to an allowlisted route. This prevents the LLM from inventing arbitrary routes.

### 6.1 Regular user (role `user`)

> **Implemented:** all rows below are live methods in the tool (v0.15.0). Iteration 8 (tags, folders, archived, usage stats) is complete.

| Tool method (suggested name) | Internal route | Status |
|---|---|---|
| `get_my_profile()` | `GET /api/v1/auths` | ✓ |
| `get_models()` | `GET /api/models` | ✓ |
| `get_my_chats(limit, tag)` | `GET /api/v1/chats` (with `include_folders`/`include_pinned`); with `tag` → `POST /api/v1/chats/tags` (query-only: pure tag filter, `{name, skip, limit}`) | ✓ |
| **`get_chats(scope="all"\|"pinned"\|"shared"\|"archived", …)`** (planned) | unifies the four chat list methods; omitted `scope` → `"all"` ≡ today's `get_my_chats`; routes unchanged per scope | ⏳ planned (8.9.9) |
| `get_my_chats` date-range filter | *(not implemented)* — `created_after`/`created_before`/`updated_after`/`updated_before` params, design recorded in §8.9.6 | ⏸️ DEFERRED (2026-08-21) |
| `get_chat_summary(chat_id)` | `GET /api/v1/chats/{id}` (markdown: metadata + first/last 3 messages; never the full content) | ✓ |
| `get_chat_metadata(chat_id)` | `GET /api/v1/chats/{id}` (metadata only: message_count, models, tags, folder, flags, dates; no message content in any format) | ✓ |
| `search_chats(text)` | `GET /api/v1/chats/search?text=` (supports `tag:`, `folder:`, `pinned:`, `archived:`, `shared:` prefixes + `snippet` in results). **Planned (Iteration 9.1):** `tag:` becomes a scope limiter (AND with free text), consistent with `pinned:`/`folder:` — §8.9.1 | ✓ |
| `get_archived_chats(limit)` | `GET /api/v1/chats/archived` (no pagination params; whole list sliced by `limit`) — **→ `get_chats(scope="archived")` (8.9.9)** | ✓ |
| `get_my_tags()` | `GET /api/v1/chats/all/tags` (tag catalog; `user_id`/`meta` not exposed) | ✓ |
| `get_chat_stats(chat_id)` | `GET /api/v1/chats/stats/usage` (**EXPERIMENTAL** endpoint; tags, message_count, models, history counts, averages) | ✓ |
| `get_my_folders()` | `GET /api/v1/folders/` (trailing slash; may 403 if folders disabled on the instance) | ✓ |
| `get_shared_chats()` | `GET /api/v1/chats/shared` — **→ `get_chats(scope="shared")` (8.9.9)** | ✓ |
| `get_pinned_chats()` | `GET /api/v1/chats/pinned` — **→ `get_chats(scope="pinned")` (8.9.9)** | ✓ |
| `get_my_files()` | `GET /api/v1/files` | ✓ |
| `get_file_content(file_id)` | `GET /api/v1/files/{id}/content` | ✓ |
| `delete_files(file_ids)` | per id: `GET /api/v1/files/{id}` + `DELETE /api/v1/files/{id}` (write, explicit — see §7.4) | ✓ |
| `get_my_prompts()` | `GET /api/v1/prompts` | ✓ |
| `get_my_tools()` | `GET /api/v1/tools` | ✓ |
| `get_knowledge_bases()` | `GET /api/v1/knowledge` | ✓ |
| `get_my_skills()` | `GET /api/v1/skills` | ✓ |
| `get_skill(skill_id)` | `GET /api/v1/skills/id/{skill_id}` | ✓ |

### 6.2 Admin only (role `admin`, with `__user__.role == 'admin'` check)

| Method | Internal route |
|---|---|
| `list_users()` | `GET /api/v1/users` |
| `get_user(user_id)` | `GET /api/v1/users/{user_id}` |
| `list_all_chats()` | `GET /api/v1/chats/all` |
| `get_admin_config()` | `GET /api/v1/auths/admin/config` |

### 6.3 Never allowed

- `retrieval*`, `rag*`, `embed*`, `rerank*`
- `memories*`
- `/api/v1/auths/signin`, `/signup` (pointless from within an already-authenticated session)
- `POST/PUT/DELETE` in general (read-only in v1)
- Any export/import route (GET or POST) — v1 is query-only (decision 2026-08-01)
- **Secret-bearing GETs (audit 2026-08-01 — never add)**: `GET /api/v1/auths/api_key` (returns the user's API key), `GET /api/v1/tools/id/{id}/valves` and `/valves/user` (return configured valve values, which can contain API keys), `GET /api/v1/tools/id/{id}` (the tool's Python source, which may contain hardcoded secrets), `GET /api/v1/knowledge/external/connections*` (external DB configs with credentials), and any `*/admin/*` config route (LDAP/OAuth secrets).
- Any route not explicitly listed

---

## 7. Security

### 7.1 Principles
1. **Zero-secret**: the tool has and needs no configured secrets. The credential travels in the request (`request.state.token`).
2. **Defense in depth**: even if an LLM tried to call a method with malicious arguments, the method can only resolve to allowlisted routes, with typed parameters.
3. **Least privilege**: a `user` role only reaches their own data; `admin` only when their role allows it (checked at runtime via `__user__['role']`).
4. **No SSRF**: the base URL resolves from the global configuration (never from LLM/user input). There is no "URL to query" parameter.
5. **No recursion**: calling chat/completions or generation-task endpoints is not allowed (avoids tool loops and costs).

### 7.2 Implementation controls
- **`Content-Type` validation**: if the response is not `application/json`, return a clear error (protects against the SPA HTML catch-all).
- **Mapped errors**: 401 → "not authenticated / invalid token"; 403 → "no permission"; 404/`not found` → "does not exist or is not yours" (without revealing the existence of others' resources).
- **Response truncation**: size limit for the response returned to the model (prevents injecting megabytes into the context).
- **Timeout**: configurable (default 15 s).
- **No token logging**: the token must never appear in logs or in messages returned to the model.
- **Profile field whitelist (security fix 2026-08-01)**: `GET /api/v1/auths/` **echoes the request token** in its body (v0.10.2 `get_session_user` returns `token`/`token_type`/`expires_at` for the frontend's session refresh). The tool never serializes the raw profile body — `_get_my_profile` builds an explicit field whitelist (`id`, `email`, `name`, `role`, `profile_image_url`, `permissions`, timestamps) and the token fields are never included, in **either** output format.
- **Whitelist on every raw detail method (audit 2026-08-01)**: an audit of all allowlisted endpoints against the v0.10.2 source found **no other token echoes** (chats/models/files/prompts/tools/knowledge/skills responses carry no credentials). For defense in depth, the two remaining raw pass-throughs were also whitelisted: `get_chat` keeps `id/title/chat/messages/dates/share_id/pinned/archived` (drops `user_id`, `meta`, `tasks`, `summary`, `folder_id`); `get_skill` keeps `id/name/description/content/is_active/dates/meta` (drops the embedded owner `UserResponse` — another user's email for shared skills — plus `access_grants`/`write_access`). List methods were already summarized with explicit field picks.
- **Output-boundary guards (hardening 2026-08-01)**: whitelists are per-method and manual, so two guards run at the output boundary to also stop *accidental* leaks (a future method, or a future server version echoing a credential under an unexpected field): (1) `_sanitize` recursively drops any dict key whose name looks like a credential (`token`, `api_key`, `password`, `secret`, `authorization`, …) when its value is a **non-empty string** — boolean permission flags named e.g. `api_keys` are kept; (2) `_run` redacts the **raw token string** from any output (success or error) before returning. A static tripwire test (`test_security.py`) pins that no method passes a raw server body into `_ok`.

- **File deletion is the only write operation (2026-08-03)**: `delete_files(file_ids)` validates the whole id list up front (one invalid id rejects the call before any request), then per file: `GET` the metadata (report what disappears; a 404 never reaches the `DELETE`) and `DELETE /api/v1/files/{id}`. The backend re-verifies authorization (`file.user_id == user.id` or admin or write access — otherwise 404), cleans up KB associations + embeddings, removes the object from storage and the vector index. Deletion is **irreversible**; the model/user decides per call. A per-file failure is reported by id without aborting the rest (bounded by `MAX_DELETE_FILES`). Orphan detection is intentionally NOT a tool method: the model derives it from `get_my_files()` (`origin_chat_id`) + `get_my_chats()`.

### 7.3 User isolation (verified in tests)
- `GET /api/v1/chats/11111111-…-1111` (nonexistent UUID) → **401 "We could not find what you're looking for"**: no existence leak.
- `GET /api/v1/users` with role `user` → **401 no permission**: no privilege escalation.
- The API filters by the token's user on all tested endpoints (`chats`, `files`, `prompts`, `tools`).

---

## 8. Functional design of the tool

### 8.1 Tool type
- Standard Open WebUI **server-side tool** (`Tools` class with `Valves`), installable from *Workspace → Tools*, assignable to models (like the instance's existing `smart_fetch_url` tool).
- Compatible with **native** function calling and with legacy mode.

### 8.2 Configuration parameters (Valves)

| Valve | Default | Purpose |
|---|---|---|
| `fallback_base_url` | `http://localhost:8080` | Last resort if the global URL is empty or unreachable |
| `timeout` | `15` | HTTP timeout in seconds |
| `max_response_chars` | `8000` | Truncation of the response returned to the model |
| `output_format` | `markdown` (per-user valve, default `markdown`) | Format of the response returned to the model: `markdown` (default, see §8.8) or `json`. Chosen per user from the chat session; no admin valve |
| *(no credential valves)* | — | **By design**: auth is automatic via `request.state.token` |

### 8.3 Method signatures

Each method receives the parameters injected by the Open WebUI runtime when declared:

```text
async def <method>(self, __user__: dict, __request__: Request, <typed parameters>)
```

- `__user__` → for role checks (`__user__.get('role') == 'admin'`).
- `__request__` → to read `request.state.token` (session JWT or API key).
- Business parameters (e.g. `chat_id: str`, `text: str`, `limit: int`) typed and validated by the tool's Pydantic/schema.

### 8.4 Internal flow of a query

```text
1. Resolve base URL (hierarchy §4.2)
2. Get token: __request__.state.token
   └─ if missing → return clear error ("no token context; is it running from the UI/API?")
3. Compose HTTP GET call to the allowlisted route
   headers = { Authorization: Bearer <token> }
4. Validate Content-Type == application/json (if not → error)
5. Map HTTP errors (401/403/404/5xx) to readable messages
6. Truncate response (max_response_chars)
7. Return the result to the model in the configured format (Markdown by default, §8.8)
```

### 8.5 Optional events (UX)

Via `__event_emitter__`, statuses can be emitted during execution (implemented in Iteration 4):
- `status` "Querying your chats…" (start, `done=False`)
- `status` same action with `done=True` (completion — stops the shimmer)
- `chat:message:error` on failure (the error block rendered in the message)

This makes the tool's execution visible in the UI, just like `search_web` or the built-in tools.

**Verbosity (2026-08-03):** progress `status` events are gated by a `verbose` valve (admin + per-user; per-user overrides admin; default `True`). **Errors are NOT gated**: `chat:message:error` is always emitted on failure, and **consolidated** — at most one error event per tool call. A batch `delete_files` with several failures emits a single "N of M file(s) could not be deleted" summary (the per-id detail stays in the returned text), so repeated failures never flood the user with toasts.

**File attachments (2026-08-03, Option B for images):** `get_file_content` shows the file in the assistant message without dumping bytes into the context:
- **Images** → `embeds` event with an HTML `<img>` fragment (contained, rounded, capped height) rendered by `FullHeightIframe` srcdoc — the non-markdown, non-artifact embed mechanism. The returned note tells the model the image is already visible and must not embed/display it again as markdown (mirrors `generate_image`'s contract).
- **Text** → `files` event (download chip) + 100-char snippet in the returned text; never a full dump.
- **Generic binary** → `files` event (download chip) + note.
The `files`/`embeds` events are persisted by the backend into the message's field automatically and re-broadcast live; no token is put in any URL (the frontend builds download URLs with the session cookie); emission is best-effort. The image embed is sandboxed (srcdoc iframe); if a deployment blocks same-origin subrequests, `iframeSandboxAllowSameOrigin` is the lever. Item schemas pinned by `test/test_file_attachment.py`.

### 8.6 Pagination, sorting and filtering (cross-cutting requirement)

**Verified in the POC** (`/api/v1/files/`): the API **paginates responses** with an observed maximum of **50 items per page**, exposes the total (`{"items":[…], "total": N}`) and accepts `page` / `pageSize`. On the real instance: 104 files → 3 pages.

**Design consequence:** every tool function that lists or searches resources must consider:

1. **Pagination** — expose `page` / `page_size` parameters (with sensible defaults) and **iterate pages internally** when the response declares a `total` greater than what was returned, or return the `total` to the model so it can decide whether to ask for more.
2. **Sorting** — where the API allows it, accept explicit, coherent criteria per resource:
   - Chats: `updated_at`, `created_at` (most recent first by default).
   - Files: `size`, `created_at`, `filename`.
   - Prompts/tools/knowledge: alphabetical by name (or `created_at` if applicable).
3. **Filtering** — expose typed filters per resource:
   - Files: `content_type` (e.g. `image/*`), `size` range (`min_size`, `max_size` bytes), partial `filename`.
   - Chats: textual search (`/api/v1/chats/search?text=…`) and/or status filter (`pinned`, `archived`, `shared`).
   - Workspace: search by name/description when the endpoint supports it.
   - **Date-range filter for chats (design recorded, DEFERRED 2026-08-21):** `created_after`/`created_before`/`updated_after`/`updated_before` on `get_chats(scope="all", …)` (post-8.9.9 name), applied **client-side** after the tag fetch / page iteration and before sort + slice (the API exposes no date filter). Full design in §8.9.6; postponed by user decision.
4. **Client vs. server filtering strategy** — apply **server-side filtering whenever the endpoint supports it** (less data transferred, cheaper). Local filtering (in the tool) remains only for criteria the API does not expose (e.g. minimum size when the listing already carries `meta.size`).
5. **Truncation and summarization** — lists returned to the model must be **summarized** (e.g. top N results + `total`), not full dumps, to avoid saturating the context (see also `max_response_chars` in §8.2).

### 8.7 Response schemas (verified)

Documented from real responses of the instance (v0.10.2). Useful for typing the tool's return values and for the summarization logic.

#### File object (`GET /api/v1/files/`)

Verified structure — every file in the instance's listing shares these fields:

| Field | Type | Example (real) | Notes |
|---|---|---|---|
| `id` | `str` (UUID) | `643f81c9-2bc8-44d7-b4a1-994cdb1c503b` | Unique file identifier |
| `user_id` | `str` (UUID) | `16dcaa6d-7122-4cd5-bc01-823064998d75` | Owner (the token's user) |
| `filename` | `str` | `generated-image.png` | File name |
| `hash` | `str \| null` | `null` | Content hash (nullable; `null` on this instance) |
| `data` | `dict` | `{}` | Auxiliary data (empty on this instance's files) |
| `meta` | `dict` | see below | Metadata (where the useful info lives) |
| `created_at` | `int` (epoch UTC) | `1785457944` | Creation timestamp |
| `updated_at` | `int` (epoch UTC) | `1785457944` | Last update timestamp |

`meta` sub-fields (all present on every file):

| Field | Type | Example (real) | Notes |
|---|---|---|---|
| `name` | `str` | `generated-image.png` | Name (duplicates `filename`) |
| `content_type` | `str` | `image/png` | MIME type — **key for image filtering** |
| `size` | `int` | `8796` | Size in **bytes** — key for size-range filtering |
| `file_hash` | `str` | `a9fe67c4…` (SHA-256) | Content hash |
| `data` | `dict` | `{chat_id, message_id, session_id}` | Origin metadata (for generated images: the chat/message/session that produced them) |

Complete example (first item of the real listing):

```json
{
  "id": "643f81c9-2bc8-44d7-b4a1-994cdb1c503b",
  "user_id": "16dcaa6d-7122-4cd5-bc01-823064998d75",
  "hash": null,
  "filename": "generated-image.png",
  "data": {},
  "meta": {
    "name": "generated-image.png",
    "content_type": "image/png",
    "size": 8796,
    "file_hash": "a9fe67c4eee6df7e75aab3b0d607105f705e3c7aea846d052827e3239872fc0d",
    "data": {
      "chat_id": "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e",
      "message_id": "4094c125-dcd1-43ea-a9a8-a8cbfb7d1e26",
      "session_id": "9sACAkY0NsNQsGNHAAB_"
    }
  },
  "created_at": 1785457944,
  "updated_at": 1785457944
}
```

Implementation notes:
- `meta.data` is valuable for cross-referencing: generated images carry `chat_id` / `message_id` / `session_id` of their origin — the tool could answer "which chat produced this image?".
- All filtering needs (`content_type`, `size`, `created_at`) are available on the listing; no binary download is required to filter.
- The upload endpoint (`POST /api/v1/files/`) additionally returns `status`, `path` (container path) and `data.status`, which are not present in the listing — relevant only if the tool ever writes.

### 8.8 Response format: Markdown-first for the model (decision 2026-08-01)

**Problem observed:** language models often read **plain text / Markdown** more reliably than deeply nested JSON. A table with columns is immediately actionable; the equivalent JSON array of objects forces the model to mentally parse structure and reduces reliability when the model must pick fields (IDs, sizes, dates) out of it.

**Decision:** the tool returns the response to the model as **Markdown by default** (plain text, no code fences around whole payloads, no JSON). A **per-user `output_format` valve** (`markdown` default | `json`) lets each user choose JSON when a specific model handles it better — there is **no admin valve** for the format (see constraints below).

**Formatting rules** (all renderers):
- **Lists → Markdown tables.** One table per resource. The summary line above the table always states counts: `**Files: 2 (2 total on server)**`.
- **Raw values, no parsing burden on the model:**
  - Sizes are shown as **raw integer bytes** (`8796`, `152340`) — never formatted with unit prefixes (`8.8 KB`). Sizes are stored as numbers in the backend API (`meta.size` is an `int`, see §8.7), and the tool passes them through as numbers. The column header states the unit (`Size (bytes)`) so the value stays unambiguous.
  - Timestamps are rendered as readable local dates/times (e.g. `2026-07-30 08:00`) instead of epoch integers — models handle ISO-like dates better than 10-digit epochs, and the raw epoch is preserved nowhere the model must use.
  - **IDs are always present** in the table (e.g. a `ID` column), because the model needs them to call follow-up methods (`get_chat_summary(id)`, `get_file_content(id)`).
- **Details (profile, single items) → flat bullets** (`- Name: John Doe`).
- **File content → fenced block** with the content type as language hint (e.g. ` ```csv `). Binary content → a one-line note with metadata (no bytes).
- **Chat history → heading + per-message blocks** (`**user** …` / `**assistant** …`).
- **Errors → plain-text one-liners**, not JSON: `Error: Not authenticated: …`.
- **Bold only on headings** — to save tokens, `**…**` is used **only for headings/sections** (`**Profile**`, `**Permissions**`, `**Chats: N**`, `**Files: N**`, message roles in `get_chat`). Keys inside hierarchies and tables are **plain** (`- Models: false`, not `- **Models**: false`).
- **Nested objects → hierarchical bullets, never embedded JSON.** Deeply nested structures (e.g. the profile's `permissions` object, depth ~3) are rendered as an indented bullet hierarchy with humanized keys (`snake_case` → `Title Case`):
  ```markdown
  **Permissions**
  - **Workspace**
    - **Models**: false
    - **Knowledge**: true
    - **Models Import**: false
  - **Chat**
    - **Controls**: true
  ```
  Scalar values stay raw (`true`/`false`/`null`, numbers without prefixes). Multimodal chat content (a list of parts) renders the same way. This follows the research-backed JSON→Markdown strategies (llm-md / 1000+ case study): key-value bullet lists for shallow objects, tables for uniform arrays (>80% key similarity), indented hierarchy for deep nesting, and **never raw JSON embedded in a bullet** (it hurts comprehension).

**Worked examples** (what the model sees for `get_my_files()` and `get_my_chats()`):

```markdown
**Files: 2 (2 total on server)**

| Filename | Type | Size (bytes) | Created | Origin chat | ID |
|---|---|---|---|---|---|
| generated-image.png | image/png | 8796 | 2026-07-30 | b5d844f0-85c5-4cdc-8cf3-4f2366bc249e | 643f81c9-2bc8-44d7-b4a1-994cdb1c503b |
| budget-report.csv | text/csv | 152340 | 2026-07-28 | — | 5e1b76e0-9b7e-4b3e-b3b5-111111111111 |

---

**Recent chats: 2**

| Title | Updated | ID |
|---|---|---|
| Budget planning | 2026-07-30 08:00 | b5d844f0-85c5-4cdc-8cf3-4f2366bc249e |
| Ideas | 2026-07-01 | aaaa |
```

**Design constraints preserved:**
- The renderers **summarize exactly as before** (top N + `total` in the summary line; never full dumps) — Markdown is a *presentation* of the same summarized data, not an invitation to return more.
- `max_response_chars` truncation still applies to the rendered Markdown.
- The summarizer logic from §8.6/§8.7 (which fields to keep, `total`, origin cross-referencing) is unchanged; only the final serialization changes (`_ok`/`_error`/renderers).
- `output_format` is a **per-user valve** (dropdown Markdown/JSON, default Markdown) — **not an admin valve**. There is no admin-level format setting; each user chooses the format for their own chats from the session UI. The tool's built-in default is Markdown.

---

## 8.9 Improvement pass — Iteration 9 (2026-08-21)

Consolidated improvement plan written 2026-08-21 (the same brief whose findings are in PLAN.md Iteration 9). **Live + v0.10.2-source research completed 2026-08-21: two brief claims corrected** (`tag:` is already a scope limiter server-side — §8.9.1; the stats anomaly is fully root-caused — §8.9.3). **Tasks 8.9.1–8.9.4 implemented (v0.17.0–v0.20.0); 8.9.5 pending; one task (the `get_my_chats` date-range filter) is DEFERRED by user decision** — its design is recorded below so it can be picked up unchanged. All tasks preserve the read-only + allowlist security model (§7).

| # | Task | Status |
|---|---|---|
| 8.9.1 | `search_chats` `tag:` semantics — VERIFY + PIN (backend already ANDs); document the orphan-tag cleanup side effect | ✅ DONE (v0.18.0) |
| 8.9.2 | `get_file_content`: image header metadata via **Pillow** (bundled with Open WebUI) — width/height/mode/bits | ✅ DONE (v0.20.0) |
| 8.9.3 | `get_chat_stats` metrics — ROOT-CAUSED: recompute the two length averages (backend bug), document the other three | ✅ DONE (v0.19.0) |
| 8.9.4 | Credential non-exposure: fail-loud sanitizer + allowlist tripwire test | ✅ DONE (v0.17.0) |
| 8.9.5 | `delete_files` destructive live test (optional, sandbox only, env-gated) | ⏳ pending |
| 8.9.6 | `get_my_chats` date-range filter | ⏸️ **DEFERRED** (2026-08-21) |
| 8.9.7 | `search_chats` `folder:` prefix — fix folder-NAME search (multi-word names broken; user report 2026-08-21) | ⏳ planned |
| 8.9.8 | `search_chats` must **require a search term** (design decision 2026-08-21) — pure-prefix calls error, never list | ⏳ planned |
| 8.9.9 | Unify the chat list methods into **`get_chats(scope="all"\|"pinned"\|"shared"\|"archived")`** (default `"all"`) — replace `get_my_chats`/`get_pinned_chats`/`get_shared_chats`/`get_archived_chats` | ⏳ planned |
| 8.9.10 | Drop the **`_my_`** prefix from all method names (`get_my_profile`→`get_profile`, `get_my_files`→`get_files`, …) | ⏳ planned |

### 8.9.1 Search: `tag:` scope-limiter semantics — VERIFY + PIN (backend already implements it)

**Verified live (2026-08-21):** `pinned:` and `folder:` scope-limit the free text server-side (`"Open WebUI"` → 37; `"Open WebUI pinned:true"` → 0; `"Sulion"` → 1; `"Sulion pinned:true"` → same 1; `"manchego"` → 0 everywhere).

**Brief claim corrected:** the brief stated `tag:` is a **standalone tag filter** that relaxes the free text. **Verified false on this backend** (v0.10.2 `models/chats.py::get_chats_by_user_id_and_search_text` + live probes): the query strips every prefix and **ANDs** the text search with the tag filter (`and_(*[EXISTS(json_each(meta.tags) = tag_i)])`); multiple `tag:` prefixes are ANDed; `tag:none` uses `NOT EXISTS`. Live: `"manchego tag:comfyui"` → 0 and `"zzz_nonexistent_xyz tag:comfyui"` → 0 (a standalone filter would return the tag's 3 chats); `"Open WebUI tag:comfyui"` → 1 = the only tag chat containing “Open WebUI”. **No scope-limiter code change is needed.**

**What remains (the real work):**
1. **Pin the semantics with tests** (mock + env-gated live): text+tag AND, multi-tag AND, `tag:none` untouched, results consistent with `get_my_chats(tag=…)`.
2. **Document the orphan-tag cleanup (no code — intended behavior):** a `tag:` query with zero matches triggers the backend's **deliberate lazy GC**: the catalog entry (`tag` row) is deleted; per-chat inline `meta.tags` are untouched (they recreate the entry when the chat is updated); a typo'd/nonexistent tag deletes nothing. The tool must **not** guard or block it (blocking would break intended behavior). Documentation notes only: (a) `search_chats` / `get_my_chats(tag=)` are read-only *queries* but can carry this write side effect — docstring line; (b) **archived asymmetry:** `search` excludes archived chats (`Chat.archived == False`), `POST /chats/tags` does not — a tag used only on archived chats is cleaned via `search_chats("tag:X")` yet still visible via `get_my_chats(tag=)`. Upstream semantics; documented, not fixed.
3. **Snippet caveat (document):** the backend snippet is built from the plain `content` string only — for v0.10.2 assistant messages (text in `output[].content[].text`) the snippet is usually absent even when the match is in assistant text. Not a tool bug; do not compensate.

**Acceptance:** tests pin the AND semantics; the orphan-tag cleanup and the archived asymmetry are documented in the docstrings (no behavioral guard added).

### 8.9.2 Image header metadata for `get_file_content` — DONE (v0.20.0)

**Context:** images previously returned only name/MIME/size/id (+ inline embed). The model could not answer "what resolution/color depth is this image?".

**Design (revised 2026-08-21 — user direction: use an existing library, don't hand-roll parsers):** **Pillow**, already bundled with Open WebUI (12.2.0) and used internally for image handling. `Image.open(io.BytesIO(body))` is **lazy**: it parses the header only and never decodes pixel data (no `Image.load()`), so the cost is O(1) regardless of file size; the body is already in memory from `_api_get_raw`. Defensive import (`from PIL import Image`, degraded to `None`) — outside Open WebUI the tool still works without the extra fields. `requirements: httpx, Pillow`.

Output fields (all optional; a bad/truncated file or missing Pillow → the fields are omitted, the call never errors): `width`, `height` (resolution in px), `color_mode` (the Pillow mode: RGB, RGBA, L, P, CMYK, …), `bit_depth` (bits per channel, derived from the mode via a small map — Pillow only exposes `img.bits` for some modes). Non-image files unaffected.

**Rendering:** markdown binary renderer gains one line `Image: 1024×768 px, RGB (8-bit)`; json carries the same fields; the "already embedded / do not re-embed" note stays.

### 8.9.3 Chat usage-stats semantics — ROOT-CAUSED (2026-08-21)

**Anomaly (live 2026-08-21, chat `cc7caaa6-fc56-4117-a685-c2e7955fb2ac` — 52 branch steps: 26 user + 26 assistant; 24 assistant carry readable text, 2 are pure `reasoning` steps with `content=''`):** `average_assistant_message_content_length` = `0.0`; `last_message_at` (2026-08-18 05:43) ≠ `updated_at` (2026-08-19 14:34); `message_count` 52 (stats) vs 50 (summary/metadata).

**Verified root cause (v0.10.2 `routers/chats.py::get_session_user_chat_usage_stats`):**

| Metric | Stats value | Source in v0.10.2 | Explanation | Action |
|---|---|---|---|---|
| `message_count` | 52 | `len(get_message_list(messages_map, currentId))` — the **main branch**, every step | The tool's count (50) = text-bearing messages (`_message_text`), excluding the 2 `reasoning` steps. Stats counts *steps*; the tool counts *readable messages*. Both internally consistent | **Document** semantics; do NOT change the tool count |
| `average_assistant_message_content_length` | `0.0` | `sum(len(message.get('content','')) for assistant msgs) / n` | **Backend bug:** v0.10.2 assistant text lives in `output[].content[].text`; plain `content` is `''` for every assistant message → `0.0` for ANY chat with assistant messages (live: 24/26 have real text, still 0.0). `len()` of multimodal `content` lists also counts elements, not chars. The export route (`_process_chat_for_export::get_message_content_length`) handles strings+lists correctly — the usage route does not | **Fix in the tool:** recompute both length averages from the ChatResponse (parsed with the existing `_message_text`); keep backend values as `…_backend` |
| `last_message_at` | 2026-08-18 05:43 | `message_list[-1].get('timestamp')` — timestamp of the **last message** on the main branch | `updated_at` (2026-08-19 14:34) is the **chat row** timestamp, moved by renames/edits/any row update (~1.4 days after the last message here). Different sources, both legitimate | **Document** (last-message time vs row-update time) |
| `history_message_count` | 52 | `len(messages_map)` — the **whole tree** (all branches) | Equal to `message_count` only because this chat has no alternate branches; diverges by design otherwise | **Document** |

**Decision:** recompute the two length averages in `_get_chat_stats` from the ChatResponse (shared `_chat_metadata_payload` fetch — no extra request) using real text lengths; document the other three metrics in the docstring; tests pin the recompute path.

### 8.9.4 Credential non-exposure: fail-loud guard + allowlist tripwire

**Context:** no credential values are serialized today (`_sanitize` + `_redact` + per-method whitelists + the static no-raw-body tripwire, §7.2). The brief asks for a *defensive* guarantee covering **future** endpoints too.

**Design:**
1. **Fail-loud sanitizer:** `_ok` logs `logger.warning` (**key name only**, never the value) whenever `_sanitize` drops a credential-named key with a non-empty string value — a future leaking method becomes visible in the server log instead of being silently cleaned. Output behavior unchanged.
2. **Allowlist tripwire (static test):** extract every `_ROUTE_* = "…"` assignment from `owui_meta.py` and fail if any matches a secret-bearing pattern (the §6.3 list: `auths/api_key`, `tools/id/{id}/valves(+/user)`, `tools/id/{id}`, `knowledge/external/connections*`, `*/admin/*` configs). A future developer adding a credential route is blocked at test/review time — "blocked by default".
3. **Documented guarantee** (this section): no meta endpoint returns credential values; the full control stack is the per-method whitelists → `_sanitize` → `_redact` → the two static tripwires (no-raw-body, no-secret-route).

### 8.9.5 `delete_files` destructive test (optional, sandbox only)

Env-gated (skipped by default) behind `OWUI_META_DESTRUCTIVE_TESTS=1` (+ `OWUI_META_LIVE_URL`/`OWUI_META_LIVE_TOKEN`): upload a disposable file (`POST /api/v1/files/`, unique random content), `delete_files([id])`, assert success + subsequent 404; foreign-file rejection via `OWUI_META_LIVE_TOKEN2` when present (clean per-id failure, rest of the batch unaffected); cleanup residue. Never runs against production by construction (opt-in env var + documented sandbox requirement).

### 8.9.6 `get_my_chats` date-range filter — ⏸️ DEFERRED (user decision 2026-08-21)

**Status:** design recorded for a future version; **not in the current scope** (the manual workaround — sort by `created_at` asc and pick the range — remains).

**Context (why it was proposed):** no native date filter; "list chats from June" requires manual range selection (the account's earliest chat is 2026-06-29, so June chats are the first 5 when sorted by `created_at` asc).

**Design (backward-compatible, all new params optional):**

```python
get_my_chats(limit=10, sort_by="updated_at", sort_order="desc", tag=None,
             created_after=None, created_before=None,
             updated_after=None, updated_before=None)
```

- **Value acceptance:** epoch int/float **or** ISO date/datetime strings (`"2026-06-01"`, `"2026-06-01 12:00"`, `"2026-06-01T12:00:00Z"`); a tolerant `_parse_ts` converts to epoch UTC (partial dates → midnight UTC).
- **Range semantics:** **half-open `[after, before)`** — `created_after` inclusive, `created_before` exclusive (June = `created_after="2026-06-01", created_before="2026-07-01"`); same for `updated_*`.
- **Application:** client-side, after the tag fetch / page iteration and **before** sort + slice (the API exposes no date filter). Composable with `tag`, `sort_by`, `sort_order`.
- `_render_chats` unchanged; the summary header can note the applied range (e.g. `June 2026`).

---

## 9. Lessons from the tests (for implementation)

1. **Session token and API key are interchangeable** for the tool's purposes: both land in `request.state.token` and the API accepts them equally.
2. **Nonexistent routes return HTML with 200**: always validate `Content-Type`.
3. **`/api/v1/chats/search` uses `?text=`, not `?q=`** (verified by FastAPI's 422).
4. **RAG is de facto disabled** on the instance (global bypass): no point including it in the allowlist.
5. **OpenAPI documentation is disabled** on the instance: the endpoint map was validated against the exact tag's source code (v0.10.2), which is the authoritative source.
6. **Router prefixes are defined in `main.py`** (`include_router(..., prefix='/api/v1/…')`), not in each router: any version upgrade must be reviewed there.
7. **Pagination is mandatory** (files POC): observed `pageSize` max 50, `total` in the response. `GET /api/v1/files/` returned 50 items with `total: 104`; 3 pages had to be iterated to list everything. Also, `content_type` and `size` (bytes) live in each item's `meta` — type/size filtering is done on the listing, without downloading binaries.
8. **The trailing slash matters, and it is NOT uniform.** Verified live (2026-08-01): the **listing routes** (`/api/v1/auths/`, `/api/v1/chats/`, `/api/v1/files/`, `/api/v1/prompts/`, `/api/v1/tools/`, `/api/v1/knowledge/`, `/api/v1/users/`) require a **trailing slash** — without it they fall through to the SPA HTML catch-all (HTTP 200, `text/html`). But the **sub-resources** (`/api/v1/chats/search`, `/pinned`, `/shared`, `/api/v1/chats/{id}`, `/api/v1/files/{id}/content`) and `/api/models` must **NOT** have a trailing slash — with one they fall to the SPA catch-all too. The allowlist must fix the canonical form of each route individually, not rely on a uniform rule or redirects (FastAPI/Starlette does not 307-redirect here; the SPA catch-all absorbs the miss). **Same applies to the newer routes (2026-08-20):** `folders/` WITH slash; `chats/all/tags`, `chats/archived`, `chats/stats/usage` WITHOUT.
9. **The chat list omits organization metadata by design.** `GET /api/v1/chats/` returns `ChatTitleIdResponse` only — no `meta`/tags/folder/pinned/archived on the items — and the default query **hides folder + pinned chats** unless `include_folders=true&include_pinned=true` (verified live 2026-08-20). Any tool feature needing per-chat organization state (tags, folder, flags) must source it from the detail endpoint (`ChatResponse` carries `meta.tags`, `folder_id`, `pinned`, `archived`), the tags catalog (`GET /api/v1/chats/all/tags`), the usage-stats endpoint (`GET /api/v1/chats/stats/usage` — tags + message_count), or the folders router (`GET /api/v1/folders/`) — never from the list items.
10. **Search filter prefixes are scope limiters — including `tag:` (verified live + v0.10.2 source, 2026-08-21).** The brief claimed `tag:` was a standalone filter; **verified false**: `get_chats_by_user_id_and_search_text` strips all prefixes and ANDs the text search with `EXISTS(meta.tags = …)`; multi-tag is AND; `tag:none` is `NOT EXISTS`. Live: `"manchego tag:comfyui"` → 0 (a standalone filter would return the tag's 3 chats). No client-side normalization is needed. **Related intended behavior (documented, not a bug):** a `tag:` query with zero matches triggers the backend's **orphan-tag cleanup** — the catalog entry is deleted (per-chat inline `meta.tags` untouched; a typo'd tag deletes nothing). Tool-relevant nuance: `search` excludes archived chats, `POST /chats/tags` does not, so a tag used only on archived chats is cleaned via `search_chats("tag:X")` yet visible via `get_my_chats(tag=)` — documented upstream asymmetry (Iteration 9 task 1, §8.9.1).
11. **The `folder:` search prefix is broken for multi-word folder names (verified live + source, 2026-08-21).** `get_chats_by_user_id_and_search_text` splits the text on spaces, so `folder:Open WebUI meta` queries only `"Open"`; `search_folders_by_names` requires an **exact normalized match** of the full name (`[\s_]+`→space, lowercase) → no match → the folder filter is silently skipped and the leftover words leak into the free text; the folder **id never matches** (names only). The backend's own normalization makes the **underscore-joined single token** work (`folder:open_webui_meta`, `_`≡space — live: 38 chats). `GET /api/v1/chats/folder/{id}` → 401 for the user role on this instance. The tool must resolve folder names client-side (greedy phrase match + underscore rewrite + leak stripping + clean error for unknown folders) — Iteration 9 task 9.7 (§8.9.7).

---

## 10. Tests performed (evidence)

### 8.9.7 Search: `folder:` prefix — folder-name resolution ⏳ PLANNED (2026-08-21)

**User report:** searching chats by folder does not work with the **folder name** — impractical, terrible UX.

**Root cause (verified live + v0.10.2 source):** the search text is split on spaces, so `folder:Open WebUI meta` sends only `"Open"` as the folder query; `search_folders_by_names` requires an **exact normalized match** (`[\s_]+`→space, lowercase) of the full name → no match → the folder filter is silently skipped and the leftover words (`WebUI meta`) leak into the free text. The folder **id never works** (the backend matches names, not ids). Live: `folder:Open` / `folder:WebUI` / `folder:<uuid>` → full unfiltered page (60); `folder:Open WebUI meta` → the same result as the plain text search "WebUI meta" (a leak artifact). `GET /api/v1/chats/folder/{id}` (the id-based route) → 401 for the user role on this instance.

**Design (`_search_chats`, zero new routes):** greedy multi-word phrase matching against the user's folders (`GET /api/v1/folders/`, already allowlisted; backend normalization semantics) → **rewrite to the underscore-normalized single token** (`folder:open_webui_meta` — the backend treats `_`≡space, so it matches exactly; verified live: 38 chats for the long folder name) → **strip the consumed words** from the free text (kills the leak) → **unknown folder → clean error listing the valid names** (no more silent no-filter). Mixed text stays AND (`"foo folder:Open WebUI meta"` → `foo folder:open_webui_meta`).

### 8.9.8 `search_chats` — search term required (design decision 2026-08-21)

**Decision:** `search_chats(text)` must require a **textual search term**. Calls whose tokens are ONLY UI filter prefixes (`pinned:true`, `tag:meta`, `folder:<name>`, `tag:none`, …) must **error** — never return a full listing. The prefixes stay as **optional refinements** of an actual text search; pure filtered listing belongs to the list tools (`get_chats(scope=…)`, `get_folders`, … — see 8.9.9/8.9.10), never to `search_chats`.

**Why:** `search_chats` was being (mis)used as a filtered listing — `"pinned:true"` / `"tag:none"` returned full listings, and `"folder:Open WebUI meta"` returned 1 chat instead of the folder's (the 9.7 bug). Searching (text matching) and listing (filtered collections) must stay separate: predictable ("nothing searched" ≠ "nothing found") and correct API usage (list tools already exist).

**Implementation (`_search_chats`):** tokenize by whitespace; if no token survives after removing the UI prefixes (`tag:`/`folder:`/`pinned:`/`archived:`/`shared:`) → **`ToolError`** pointing to the list tools; otherwise proceed (9.1 tag AND + 9.7 folder resolution apply to the remaining text + prefixes). Signature unchanged.

**Alternatives considered:** empty result instead of error (rejected — indistinguishable from "nothing found"); keep hybrid search+listing (rejected — the current confusing state); a `list_only` flag (rejected — redundant with list tools).

**Synergies:** the lone-`tag:` **orphan-tag cleanup becomes unreachable via `search_chats`** (pure-prefix calls error before the backend — simplifies §8.9.1); `get_chats(tag=)` keeps its documented cleanup side effect; the 9.7 folder fix remains needed for text+folder combos.

### 8.9.9 Unify chat listing into `get_chats(scope=…)` (design decision 2026-08-21)

**Decision:** replace `get_my_chats`/`get_pinned_chats`/`get_shared_chats`/`get_archived_chats` with one **`get_chats(scope="all"|"pinned"|"shared"|"archived", limit=10, sort_by="updated_at", sort_order="desc", tag=None)`**. **Omitted `scope` → `"all"`** (user decision): `get_chats()` ≡ today's `get_my_chats()` (list with `include_folders`/`include_pinned` + optional `tag`). The four methods are the same resource with the same result shape and near-identical params — a single documented `Literal` scope kills the model's guessing (`get_pinned` vs `get_shared`). Backend/allowlist unchanged per scope (chats listing + `POST /chats/tags`, `/chats/pinned`, `/chats/shared`, `/chats/archived`); `"Archived chats"` label kept; invalid `scope` → clean `ToolError`. The deferred date-range filter (8.9.6) applies to `scope="all"`.

### 8.9.10 Drop the `_my_` prefix from method names (design decision 2026-08-21)

**Decision:** rename all `get_my_*` public methods to `get_*` (`get_profile`, `get_files`, `get_prompts`, `get_tools`, `get_skills`, `get_folders`, `get_tags`; `get_my_chats` absorbed into `get_chats` — 8.9.9). The tool only ever sees the requesting user's data (token-scoped), so `my` is redundant. **Breaking change** (no server-side alias in Open WebUI tools): stored history referencing the old names shows unresolved calls; the model re-learns the new names from the docstrings; README documents it.

### 10.1 Connectivity (no credentials)
| Test | Result |
|---|---|
| `GET /health` | ✅ 200 (0.08 s) |
| `GET /api/version` | ✅ `{"version":"0.10.2"}` |
| `GET /api/config` | ✅ public (auth active, signup off) |
| `GET /api/models` without token | ✅ 401 `Not authenticated` |

### 10.2 Authentication (with `sk-…` API key, role `user`)
| Test | Result |
|---|---|
| `GET /api/v1/auths` | ✅ profile "John Doe", role `user`, email `john.doe@example.com`, full permissions |
| `GET /api/models` | ✅ visible models (e.g. `deepseek-v4-coding-assistant`) |
| `GET /api/v1/chats` | ✅ only the requester's chats |
| `GET /api/v1/chats/{id}` | ✅ full chat with history |
| `GET /api/v1/chats/search?text=gastos` | ⏳ format pending confirmation (parameter `text` confirmed by 422) |
| `GET /api/v1/files` | ✅ only the requester's files |
| `GET /api/v1/files/{id}/content` | ✅ `image/png` 8796 B |
| `GET /api/v1/knowledge` | ✅ `{"items":[],"total":0}` |
| `GET /api/v1/prompts` | ✅ prompts (e.g. "Get current news") |
| `GET /api/v1/tools` | ✅ tools (e.g. "Enhance Image") |
| `GET /api/v1/users` (user role) | ✅ 401 no permission (isolation) |
| `GET /api/v1/chats/{nonexistent UUID}` | ✅ 401 not found (no leak) |
| `POST /api/v1/retrieval/query` | ✅ 405 (RAG non-operational, out of scope) |

---

The manual evidence above is now also enforced **automatically** by the env-gated live suite `test/test_live.py` + isolation checks `test/test_isolation.py` (Iteration 5, committed 2026-08-20): re-validates the endpoint map, the `/auths/` token-echo protection, SPA-HTML/redirect traps, `/users` blocked for a user role, per-user data scoping, and the no-token-in-output guard — against a live instance when `OWUI_META_LIVE_URL` / `OWUI_META_LIVE_TOKEN` are set (e.g. `source /tmp/owui_live.env`), skipped otherwise.

---

## 11. Implementation roadmap

### Phase 1 — MVP (recommended)
- [ ] Tool with the read-only methods of allowlist §6.1
- [ ] Base URL resolution (§4.2) and authentication via `request.state.token` (§3)
- [ ] `Content-Type` validation, error mapping, truncation, timeout
- [ ] Publish in *Workspace → Tools* and test on a function-calling model
- [ ] Isolation test with a second user (verify they only see their own data)

### Phase 2 — Extension
- [ ] ~~Admin methods (§6.2) with role check~~ — **DEFERRED to a future version (2026-08-01)**
- [ ] Status events with `__event_emitter__` (§8.5)
- [ ] **Pagination, sorting and filtering** across all list/search functions (§8.6): iterate pages, per-resource sort criteria, typed filters (type/size for files, text/status for chats) and smart result summarization (e.g. list titles without full history)
- [ ] **Markdown-first output** (§8.8): renderers per resource (tables for lists, bullets for details, fenced blocks for content), per-user `output_format` valve, raw sizes in bytes

### Phase 3 — Other considerations
- [ ] Evaluate whether it's worth proposing as a PR to the Open WebUI core (e.g. exposing an explicit `__token__` in `extra_params`, in addition to `__request__`)
- [ ] Document rate/cost limits (calls to `/api/v1/chats/{id}` with large histories)

---

## 12. Open questions / pending decisions

1. **Read-only methods or some controlled writes?** (v1 proposal: read-only)
2. **Expose a chat's full history or only metadata?** (the endpoint returns everything; truncation/summarization is advisable)
3. **Include `get_shared_chats` / `get_pinned_chats` in the MVP?** (verified as routes in v0.10.2, not tested with data)
4. **Integrate the tool into a specific instance model or make it available to all models the admin decides?**
5. **What is the pagination default per resource?** (e.g. chats: latest 10; files: all of page 1 with `total`; search: top N by relevance) — to be defined in Phase 1 with the desired UX.
6. **Expose `page`/`page_size` to the model or do transparent internal iteration?** (recommended: transparent with a maximum page limit to prevent cost abuse)
7. **Response format for the model** — **RESOLVED (2026-08-01):** Markdown-first by default (per-user `output_format` valve, see §8.8). JSON remains available as an opt-in choice for models that handle it better.
