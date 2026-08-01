# Image to File Storage — Design Document

## Problem

When a user uploads an image via the `+` button in Open WebUI:

1. The image is **already persisted** as a permanent file (upload API →
   `files` table + disk storage). A file reference `{id, type: "image",
   url: "/api/v1/files/{id}/content", ...}` is returned.

2. The file reference is at `body["files"]` (top-level key) inside
   `process_chat_payload()`. After the filter pipeline runs,
   `process_chat_payload` moves it into `metadata["files"]`.

3. **`chat_completion_files_handler()`** processes `metadata["files"]`
   through the RAG pipeline (embedding, retrieval, reranking). For images
   this is wasteful — vectorizing pixel data yields no useful context.

4. For messages reconstructed from DB history, stored `message["files"]`
   with `type: "image"` are converted to `image_url` content blocks,
   which later get converted to **base64** by
   `convert_url_images_to_base64()`.

The result: wasted RAG compute on images + bloated LLM context with
~500KB base64 strings that a non-vision model cannot process.

## Solution

A **Filter** (class `Filter`) registered in the filter pipeline that
runs before `chat_completion_files_handler`. It does the following:

1. **Strips image file refs from `body["files"]`** — so RAG never sees them.
2. **Strips `image_url` blocks** from message content — by the time the
   filter runs, `convert_url_images_to_base64()` has already converted
   them to base64 in memory, so stripping them is what keeps that base64
   out of the LLM context (the filter does not *prevent* the conversion;
   it removes the result of it from the payload).
3. **Injects `<attached_files>` block** with `<file>` tags into the last
   user message, matching `add_file_context()` format, so the model knows
   images were attached and can reference them by absolute file URL.

## Execution Order

```
User message (with image uploaded via "+")
    │
    ▼
 convert_url_images_to_base64()   ──► base64 in message content
    │
    ▼
 Image to File Storage (priority 0)
    │  ├── body["files"] ──► remove image entries
    │  └── message content ──► strip image_url blocks
    ▼
 files = form_data.pop('files', None)   ──► no images left
    │
    ▼
 chat_completion_files_handler()   ──► RAG only on non-image files
    │
    ▼
 LLM receives text + <attached_files> tags
```

## Key Design Decisions

### 1. Matches add_file_context() format

The injected `<file>` tags match Open WebUI's `add_file_context()`
format exactly. The model sees the same XML structure regardless of
source. The block is deterministic (same input → same output), preserving
prefix-based context caching.

### 2. Absolute file URLs for downstream tools

The injected `<file>` tags carry **absolute** URLs resolved from the
admin-configured "WebUI URL" (`webui.url`), falling back to
`request.base_url` when unset. Downstream tools (e.g. ComfyUI nodes that
load images by URL) can fetch the image directly without knowing the
host. The `id` attribute is preserved so Open WebUI's builtin `view_file`
tool keeps working unchanged. Relative paths are prefixed only when they
start with `/`; data URIs and already-absolute URLs pass through
unchanged.

### 3. No duplicate persistence

For images uploaded via the `+` button, the filter detects the existing
file ID in `body["files"]` and does not persist again — the file is
already on disk. Since **v2.12.1** it also records each such file's
content hash (`meta["file_hash"]`) so the `image_url` copy of the same
image in the current message reuses that file instead of persisting a
second one (see "Content-Hash Deduplication" below).

For pasted images (Ctrl+V), the filter decodes the `data:` URI **once**,
hashes the raw bytes, and looks up an existing file owned by the user
with the same `meta["file_hash"]`. On a hit it reuses that URL instead
of writing a new file; on a miss it hands the already-decoded bytes
directly to `upload_image()` (the same endpoint Open WebUI's
`get_image_url_from_base64()` delegates to, minus the second decode).
See "Content-Hash Deduplication" below.

The `__user__` dict passed to filter inlets is converted to a `UserModel`
(`_ensure_user_model()`) because `upload_file_handler` expects attribute
access (e.g. `user.email`); without this the persistence would fail. If
persistence fails, a placeholder tag `<file url="(base64 stripped)">`
is injected so the model still knows an image was attached.

### 4. Non-image files pass through unchanged

### 5. Graceful message content handling

After stripping, if a message has no content left, an empty text block
is inserted to prevent 400 errors from strict providers.

## Known Limitations

