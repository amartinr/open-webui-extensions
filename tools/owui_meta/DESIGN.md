# Design Document — Meta-tool for Querying the Open WebUI Internal API

**Version:** 1.0
**Date:** 2026-07-31
**Author:** Abel (with technical assistance)
**Status:** Design validated through real-world tests against the `open-webui.private` instance (v0.10.2)

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
- **Writing/deleting** user data: the tool is **read-only** in its first version.
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

Sources: **real curl tests with a `user`-role API key** against `http://open-webui.private/`, and **source-code extraction of the v0.10.2 tag** (routers + `main.py` prefixes).

### 5.1 ✅ Verified working (role `user`)

| Area | Endpoint | Observed response |
|---|---|---|
| Profile | `GET /api/v1/auths/` (trailing slash) | Full profile: id, name, email, role, permissions. **⚠️ The body ALSO echoes the request token** (`token`/`token_type`/`expires_at` — `get_session_user` supports the frontend's session refresh); the tool **field-whitelists** the profile and the token never reaches the model (§7.2) |
| Models | `GET /api/models` | OpenAI-compatible: `{"data":[{id, name, owned_by, info…}]}` |
| Chats | `GET /api/v1/chats/` (trailing slash) | Array of your chats `{id, title, updated_at, created_at, …}` |
| Chats | `GET /api/v1/chats/{id}` | Full chat with message history |
| Chats | `GET /api/v1/chats/search?text=…` | Search (parameter confirmed: `text`, not `q`); **no trailing slash** |
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

| Tool method (suggested name) | Internal route |
|---|---|
| `get_my_profile()` | `GET /api/v1/auths` |
| `get_models()` | `GET /api/models` |
| `get_my_chats(limit)` | `GET /api/v1/chats` |
| `get_chat(chat_id)` | `GET /api/v1/chats/{id}` |
| `search_chats(text)` | `GET /api/v1/chats/search?text=` |
| `get_shared_chats()` | `GET /api/v1/chats/shared` |
| `get_pinned_chats()` | `GET /api/v1/chats/pinned` |
| `get_my_files()` | `GET /api/v1/files` |
| `get_file_content(file_id)` | `GET /api/v1/files/{id}/content` |
| `get_my_prompts()` | `GET /api/v1/prompts` |
| `get_my_tools()` | `GET /api/v1/tools` |
| `get_knowledge_bases()` | `GET /api/v1/knowledge` |
| `get_my_skills()` | `GET /api/v1/skills` |
| `get_skill(skill_id)` | `GET /api/v1/skills/id/{skill_id}` |

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

Via `__event_emitter__`, statuses can be emitted during execution:
- `status` "Querying your chats…" (start)
- `status` "N chats found" (end)
- `status` with error on failure

This makes the tool's execution visible in the UI, just like `search_web` or the built-in tools.

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
  - **IDs are always present** in the table (e.g. a `ID` column), because the model needs them to call follow-up methods (`get_chat(id)`, `get_file_content(id)`).
- **Details (profile, single items) → flat bullets** (`- Name: Abel`).
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

## 9. Lessons from the tests (for implementation)

1. **Session token and API key are interchangeable** for the tool's purposes: both land in `request.state.token` and the API accepts them equally.
2. **Nonexistent routes return HTML with 200**: always validate `Content-Type`.
3. **`/api/v1/chats/search` uses `?text=`, not `?q=`** (verified by FastAPI's 422).
4. **RAG is de facto disabled** on the instance (global bypass): no point including it in the allowlist.
5. **OpenAPI documentation is disabled** on the instance: the endpoint map was validated against the exact tag's source code (v0.10.2), which is the authoritative source.
6. **Router prefixes are defined in `main.py`** (`include_router(..., prefix='/api/v1/…')`), not in each router: any version upgrade must be reviewed there.
7. **Pagination is mandatory** (files POC): observed `pageSize` max 50, `total` in the response. `GET /api/v1/files/` returned 50 items with `total: 104`; 3 pages had to be iterated to list everything. Also, `content_type` and `size` (bytes) live in each item's `meta` — type/size filtering is done on the listing, without downloading binaries.
8. **The trailing slash matters, and it is NOT uniform.** Verified live (2026-08-01): the **listing routes** (`/api/v1/auths/`, `/api/v1/chats/`, `/api/v1/files/`, `/api/v1/prompts/`, `/api/v1/tools/`, `/api/v1/knowledge/`, `/api/v1/users/`) require a **trailing slash** — without it they fall through to the SPA HTML catch-all (HTTP 200, `text/html`). But the **sub-resources** (`/api/v1/chats/search`, `/pinned`, `/shared`, `/api/v1/chats/{id}`, `/api/v1/files/{id}/content`) and `/api/models` must **NOT** have a trailing slash — with one they fall to the SPA catch-all too. The allowlist must fix the canonical form of each route individually, not rely on a uniform rule or redirects (FastAPI/Starlette does not 307-redirect here; the SPA catch-all absorbs the miss).

---

## 10. Tests performed (evidence)

### 10.1 Connectivity (no credentials)
| Test | Result |
|---|---|
| DNS `open-webui.private` | ✅ `172.16.1.1` |
| `GET /health` | ✅ 200 (0.08 s) |
| `GET /api/version` | ✅ `{"version":"0.10.2"}` |
| `GET /api/config` | ✅ public (auth active, signup off) |
| `GET /api/models` without token | ✅ 401 `Not authenticated` |

### 10.2 Authentication (with `sk-…` API key, role `user`)
| Test | Result |
|---|---|
| `GET /api/v1/auths` | ✅ profile "Abel", role `user`, email `amartinr@lowendlab.com`, full permissions |
| `GET /api/models` | ✅ visible models (e.g. `deepseek-v4-coding-assistant`) |
| `GET /api/v1/chats` | ✅ only Abel's chats |
| `GET /api/v1/chats/{id}` | ✅ full chat with history |
| `GET /api/v1/chats/search?text=gastos` | ⏳ format pending confirmation (parameter `text` confirmed by 422) |
| `GET /api/v1/files` | ✅ only Abel's files |
| `GET /api/v1/files/{id}/content` | ✅ `image/png` 8796 B |
| `GET /api/v1/knowledge` | ✅ `{"items":[],"total":0}` |
| `GET /api/v1/prompts` | ✅ prompts (e.g. "Get current news") |
| `GET /api/v1/tools` | ✅ tools (e.g. "Enhance Image") |
| `GET /api/v1/users` (user role) | ✅ 401 no permission (isolation) |
| `GET /api/v1/chats/{nonexistent UUID}` | ✅ 401 not found (no leak) |
| `POST /api/v1/retrieval/query` | ✅ 405 (RAG non-operational, out of scope) |

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
