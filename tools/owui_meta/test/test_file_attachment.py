"""File attachment tests: the ``files`` event emitted by get_file_content.

Design (PLAN.md §7, 2026-08-03): get_file_content attaches the requested
file to the assistant message via the native Open WebUI ``files`` event so
the user can preview/download it in the UI, while the tool's text response
stays clean — a 100-character snippet for text files, metadata + note for
binaries.

The event item schema is pinned here because it must match what the
frontend renders (verified against main's ResponseMessage.svelte /
FileItem.svelte / FileItemModal.svelte):

- images  -> {"type": "image", "url": "/api/v1/files/{id}/content", ...}
             (Image.svelte needs a '/'-prefixed path for the inline preview)
- others  -> {"type": "file", "url": <id>, "meta": {"content_type": ...}, ...}
             (FileItem opens /files/{url}/content with the session cookie)
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, binary_response, json_response, make_tools

FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"


class FakeEmitter:
    """Records every event the tool emits (best-effort callable)."""

    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def files_event(self):
        """Return the ``files`` event, ignoring status/progress events."""
        return next(e for e in self.events if e.get("type") == "files")

    def embeds_event(self):
        """Return the ``embeds`` event, ignoring status/progress events."""
        return next(e for e in self.events if e.get("type") == "embeds")


def api_with_metadata(filename):
    """Handler serving file metadata (individual route) + content."""

    def handler(request):
        if request.url.path == f"/api/v1/files/{FILE_ID}":
            return json_response({"id": FILE_ID, "filename": filename})
        return binary_response(b"hello file content", "text/plain")

    return handler


async def test_text_file_emits_file_attachment():
    emitter = FakeEmitter()
    tools = make_tools(api_with_metadata("notes.txt"),
                       base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=emitter)

    assert len(emitter.events) >= 1
    event = emitter.files_event()
    assert event["type"] == "files"
    files = event["data"]["files"]
    assert len(files) == 1
    item = files[0]
    assert item["type"] == "file"
    assert item["url"] == FILE_ID  # FileItem opens /files/{url}/content
    assert item["name"] == "notes.txt"
    assert item["size"] == 18
    assert item["content_type"] == "text/plain"
    assert item["meta"] == {"content_type": "text/plain"}

    payload = json.loads(out)
    assert payload["content"] == "hello file content"  # snippet: 18 < 100 chars
    assert payload["truncated"] is False
    assert payload["filename"] == "notes.txt"


async def test_image_file_emits_embeds_html_preview():
    def handler(request):
        if request.url.path == f"/api/v1/files/{FILE_ID}":
            return json_response({"id": FILE_ID, "filename": "pic.png"})
        return binary_response(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png")

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=emitter)

    # images use the embeds mechanism (HTML inline, like a snippet) — NOT files
    event = emitter.embeds_event()
    assert event["type"] == "embeds"
    embeds = event["data"]["embeds"]
    assert len(embeds) == 1
    html = embeds[0]
    assert f"src=\"/api/v1/files/{FILE_ID}/content\"" in html
    assert "max-height:320px" in html
    assert "pic.png" in html
    # no files event for images (avoids double rendering)
    with pytest.raises(StopIteration):
        emitter.files_event()

    payload = json.loads(out)
    assert "content" not in payload
    assert "embedded in the conversation" in payload["note"]
    assert "Do NOT embed or display it again" in payload["note"]


async def test_generic_binary_emits_file_attachment():
    def handler(request):
        if request.url.path == f"/api/v1/files/{FILE_ID}":
            return json_response({"id": FILE_ID, "filename": "spec.pdf"})
        return binary_response(b"%PDF-1.4\n%fake", "application/pdf")

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=emitter)

    item = emitter.files_event()["data"]["files"][0]
    assert item["type"] == "file"
    assert item["url"] == FILE_ID
    assert item["name"] == "spec.pdf"
    assert item["content_type"] == "application/pdf"
    assert item["meta"] == {"content_type": "application/pdf"}

    payload = json.loads(out)
    assert "content" not in payload
    assert "Binary content" in payload["note"]


async def test_text_snippet_truncated_at_100_chars():
    long_text = "x" * 500

    def handler(request):
        if request.url.path == f"/api/v1/files/{FILE_ID}":
            return json_response({"id": FILE_ID, "filename": "long.txt"})
        return binary_response(long_text.encode(), "text/plain")

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=emitter)

    payload = json.loads(out)
    assert payload["content"] == "x" * 100
    assert payload["truncated"] is True
    assert payload["total_chars"] == 500


async def test_attachment_name_falls_back_to_file_id():
    def handler(request):
        # metadata route 404s -> name falls back to the file id
        if request.url.path == f"/api/v1/files/{FILE_ID}":
            return json_response({"unexpected": request.url.path}, status=404)
        return binary_response(b"some text", "text/plain")

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=emitter)

    item = emitter.files_event()["data"]["files"][0]
    assert item["name"] == FILE_ID


async def test_no_emitter_still_returns_result():
    tools = make_tools(api_with_metadata("notes.txt"),
                       base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest())  # no __event_emitter__
    payload = json.loads(out)
    assert payload["content"] == "hello file content"


async def test_emitter_failure_does_not_break_tool_call():
    class BrokenEmitter:
        async def __call__(self, event):
            raise RuntimeError("ui socket gone")

    tools = make_tools(api_with_metadata("notes.txt"),
                       base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest(), __event_emitter__=BrokenEmitter())
    payload = json.loads(out)
    assert payload["content"] == "hello file content"