- **⚠️ TO VERIFY — model initially sees two images on `+` upload**: on a
  `+` upload the model has been observed saying "you sent two images" in
  the first turn, then "one" in later turns. Expected root cause: the
  deployed filter is **v2.11** (it persists the `image_url` copy as a
  second UUID) instead of **v2.12.1** (which reuses this-turn `+`
  uploads by content hash — see "Content-Hash Deduplication"). If it
  still happens with v2.12.1 deployed, the this-turn hash match is
  failing (e.g. `meta["file_hash"]` absent on the instance's files) —
  then add diagnostic logging and revisit. **Status: needs re-deployment
  check + verification.**
- **Pasted images are announced only once** (v2.12.0): the filter now
  only tags images from the **last user message** (the current turn's
  attachments). Re-hydrated history from earlier turns is stripped but
  not re-announced, so no image is re-injected forever. Consequence: the
  model retains conversational memory of the image but loses the file
  reference (id/URL) in later turns — if a tool needs it again the user
  re-attaches the image.
- **Re-persistence of pasted images across turns (mostly fixed)**: pasted
  images stay in the stored chat message as `data:` URIs. Since v2.12.0
  the filter only persists/announces them in the turn they are pasted
  (last user message); earlier, every turn hashed the pasted content.
  Since v2.10.0 the content-hash dedup reuses the first persisted file,
  so one paste yields one file on disk / one `files` row instead of N
  copies after N turns. Remaining caveat: the check-then-insert has no
  unique index behind it (TOCTOU), so two strictly concurrent requests
  could still write two files — worst case equals the pre-dedup
  behavior, it never reuses another user's file.
- **Core `add_file_context()` blocks remain**: with native function
  calling, the core still prepends its own `<attached_files>` block per
  stored user message *after* the filter runs (from stored
  `message.files`). Those blocks are per-message, stable and cached; the
  `agent_loop_guard` pipe collapses and deduplicates them with the
  filter's block. See "Attached-Files Accumulation — Verified Mechanism"
  below for the verified pipeline order, the two independent sources of
  blocks, and the pipe-based cleanup design.
- **Authenticated downloads**: `/api/v1/files/{id}/content` requires
  authentication (JWT or API key) and ownership checks. External tools
  fetching the absolute URL need valid credentials; the filter itself
  does not issue tokens.
- **Tool-result images are also stripped**: when a tool produces an
  image, `chat_completion_tools_handler()` turns it into an `image_url`
  block (persisting base64 or keeping the raw URL), which the filter
  then converts to a `<file>` tag like any pasted image. This is
  **intentional**: the deployment activates the vision capability on
  models that don't really have it (otherwise Open WebUI refuses to
  attach images to non-vision models), so no base64 must ever reach
  those models — references only.

  **Future option — `vision_capable_models` valve**: the filter could
  read the model's `capabilities.vision` (via `__metadata__.model`) and
  let `image_url` blocks pass through untouched for models that truly
  support vision, while still stripping them for non-vision models.
  Not implemented — would change the reference-only behaviour described
  above, and is not needed for the current deployment.

## Content-Hash Deduplication (v2.10.0, extended v2.12.1)

Implemented: option 1 of the former "Open Options" list. Pasted images
are deduplicated by content before persisting:

1. **Decode once** — `_decode_base64_uri()` turns the `data:` URI into
   `(raw_bytes, content_type)`. The base64 string is transport only;
   the stored hash is computed over the decoded bytes.
2. **Hash the bytes** — `sha256(raw)` matches the digest
   `upload_file_handler` already stores in `files.meta["file_hash"]`
   (verified against `routers/files.py`, `routers/images.py` and
   `models/files.py` on main). Both persist paths — web upload (`+`)
   and pasted images — go through `upload_file_handler`, so the same
   image pasted in two chats, or uploaded by `+` and later pasted,
   yields the same hash and reuses one file.
3. **Look up by owner + hash** — `Files.get_files_by_user_id(user.id)`
   + a Python filter on `meta["file_hash"]`. Scoping by `user_id` keeps
   the reuse correct (a file is never shared across users). Best-effort:
   any lookup error degrades to persisting a new file.
4. **Reuse or persist** — on a hit, the existing file's relative URL is
   returned (and the file is linked to the chat message via
   `Chats.insert_chat_files`, mirroring `upload_image()`); on a miss,
   the already-decoded bytes go straight to `upload_image()`, avoiding
   the second decode that `get_image_url_from_base64()` would do.

**v2.12.1 — this-turn `+` uploads take priority.** A `+` upload reaches
the filter twice in the same request: as a ref in `body["files"]` (the
file already on disk) and as an `image_url` (base64) copy in the current
message. Before v2.12.1 the base64 copy was persisted separately, minting
a second UUID — the model then saw **two** file tags for one upload (and
on later turns "rectified" to one, because the re-hydrated history only
carries one copy). v2.12.1 fixes it by:

