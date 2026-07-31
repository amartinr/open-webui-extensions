# Image to File Storage Filter

Prevents images from being injected into the LLM context. Strips them
from the payload and injects `<attached_files>` tags referencing the
image files stored on disk by Open WebUI.

## Installation

1. Copy `image_to_file_storage.py` to Open WebUI Functions (Admin →
   Functions → Add Function).
2. Assign the filter to models (enable it as global filter or per-model).

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `priority` | `0` | Execution order. Lower values run first. |

## How It Works

1. Images uploaded via `+` (already persisted on disk by Open WebUI's
   upload API) are removed from the LLM payload without touching the file
2. Pasted images (Ctrl+V, `data:` URIs) are persisted as permanent files
   via `get_image_url_from_base64()` — the same mechanism Open WebUI uses
   natively — then removed from the payload
3. An `<attached_files>` block with `<file>` tags is injected so the
   model knows about the images
4. Non-image files (PDFs, documents) pass through unchanged

## File References

The `<attached_files>` block contains one `<file>` tag per image:

```xml
<file type="image" id="abc123" url="https://your-owui-host.example/api/v1/files/abc123/content"/>
```

The URL is made **absolute** using the admin-configured **WebUI URL**
(Admin Settings → General → `webui.url`), falling back to the request's
base URL when unset. The `id` attribute keeps the builtin `view_file`
tool working unchanged, and the absolute URL can be passed to external
tools (e.g. ComfyUI nodes that load images by URL).
