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
2. Pasted images (Ctrl+V) arrive as `data:` URIs (base64) —
   `convert_url_images_to_base64()` already ran before the filter. They
   are persisted as permanent files with **content-hash dedup**: the URI
   is decoded once, `sha256` is computed over the raw bytes (the same
   digest Open WebUI stores in `files.meta["file_hash"]`), and an
   existing file owned by the user with the same hash is reused instead
   of writing a new one — then removed from the payload
3. A deduplicated `<attached_files>` block with `<file>` tags is
   injected so the model knows about the images of the **current turn**
   — the same file referenced from the current message and `body["files"]`
   is tagged only once
4. Re-hydrated history images (from earlier turns) are **stripped but
   not re-announced**: their base64 never reaches the LLM, but they are
   not re-tagged either — they were already announced in their own turn,
   the model retains conversational memory, and re-injecting an
   ever-staler image every turn wastes tokens and breaks prefix caching
5. Non-image files (PDFs, documents) pass through unchanged

## File References

The `<attached_files>` block contains one `<file>` tag per **unique**
image of the **current turn** — the same file referenced from the
current message and `body["files"]` is tagged only once (deduplicated
within each request):

```xml
<file type="image" id="abc123" url="https://your-owui-host.example/api/v1/files/abc123/content"/>
```

The URL is made **absolute** using the admin-configured **WebUI URL**
(Admin Settings → General → `webui.url`), falling back to the request's
base URL when unset. The `id` attribute keeps the builtin `view_file`
tool working unchanged, and the absolute URL can be passed to external
tools (e.g. ComfyUI nodes that load images by URL).

## Known Limitations

- **Pasted images are announced only once** (in the turn they are
  pasted). In later turns the re-hydrated history is stripped but not
  re-tagged, so the model no longer sees the file reference in context —
  it retains conversational memory of the image, but if a tool needs the
  file id/URL again the user re-attaches it. (v2.12.0)
- **Core `add_file_context()` blocks remain**: with native function
  calling, the core still prepends its own `<attached_files>` block per
  stored user message *after* the filter runs. Those blocks are
  per-message, stable and cached; the `agent_loop_guard` pipe collapses
  and deduplicates them with the filter's block. See `DESIGN.md` →
  "Attached-Files Accumulation — Verified Mechanism" for the pipeline
  order and the pipe-based cleanup.