- collecting `hash → file id` for this turn's `+` uploads (`body["files"]`,
  via `_file_hash_of`), and
- in `_persist_base64`, reusing that file when the decoded bytes' sha256
  matches — before falling back to the user-wide lookup and then to
  persisting a new file.

Now a single `+` upload yields a single file tag from the first turn.

Notes on the hash field:

- The table has **two** hashes — `meta["file_hash"]` (sha256 of the raw
  upload bytes, written by `upload_file_handler`) and the top-level
  `hash` column (set later by `routers/retrieval.py` for vector-DB
  sync). Dedup must read `meta["file_hash"]`, never `hash`.
- `upload_file_handler` accepts a client-supplied `file_hash`
  (`file_metadata.get('file_hash') or sha256(contents)`); the web
  frontend does not send one, so for the two paths this filter touches
  the stored value is always the server-computed `sha256(bytes)`.

Known trade-offs:

- **TOCTOU**: no unique index exists on `meta["file_hash"]`; two
  concurrent requests can double-insert. Accepted as best-effort.
- **Intra-user only**: the same content pasted by two different users
  stays two files (correct ownership semantics).
- **Per-user scan**: `get_files_by_user_id()` loads the user's file
  list and filters in Python — fine for typical volumes, not a DB-level
  hash lookup (the JSON-subscript query used by
  `get_pending_files_for_knowledge()` would be the heavier alternative).

## Tag Deduplication (v2.11.0)

Implemented: option 2 of the former "Open Options" list. The injected
`<attached_files>` block is deduplicated **within each request**, so the
same file is never tagged more than once.

- The inlet keeps a `seen` set keyed by file id — or by the id extracted
  from a `/api/v1/files/{id}/content` URL (relative or absolute);
  external URLs key by the full URL. Every source of tags goes through
  the same dedup: `body["files"]` refs, pasted-and-persisted images,
  re-hydrated historical `image_url` blocks.
- The v2.10.0 content-hash dedup is what makes this effective: pasted
  images now resolve to a **stable** file id, so the same image
  referenced from history and from the current message collapses to one
  tag. Without it, each turn would mint a new UUID and the `seen` set
  would have nothing to collapse (the op-1-enables-op-2 relationship).
- **Fixes the "mixed uploads" limitation**: the `has_uploaded` global
  gate — which silently dropped pasted images whenever a `+` upload
  existed — is removed. The `seen` set replaces it precisely: a `+`
  upload and its own `image_url` twin deduplicate by id, while a
  genuinely pasted image is still persisted and tagged.
- Persisted pastes now carry the **real file id** in their `<file>` tag
  (`id="{file_id}"`, returned alongside the URL by `_persist_base64()`),
  matching the `+` upload format and keeping the builtin `view_file`
  tool working.
- Synthetic placeholder tags (`url="(base64 stripped)"`) are **never**
  deduplicated: each one reports a distinct persistence failure.
- Limit: `add_file_context()` in the core middleware still prepends its
  own `<attached_files>` block after filters run; that block's
  duplicates cannot be fixed from the filter.

## Attached-Files Accumulation — Verified Mechanism (2026-08-01)

*Verified against open-webui `main` (`backend/open_webui/utils/middleware.py`,
`backend/open_webui/utils/chat.py`, `backend/open_webui/functions/__init__.py`).
Line numbers below refer to `main` at verification time. The deployed Open
WebUI version was not available for inspection — the ordering was verified
from the code path, not by runtime tracing.*

### Why a filter cannot fix the accumulation

The filter inlet runs early in `process_chat_payload()`; the core's own
`add_file_context()` runs *afterwards*; and the pipe is the last code
before the provider. A filter therefore never sees (or can fix) the blocks
the core adds after it. Order inside `process_chat_payload()` (middleware.py):

```
re-hydrate stored history  (message.files → image_url parts)
  → convert_url_images_to_base64()      [line 2396]
  → filter inlets                       [line 2517: process_filter_functions]
  → files = form_data.pop('files')      [line 2600]
  → add_file_context()                  [line 2865, only when
                                          use_builtin_tools: native FC]
  → generate_chat_completion()          (utils/chat.py)
      → model.get('pipe') → generate_function_chat_completion()
            → pipe(body, ...)           ← LAST code before the provider
```

### Two independent sources of `<attached_files>` blocks

