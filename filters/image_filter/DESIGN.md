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
   images were attached.
4. **Uploads to ComfyUI** (optional) — when `comfyui_base_url` is
   configured, the filter uploads the image to ComfyUI's `/upload/image`
   endpoint and adds an extra `<file>` tag with the ComfyUI-local filename
   that workflows can reference without authentication.

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
    │  ├── message content ──► strip image_url blocks
    │  └── upload to ComfyUI (if configured)
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

### 1. ComfyUI bypasses auth limitation

External tools (ComfyUI) cannot fetch Open WebUI file URLs because the
API requires authentication (JWT or API key). By uploading the image
directly to ComfyUI's `input/` folder, the workflow can reference it by
local filename — no auth needed.

### 2. Matches add_file_context() format

The injected `<file>` tags match Open WebUI's `add_file_context()`
format exactly. The model sees the same XML structure regardless of
source. The block is deterministic (same input → same output), preserving
prefix-based context caching.

### 3. No duplicate persistence

For images uploaded via the `+` button, the filter detects the existing
file ID in `body["files"]` and does not persist again. It only reads the
file from disk for the optional ComfyUI upload.

For pasted images (Ctrl+V), the filter calls Open WebUI's
`get_image_url_from_base64()` to create a permanent file record, then
optionally uploads to ComfyUI.

### 4. Non-image files pass through unchanged

### 5. Graceful message content handling

After stripping, if a message has no content left, an empty text block
is inserted to prevent 400 errors from strict providers.

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | 0 | Execution order (lower = first). |
| `comfyui_base_url` | `None` | ComfyUI base URL (e.g. `http://akari:8188`). Uploads images to ComfyUI so workflows can reference them locally. |
| `comfyui_api_key` | `None` | ComfyUI Bearer token if required. |
