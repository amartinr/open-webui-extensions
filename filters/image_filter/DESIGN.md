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
runs before both `chat_completion_files_handler` and
`convert_url_images_to_base64()`. It has two independent actions:

### 1. Strip image file refs from `body["files"]`

Removes entries with `type: "image"` or `content_type` starting with
`image/` from `body["files"]` (top-level). At the time the filter
runs, file references are still at the top-level key;
`process_chat_payload` moves them into `metadata["files"]` *after*
the filter pipeline completes. This prevents the RAG pipeline from
trying to embed images.

Non-image files (documents, PDFs, etc.) pass through unchanged.

### 2. Strip `image_url` blocks from user message content

When messages are reconstructed from DB history, image files are
converted to `image_url` content blocks (mirroring frontend logic).
This filter removes those blocks, preventing:
- `convert_url_images_to_base64()` from converting them to base64
- The LLM from receiving unusable image data

Since the images are already stored as permanent files, no additional
storage is needed.

### 3. Inject `<attached_files>` block

After stripping images, the filter injects an `<attached_files>` block
into the last user message using the **exact same format** as Open
WebUI's own `add_file_context()`. This gives the model a deterministic,
stable reference to the attached images:

```xml
<attached_files>
<file type="image" id="abc123" url="/api/v1/files/abc123/content" content_type="image/png" name="photo.png"/>
</attached_files>

[original user text...]
```

This is deterministic (same input → same output) and does not break
prefix-based context caching.

### Base64 persistence fallback

Images arrive in two shapes depending on how the user attached them:

| Source | `body["files"]` | Message content |
|--------|:---:|:---:|
| Button +  | File ref with ID | `image_url` with base64 (added by `convert_url_images_to_base64`) |
| Pasted (Ctrl+V) | `None` | `image_url` with base64 (client-side `data:` URI) |

Since the filter runs *after* `convert_url_images_to_base64`, both cases look
the same at the content level. The filter checks `body["files"]` first: if
the image was uploaded and already has a file ID, it uses that ID directly
for the `<file>` tag and skips the base64 persistence.

When `body["files"]` is empty (pasted image), the filter calls Open WebUI's
`get_image_url_from_base64()` to persist the image and obtain a file URL.

Since filter inlets receive `__user__` as a plain dict, the filter converts
it to a `UserModel` instance before passing it to the internal upload API,
which expects attribute access (`user.email`).

## Execution Order

```
User message (with image uploaded via "+")
    │
    ▼
 convert_url_images_to_base64()   ◄── converts file URLs → base64 in
    │                                   message content (irrelevant —
    │                                   body["files"] is untouched here)
    ▼
 Pipeline Inlet Filters ──►  Image to File Storage (priority 0)
    │                              │
    │                              ├── body["files"] ──► remove image entries
    │                              └── message content ──► strip image_url blocks
    │                                                   (discards base64 added above)
    ▼
 files = form_data.pop('files', None)   ◄── no images left
    │
    ▼
 metadata["files"] = files             ◄── no image refs
    │
    ▼
 chat_completion_files_handler()        ◄── RAG only on non-image files
    │
    ▼
 LLM receives only text content
```

## Key Design Decisions

### 1. Three actions, two valves

| Action | Valve | Default |
|--------|-------|---------|
| Strip images from `body["files"]` | `strip_files_metadata` | `True` |
| Strip `image_url` from message content | `strip_image_url_context` | `True` |
| Inject `<attached_files>` block | Always active when any image is stripped | — |

The `<attached_files>` injection happens automatically whenever images
are removed, because without it the model would have no way to know
that images were attached.

Each action can be toggled independently via Valves:

| Valve | Default | Purpose |
|-------|---------|---------|
| `strip_files_metadata` | `True` | Prevent RAG on images |
| `strip_image_url_context` | `True` | Prevent base64 injection in context |

### 2. Matches add_file_context() format

The injected `<file>` tags use the exact same format as Open WebUI's
`add_file_context()` in `middleware.py`. This means:
- The model sees the same XML structure regardless of whether tags come
  from this filter or from the builtin RAG pipeline.
- The block is deterministic: same files → same text → same cache key.
  Prefix-based context caching is not invalidated between turns.
- When native FC is enabled, `add_file_context()` also runs later and
  injects tags for stored messages — the redundancy is harmless and
  ensures coverage regardless of FC mode.

### 3. No storage calls

Unlike earlier versions, this filter never calls
`get_image_url_from_base64()`. Images uploaded via the `+` button are
**already stored** by the upload API. The filter just removes
references from the LLM-bound payload.

### 4. Filter runs at priority 0

We set `priority = 0` so this filter executes before
`chat_completion_files_handler`.  It also runs *after*
`convert_url_images_to_base64`, which means any base64 generated for
reconstructed messages is discarded by the filter — a small CPU cost
(~15-20ms per 2MB image) that is far outweighed by the ~160k tokens
saved in the LLM context.

### 5. Non-image files pass through

Only entries matching `image/` content_type or `"image"` type are
removed. Documents, PDFs, spreadsheets, and other text-based files
continue to be processed normally through the RAG pipeline.

### 6. Graceful message content handling

After stripping `image_url` blocks, if the content list becomes empty,
an empty text block `{"type": "text", "text": ""}` is inserted to keep
the message valid (prevents 400 errors from strict providers).

### 7. Downstream integration

Images remain accessible at their stored URL:
```
/api/v1/files/{id}/content
```

This can be used by:
- **ComfyUI workflows** (via "Load Image by URL" node)
- **Custom Pipes** that need to read attached images
- **Any external tool** with access to Open WebUI

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | 0 | Execution order (lower = first). |
| `base_url` | `None` | Public base URL for file references (e.g. `http://open-webui:8080`). When set, file URLs in the `<attached_files>` block use this instead of the auto-detected request base. Leave empty to auto-detect. |
