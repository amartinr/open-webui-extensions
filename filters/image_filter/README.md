# Image to File Storage Filter

Prevents images uploaded via the `+` button from being injected into the
LLM context. Images are already stored as permanent files by Open
WebUI's upload API — this filter simply removes them from the
LLM-bound payload so they neither undergo RAG embedding nor get
converted to base64.

## Installation

1. Copy `image_to_file_storage.py` to your Open WebUI functions
   directory or use the Admin Panel → Functions → Add Function.

2. Assign the filter to models or enable it as a global filter.

## How It Works

When you upload an image via the `+` button, Open WebUI:

1. **Uploads and stores** the image as a permanent file
2. **Returns a file reference** `{id, type: "image", url: "/api/v1/files/{id}/content"}`
3. **Puts it in `body["files"]`** → later moved to `metadata["files"]` → RAG pipeline tries to embed it (wasteful)
4. **For saved chats**, reconstructs messages and converts stored image
   references to `image_url` blocks → then to base64 (bloated context)

This filter intercepts at step 3 and 4:

- **Removes image entries** from `body["files"]` so RAG skips them
- **Removes `image_url` blocks** from message content so they never
  become base64

Non-image files (PDFs, documents) pass through unchanged.

## Valves (Admin Settings)

| Valve | Default | Description |
|-------|---------|-------------|
| `strip_files_metadata` | `True` | Remove image refs from `body["files"]` (top-level) to prevent RAG. |
| `strip_image_url_context` | `True` | Remove `image_url` blocks from message content to prevent base64. |

## Downstream Integration

Images remain stored and accessible at:
```
/api/v1/files/{id}/content
```

Use this URL from:
- **ComfyUI** (via "Load Image by URL" node)
- **Custom Pipes** that need to read attached images
- **Any tool** with access to Open WebUI

## How It Works

See [DESIGN.md](./DESIGN.md) for the full architecture.
