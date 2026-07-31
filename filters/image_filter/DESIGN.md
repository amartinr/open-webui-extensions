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
2. **Strips `image_url` blocks** from reconstructed message content — so
   they never become base64 in the LLM context.
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

For pasted images (Ctrl+V), the filter calls Open WebUI's
`get_image_url_from_base64()` to create a permanent file record, then
references it by URL. This is the same mechanism Open WebUI itself uses
for pasted images.

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

- **Mixed uploads in one message**: if a user uploads via `+` *and*
  pastes another image in the same message, the pasted image falls into
  the `has_uploaded` branch (global flag) and is stripped without being
  persisted or referenced. Only pure paths (`+` only, or paste only) are
  handled correctly today.
- **Re-persistence of pasted images across turns**: pasted images stay
  in the stored chat message as `data:` URIs (they never go through the
  upload API). The filter's Step 2 walks *all* user messages, so on
  every subsequent turn `load_messages_from_db()` reloads that content
  and `_persist_base64()` writes a brand-new file (new UUID, no hash
  dedup). One paste can yield N copies on disk / N `files` rows after
  N turns.
- **Growing duplicate references**: for `+` uploads, historical messages
  are re-hydrated with `image_url` blocks each turn, so the injected
  `<attached_files>` block re-tags past images and grows with the
  conversation. With native function calling, `add_file_context()` (which
  runs *after* filters) prepends its own `<attached_files>` block from
  the stored message `files`, so the last user message can carry two
  blocks referencing the same file.
- **Authenticated downloads**: `/api/v1/files/{id}/content` requires
  authentication (JWT or API key) and ownership checks. External tools
  fetching the absolute URL need valid credentials; the filter itself
  does not issue tokens.

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | 0 | Execution order (lower = first). |
