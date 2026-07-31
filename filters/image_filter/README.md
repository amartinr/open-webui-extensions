# Image to File Storage Filter

Prevents images from being injected into the LLM context. Strips them
from the payload and injects `<attached_files>` tags. Optionally uploads
to ComfyUI so workflows can reference images by local filename.

## Installation

1. Copy `image_to_file_storage.py` to Open WebUI Functions (Admin →
   Functions → Add Function).
2. Assign the filter to models (enable it as global filter or per-model).

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `comfyui_base_url` | `None` | ComfyUI base URL (e.g. `http://akari:8188`). Uploads images so workflows can find them locally. |
| `comfyui_api_key` | `None` | ComfyUI Bearer token if required. |

## How It Works

1. Images uploaded via `+` or pasted are removed from the LLM payload
2. An `<attached_files>` block with `<file>` tags is injected so the
   model knows about the images
3. If `comfyui_base_url` is set, images are uploaded to ComfyUI and a
   second `<file type="comfyui">` tag with the local filename is added
4. Non-image files (PDFs, documents) pass through unchanged

## Downstream Integration

When ComfyUI valves are configured, the `<attached_files>` block will
contain two `<file>` tags per image:

```xml
<file type="image" id="abc123" url="/api/v1/files/abc123/content"/>
<file type="comfyui" url="a1b2c3d4.png" name="a1b2c3d4.png"/>
```

Workflows can reference the file by the ComfyUI-local filename (from
the `type="comfyui"` tag) without needing authentication.
