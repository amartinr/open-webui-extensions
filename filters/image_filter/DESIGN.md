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
already on disk.

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

- **Re-persistence of pasted images across turns (mostly fixed)**: pasted
  images stay in the stored chat message as `data:` URIs, and the
  filter's Step 2 walks *all* user messages, so every turn still hashes
  the pasted content. Since v2.10.0 the content-hash dedup (below)
  reuses the first persisted file, so one paste yields one file on disk
  / one `files` row instead of N copies after N turns. Remaining caveat:
  the check-then-insert has no unique index behind it (TOCTOU), so two
  strictly concurrent requests could still write two files — worst case
  equals the pre-dedup behavior, it never reuses another user's file.
- **Growing duplicate references (partially fixed)**: for `+` uploads,
  historical messages are re-hydrated with `image_url` blocks each turn;
  since v2.11.0 the filter's own `<attached_files>` block no longer
  grows (tag dedup by id/url, see "Tag Deduplication" below). With
  native function calling, `add_file_context()` (which runs *after*
  filters) still prepends its own `<attached_files>` block from the
  stored message `files`, so the last user message can carry two blocks
  — the core's duplicates cannot be fixed from the filter.
- **Authenticated downloads**: `/api/v1/files/{id}/content` requires
  authentication (JWT or API key) and ownership checks. External tools
  fetching the absolute URL need valid credentials; the filter itself
  does not issue tokens.

## Content-Hash Deduplication (v2.10.0)

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