| Source | When | Tag format |
|--------|------|------------|
| image_filter (Step 3) | in the inlet, before RAG | one block prepended to the last user message; absolute URLs (`webui.url`); `type="image"` + `id` |
| core `add_file_context()` (middleware.py:1570) | after all filters, per stored user message that has non-`data:` `files` | one block **per user message**; URLs as stored (relative `/api/v1/files/{id}/content`); `type` from stored file |

Both can be present in the final payload: with native function calling the
same turn can carry the filter's block **plus** one core block per
historical user message that has files.

### Why the block grows every turn (observed behaviour — fixed in v2.12.0)

1. The core re-hydrates the stored history each turn: every stored user
   message's `files` (images) become `image_url` parts, then base64
   (line 2396).
2. **Before v2.12.0** the filter's Step 2 walked **all** user messages,
   stripped those `image_url` parts and tagged them — so its own block
   was rebuilt each turn as the union of **all images of the
   conversation**. Since v2.12.0 the filter only tags images from the
   **last user message** (the current turn); re-hydrated history is
   stripped but not re-announced, so the filter's block only ever
   contains the current turn's images.
3. `add_file_context()` adds one block per stored user message that has
   files (stable, cached).

Net effect (v2.12.0): the filter's block carries only the current turn's
images; the core's per-message blocks carry each `+` upload once in its
own message. The `agent_loop_guard` pipe collapses the core blocks with
the filter's block and deduplicates by UUID (image tags only). The
v2.11.0 dedup collapses duplicates **within one request**; v2.12.0
removes the cross-turn re-announcement that made pasted images reappear
forever.

### Unverified claim: "the id changed between the raw and the resolved form"

A report from another agent described a "raw" upload-turn form
`<file type="file" url="{uuid}" content_type=... name=...>` (bare UUID, no
`/api/v1/files/` path) and that the same image later appeared with a
different `id`. This could not be reproduced from the code:
`format_file_tag()` (middleware.py:1570) always emits the stored
`file.id`, and no frontend code in `main` emits a bare-UUID `url` inside
`<attached_files>`. Treat the raw form as deployment/version specific and
**unverified**. The path-based dedup key below is robust either way for
resolved URLs; a bare-UUID `url` needs its own fallback (design note 4).

### Design for the pipe cleanup (implemented in `pipes/agent_loop_guard`)

The pipe is the last code that sees the assembled payload:
`generate_function_chat_completion()` calls `pipe(body, ...)` and its
return goes straight to the provider/gateway (functions.py:150-345). This
repo's `agent_loop_guard` pipe is already a manifold proxy for all models
(single-user deployment) and already mutates `messages` in-place before
forwarding — it covers every chat without extra deployment. The cleanup
step (v2.2.0+) does:

1. **Collapse** all `<attached_files>` blocks (filter's + core's, across
   all messages) into a single block on the last user message.
2. **Deduplicate `<file>` tags** with a canonical key, in order:
   - the URL **path** (`/api/v1/files/{id}/content`) — collapses the
     filter's absolute URL and the core's relative URL of the same file;
   - the bare `id` when the URL is absent;
   - a bare-UUID `url` (the unverified raw form) → treat the UUID as the
     file id;
   - external URLs → the full URL.
3. **Normalize to absolute URLs** (`webui.url`, same fallback as the
   filter) so downstream tools (ComfyUI URL-loading nodes) keep working.
4. **Fail-open**: any cleanup error must be logged and the payload
   forwarded unchanged — a cleanup bug must never break the gateway
   forwarding.

**Open decision — scope of the collapsed block.**

- **(a) Collapse + dedup** (documented default): the block still contains
  all images of the conversation each turn, deduplicated. Deterministic
  (preserves prefix-based context caching), matches what the model has
  seen so far.
- **(b) Only the current turn's images**: strip older attachments from the
  block. Fewer tokens per turn, but changes semantics (the model stops
  seeing older attachments), the block is no longer the stable union, and
  prefix-caching benefits shrink.

(a) is the default; (b) is a deliberate deviation that must be chosen
consciously.

## Remaining Option

1. **Rewrite the stored message after persisting (invasive)**: replace
   the `data:` URI in the saved user message with the new file reference
   and add the file to `message.files`, so later turns don't reload the
   base64 at all. Risk: mutating stored chat content; the image may stop
   rendering in the UI if the `files` entry is not added correctly.
   Would make the hash-dedup unnecessary but is riskier. (Not
   implemented.)

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | 0 | Execution order (lower = first). |
