"""Iteration 9 — task 9.1: `tag:` scope-limiter semantics + documentation.

The improvement brief claimed `tag:` is a standalone filter that relaxes the
free text in ``search_chats``. Verified false on this backend (v0.10.2
``models/chats.py::get_chats_by_user_id_and_search_text`` + live probes): the
query strips every UI prefix and ANDs the text search with the tag filter
(``EXISTS(json_each(meta.tags) = tag)``); multiple ``tag:`` prefixes are ANDed;
``tag:none`` is ``NOT EXISTS``.

What this suite pins:

- mock: ``search_chats`` passes the text (incl. ``tag:`` prefixes) through to
  the backend untouched — the scope limiting is server-side, not re-implemented;
- mock: ``tag:none`` passthrough;
- docstrings: the orphan-tag cleanup side effect and the archived-chat
  asymmetry are documented (no behavioral guard — blocking would break the
  backend's intended lazy cleanup);
- live (env-gated): the decisive AND case (absent text + tag → 0 results, a
  standalone filter would return the tag's chats) and consistency between
  ``search_chats("tag:X")`` and ``get_chats(tag="X")``.
"""

import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

import owui_meta
from helpers import FakeRequest, Recorder, binary_response, json_response, make_tools

LIVE_URL = os.getenv("OWUI_META_LIVE_URL", "").strip()
LIVE_TOKEN = os.getenv("OWUI_META_LIVE_TOKEN", "").strip()

live = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN),
    reason="set OWUI_META_LIVE_URL and OWUI_META_LIVE_TOKEN to run the live cases",
)


def live_tools(output_format: str = "json") -> owui_meta.Tools:
    tools = owui_meta.Tools()
    tools.valves.output_format = output_format
    tools._base_url_override = LIVE_URL
    owui_meta.Config = None
    return tools


def live_request() -> FakeRequest:
    return FakeRequest(token=LIVE_TOKEN)


# ── mock: passthrough + surfacing ────────────────────────────────────

async def test_search_chats_passes_tag_prefix_text_through_unchanged():
    # The tool must NOT re-implement the scope limiting: the backend ANDs
    # text + tag server-side, so the text travels unchanged.
    def handler(request):
        assert request.url.params["text"] == "foo tag:budget"
        return json_response([{"id": "c1", "title": "Budget", "snippet": "match foo"}])

    recorder = Recorder(handler)
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("foo tag:budget", FakeRequest())
    data = json.loads(out)
    assert data["query"] == "foo tag:budget"
    assert [c["id"] for c in data["chats"]] == ["c1"]
    assert data["chats"][0]["snippet"] == "match foo"


async def test_search_chats_tag_none_passthrough():
    # 9.8: a call whose tokens are ONLY UI prefixes (here tag:none) searches
    # nothing — it must NOT hit the network and must return a guiding hint
    # (NOT a ToolError: that would emit chat:message:error and, in v0.10.2,
    # mark the message errored and BLOCK the next send, breaking the chat).
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return json_response([], status=200)

    tools = make_tools(handler, base_url="http://webui.example.test")
    out = await tools.search_chats("tag:none", FakeRequest())
    assert seen == [], "pure-prefix call must not hit the network"
    assert out.startswith("Error:") is False
    assert "get_chats" in out and "get_tags" in out


async def test_search_chats_pure_prefix_returns_guide_for_each_prefix():
    # 9.8 acceptance: every non-folder UI prefix alone → guiding hint, never
    # a listing and never a ToolError. (folder: is excluded — a valid
    # folder: is a legitimate scope after the 9.7 resolution.)
    expected_hints = {
        "pinned:true": "get_chats(scope='pinned')",
        "tag:meta": "get_chats(tag='meta')",
        "archived:true": "get_chats(scope='archived')",
        "shared:true": "get_chats(scope='shared')",
        "tag:none": "get_tags",
    }
    for term, needle in expected_hints.items():
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return json_response([], status=200)

        tools = make_tools(handler, base_url="http://webui.example.test")
        out = await tools.search_chats(term, FakeRequest())
        assert seen == [], f"pure-prefix {term!r} must not hit the network"
        assert out.startswith("Error:") is False, term
        assert needle in out, f"{term!r}: missing {needle!r} in {out!r}"


async def test_search_chats_text_plus_prefix_still_works():
    # 9.8: a real text term + prefix keeps working as AND (server-side).
    def handler(request):
        assert request.url.params["text"] == "ventilador pinned:true"
        return json_response([{"id": "c1", "title": "Ventilador"}])

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("ventilador pinned:true", FakeRequest())
    data = json.loads(out)
    assert data["query"] == "ventilador pinned:true"
    assert [c["id"] for c in data["chats"]] == ["c1"]


# ── 9.7: folder: name resolution ─────────────────────────────────────

_FOLDERS = [
    {"id": "f1", "name": "Open WebUI meta", "parent_id": None},
    {"id": "f2", "name": "IA generativa", "parent_id": None},
    {"id": "f3", "name": "Single", "parent_id": None},
]


def _folder_handler(text_assert=None, chats=None):
    """Handler that serves /api/v1/folders/ and asserts the search text."""
    def handler(request):
        if request.url.path == "/api/v1/folders/":
            return json_response(_FOLDERS)
        if request.url.path == "/api/v1/chats/search":
            if text_assert is not None:
                assert request.url.params["text"] == text_assert, request.url.params
            return json_response(chats or [{"id": "c1", "title": "In folder"}])
        return json_response({"unexpected": request.url.path}, status=404)
    return handler


async def test_search_chats_folder_multiword_resolves_and_strips_leak():
    # 9.7: "folder:Open WebUI meta" must resolve to the canonical single
    # token "folder:Open_WebUI_meta" — and the leaked words ("WebUI meta")
    # must NOT remain in the free text.
    tools = make_tools(_folder_handler(text_assert="folder:Open_WebUI_meta"),
                       base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("folder:Open WebUI meta", FakeRequest())
    data = json.loads(out)
    assert data["query"] == "folder:Open_WebUI_meta"
    assert [c["id"] for c in data["chats"]] == ["c1"]


async def test_search_chats_folder_underscore_single_token():
    # 9.7: the underscore-joined single token (which the backend already
    # normalizes) keeps working and is rewritten to the real name form.
    tools = make_tools(_folder_handler(text_assert="folder:Open_WebUI_meta"),
                       base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("folder:open_webui_meta", FakeRequest())
    data = json.loads(out)
    assert data["query"] == "folder:Open_WebUI_meta"


async def test_search_chats_folder_single_word():
    # 9.7: a single-word folder name matches directly.
    tools = make_tools(_folder_handler(text_assert="folder:Single"),
                       base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("folder:Single", FakeRequest())
    assert json.loads(out)["query"] == "folder:Single"


async def test_search_chats_folder_unknown_clean_error_lists_names():
    # 9.7: unknown folder → clean error listing the valid names, and no
    # request to the search endpoint (no silent no-filter).
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/folders/":
            return json_response(_FOLDERS)
        return json_response([], status=200)

    tools = make_tools(handler, base_url="http://webui.example.test")
    out = await tools.search_chats("folder:NoExiste", FakeRequest())
    assert seen == ["/api/v1/folders/"], "unknown folder must not hit search"
    assert out.startswith("Error:")
    assert "NoExiste" in out and "Open WebUI meta" in out and "Single" in out


async def test_search_chats_folder_plus_text_is_and():
    # 9.7: text + folder stays AND; the folder phrase is consumed (no leak).
    tools = make_tools(
        _folder_handler(text_assert="ventilador folder:Open_WebUI_meta"),
        base_url="http://webui.example.test", output_format="json",
    )
    out = await tools.search_chats("ventilador folder:Open WebUI meta", FakeRequest())
    data = json.loads(out)
    assert data["query"] == "ventilador folder:Open_WebUI_meta"


async def test_search_chats_folder_fetch_failure_is_clean_error():
    # 9.7: if the folders route fails (e.g. folders disabled → 403), the
    # call errors cleanly instead of silently ignoring the folder filter.
    def handler(request):
        if request.url.path == "/api/v1/folders/":
            return json_response({"detail": "forbidden"}, status=403)
        return json_response([], status=200)

    tools = make_tools(handler, base_url="http://webui.example.test")
    out = await tools.search_chats("folder:Open WebUI meta", FakeRequest())
    assert out.startswith("Error:")
    assert "Forbidden" in out


async def test_get_chats_tag_uses_post_tags_route():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/chats/tags"
        body = json.loads(request.content)
        assert body == {"name": "budget", "skip": 0, "limit": 50}
        return json_response([{"id": "c1", "title": "Budget"}])

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_chats(tag="budget", limit=10, __request__=FakeRequest())
    data = json.loads(out)
    assert [c["id"] for c in data["chats"]] == ["c1"]


def test_tag_semantics_documented_in_docstrings():
    # No behavioral guard is added (blocking would break the backend's
    # intended orphan-tag cleanup) — the semantics live in the docstrings.
    def flat(doc: str) -> str:
        return " ".join(doc.split())

    search_doc = flat(owui_meta.Tools.search_chats.__doc__ or "")
    for needle in ("scope limiters", "orphan-tag", "archived chats",
                   "real search term", "get_folders", "resolved client-side",
                   "unknown folder", "get_tags"):
        assert needle in search_doc, f"search_chats docstring missing {needle!r}"
    list_doc = flat(owui_meta.Tools.get_chats.__doc__ or "")
    for needle in ("orphan-tag", "archived chats"):
        assert needle in list_doc, f"get_chats docstring missing {needle!r}"


# ── live: decisive AND + consistency (env-gated) ─────────────────────

@live
async def test_live_search_tag_is_scope_limiter_and():
    tools = live_tools()
    tags = (json.loads(await tools.get_tags(live_request())) or {}).get("tags") or []
    if not tags:
        pytest.skip("instance has no tags")
    tag = tags[0]["name"]
    # Decisive case from the brief correction: a text term ABSENT from the
    # corpus + tag → 0 results. A standalone tag filter (what the brief
    # claimed) would return the tag's chats.
    out = await tools.search_chats(f"zzzz_owui_nonexistent_xyz tag:{tag}", live_request())
    data = json.loads(out)
    assert len(data.get("chats", [])) == 0


@live
async def test_live_search_tag_consistent_with_get_chats_tag():
    tools = live_tools()
    tags = (json.loads(await tools.get_tags(live_request())) or {}).get("tags") or []
    if not tags:
        pytest.skip("instance has no tags")
    tag = tags[0]["name"]
    by_list = json.loads(await tools.get_chats(tag=tag, limit=50, __request__=live_request()))
    ids_list = {c["id"] for c in by_list.get("chats", [])}
    if not ids_list:
        pytest.skip("tag has no chats")
    # 9.8: lone "tag:X" is an error, so search with a real term — the first
    # tag chat's title — and verify text+tag is AND (a subset of the tag).
    title = by_list["chats"][0]["title"]
    by_search = json.loads(await tools.search_chats(f"{title} tag:{tag}", live_request()))
    ids_search = {c["id"] for c in by_search.get("chats", [])}
    assert ids_search <= ids_list, f"search {ids_search} not subset of tag {ids_list}"
    assert by_list["chats"][0]["id"] in ids_search


# ── 9.3: chat stats — recomputed length averages (backend bug) ────────

def _chat_response_payload(specs):
    """Build a ChatResponse body for message specs [(role, text|None), ...].

    ``None`` text = a textless step (e.g. a ``reasoning`` output), matching
    the v0.10.2 shape: assistant text lives in ``output[].content[].text``
    and plain ``content`` is empty.
    """
    messages = {}
    prev = None
    current = None
    for i, (role, text) in enumerate(specs):
        mid = f"m{i}"
        if text is None:
            msg = {"id": mid, "role": role, "parentId": prev, "content": "",
                   "output": [{"type": "reasoning"}]}
        else:
            msg = {"id": mid, "role": role, "parentId": prev, "content": "",
                   "output": [{"type": "message",
                               "content": [{"type": "output_text", "text": text}]}]}
        messages[mid] = msg
        prev = mid
        current = mid
    return {"id": "chat1", "title": "T",
            "chat": {"history": {"messages": messages, "currentId": current}}}


def _stats_handler(chat_payload, stats_item):
    def handler(request):
        if request.url.path == "/api/v1/chats/stats/usage":
            return json_response({"items": [stats_item], "total": 1})
        if request.url.path == "/api/v1/chats/chat1":
            return json_response(chat_payload)
        raise AssertionError(f"unexpected path {request.url.path}")
    return handler


async def test_chat_stats_recomputes_length_averages():
    user_texts = ["short", "a somewhat longer user message"]
    asst_texts = ["assistant reply one", "assistant reply two is a bit longer"]
    specs = [("user", t) for t in user_texts] + [("assistant", t) for t in asst_texts]
    stats_item = {
        "id": "chat1", "message_count": 4, "models": {}, "tags": [],
        "history_message_count": 4, "history_user_message_count": 2,
        "history_assistant_message_count": 2, "average_response_time": 1.5,
        "average_user_message_content_length": 0.0,   # the v0.10.2 bug
        "average_assistant_message_content_length": 0.0,
        "last_message_at": 1000, "created_at": 100, "updated_at": 200,
    }
    tools = make_tools(_stats_handler(_chat_response_payload(specs), stats_item),
                       base_url="http://webui.example.test", output_format="json")
    data = json.loads(await tools.get_chat_stats("chat1", FakeRequest()))
    exp_user = sum(len(t) for t in user_texts) / len(user_texts)
    exp_asst = sum(len(t) for t in asst_texts) / len(asst_texts)
    assert data["average_user_message_content_length"] == exp_user
    assert data["average_assistant_message_content_length"] == exp_asst
    # raw backend values preserved under …_backend
    assert data["average_user_message_content_length_backend"] == 0.0
    assert data["average_assistant_message_content_length_backend"] == 0.0


async def test_chat_stats_no_text_yields_none_not_zero():
    # Assistant with only reasoning output → recomputed average is None
    # (never 0.0), so it cannot masquerade as a real measurement.
    stats_item = {
        "id": "chat1", "message_count": 2, "models": {}, "tags": [],
        "average_user_message_content_length": 1.0,
        "average_assistant_message_content_length": 0.0,
        "last_message_at": 1, "created_at": 0, "updated_at": 1,
    }
    tools = make_tools(
        _stats_handler(_chat_response_payload([("user", "hi"), ("assistant", None)]), stats_item),
        base_url="http://webui.example.test", output_format="json",
    )
    data = json.loads(await tools.get_chat_stats("chat1", FakeRequest()))
    assert data["average_user_message_content_length"] == 2.0
    assert data["average_assistant_message_content_length"] is None
    assert data["average_assistant_message_content_length_backend"] == 0.0


async def test_chat_stats_markdown_notes_the_correction():
    stats_item = {
        "id": "chat1", "message_count": 2, "models": {}, "tags": [],
        "average_user_message_content_length": 0.0,
        "average_assistant_message_content_length": 0.0,
        "last_message_at": 1, "created_at": 0, "updated_at": 1,
    }
    specs = [("user", "hello there"), ("assistant", "hi! how can I help today?")]
    tools = make_tools(_stats_handler(_chat_response_payload(specs), stats_item),
                       base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_chat_stats("chat1", FakeRequest())
    assert "Avg assistant msg length (chars): 25.0" in out
    assert "corrected above" in out
    assert "0.0 = v0.10.2 bug" in out


async def test_chat_stats_keeps_backend_values_when_chat_fetch_fails():
    def handler(request):
        if request.url.path == "/api/v1/chats/stats/usage":
            return json_response({"items": [{
                "id": "chat1", "message_count": 1, "models": {}, "tags": [],
                "average_user_message_content_length": 3.0,
                "average_assistant_message_content_length": 0.0,
                "last_message_at": 1, "created_at": 0, "updated_at": 1,
            }], "total": 1})
        if request.url.path == "/api/v1/chats/chat1":
            return json_response({"error": "boom"}, status=500)
        raise AssertionError(f"unexpected path {request.url.path}")
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    data = json.loads(await tools.get_chat_stats("chat1", FakeRequest()))
    # chat fetch failed → enrichment degrades to backend values, no error
    assert data["average_user_message_content_length"] == 3.0
    assert data["average_assistant_message_content_length"] == 0.0


# ── 9.2: image header metadata (Pillow — bundled with Open WebUI) ─────

pytestmark_92 = pytest.mark.skipif(
    owui_meta.Image is None, reason="Pillow not available"
)


def _make_image(fmt: str, size=(64, 48), mode="RGB"):
    """Generate a real image of the given format via Pillow."""
    import io as _io
    buf = _io.BytesIO()
    img = owui_meta.Image.new(mode, size)
    img.save(buf, format=fmt)
    return buf.getvalue()


@pytestmark_92
def test_image_header_png_rgb():
    info = owui_meta.Tools()._image_header_info(_make_image("PNG", (1024, 768), "RGB"))
    assert info == {"width": 1024, "height": 768, "color_mode": "RGB", "bit_depth": 8}


@pytestmark_92
def test_image_header_png_rgba():
    info = owui_meta.Tools()._image_header_info(_make_image("PNG", (64, 48), "RGBA"))
    assert info["color_mode"] == "RGBA"
    assert info["bit_depth"] == 8


@pytestmark_92
def test_image_header_jpeg():
    info = owui_meta.Tools()._image_header_info(_make_image("JPEG", (640, 480), "RGB"))
    assert info["width"] == 640 and info["height"] == 480
    assert info["color_mode"] == "RGB"


@pytestmark_92
def test_image_header_gif():
    info = owui_meta.Tools()._image_header_info(_make_image("GIF", (320, 240), "P"))
    assert info["width"] == 320 and info["height"] == 240


@pytestmark_92
def test_image_header_webp():
    try:
        body = _make_image("WEBP", (10, 20), "RGBA")
    except Exception:
        pytest.skip("libwebp not available")
    info = owui_meta.Tools()._image_header_info(body)
    assert info["width"] == 10 and info["height"] == 20
    assert info["color_mode"] == "RGBA"


@pytestmark_92
def test_image_header_bmp():
    info = owui_meta.Tools()._image_header_info(_make_image("BMP", (100, 50), "RGB"))
    assert info["width"] == 100 and info["height"] == 50
    assert info["color_mode"] == "RGB"
    assert info["bit_depth"] == 8  # bits per channel


@pytestmark_92
def test_image_header_tiff():
    info = owui_meta.Tools()._image_header_info(_make_image("TIFF", (800, 600), "RGB"))
    assert info["width"] == 800 and info["height"] == 600


def test_image_header_garbage_and_truncated_never_error():
    tools = owui_meta.Tools()
    assert tools._image_header_info(b"") == {}
    assert tools._image_header_info(b"\x00\x01\x02garbage") == {}
    # valid PNG signature but truncated payload → Pillow raises, we degrade
    assert tools._image_header_info(b"\x89PNG\r\n\x1a\n\x00\x00\x00") == {}


def _file_metadata(filename="photo.png", content_type="image/png", size=10):
    return {"id": "f1", "filename": filename,
            "meta": {"content_type": content_type, "size": size}}


def _file_content_handler(body, content_type="image/png", filename="photo.png"):
    def handler(request):
        if request.url.path == "/api/v1/files/f1":
            return json_response(_file_metadata(filename, content_type, len(body)))
        if request.url.path == "/api/v1/files/f1/content":
            return binary_response(body, content_type)
        raise AssertionError(f"unexpected path {request.url.path}")
    return handler


@pytestmark_92
async def test_get_file_content_image_includes_header_metadata():
    body = _make_image("PNG", (1024, 768), "RGBA") + b"trailing"
    tools = make_tools(_file_content_handler(body), base_url="http://webui.example.test",
                       output_format="json")
    data = json.loads(await tools.get_file_content("f1", FakeRequest()))
    assert (data["width"], data["height"]) == (1024, 768)
    assert data["color_mode"] == "RGBA"
    assert data["bit_depth"] == 8
    assert data["size"] == len(body)
    # never pixel data in the output
    assert "trailing" not in str(data)


@pytestmark_92
async def test_get_file_content_image_markdown_line():
    tools = make_tools(_file_content_handler(_make_image("PNG", (640, 480), "RGB")),
                       base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_file_content("f1", FakeRequest())
    assert "- Image: 640\u00d7480 px, RGB (8-bit)" in out


@pytestmark_92
async def test_get_file_content_binary_image_without_pillow_degrades():
    # Simulate Pillow absence: the enrichment is skipped, no error, no fields.
    tools = make_tools(_file_content_handler(_make_image("PNG", (4, 4))),
                       base_url="http://webui.example.test", output_format="json")
    original = owui_meta.Image
    owui_meta.Image = None
    try:
        data = json.loads(await tools.get_file_content("f1", FakeRequest()))
    finally:
        owui_meta.Image = original
    for key in ("width", "height", "color_mode"):
        assert key not in data


async def test_get_file_content_binary_non_image_unaffected():
    body = b"%PDF-1.4 fake pdf content"
    tools = make_tools(_file_content_handler(body, content_type="application/pdf",
                                             filename="doc.pdf"),
                       base_url="http://webui.example.test", output_format="json")
    data = json.loads(await tools.get_file_content("f1", FakeRequest()))
    assert data["content_type"] == "application/pdf"
    for key in ("width", "height", "color_mode", "bit_depth"):
        assert key not in data


async def test_get_file_content_binary_non_image_unaffected():
    body = b"%PDF-1.4 fake pdf content"
    tools = make_tools(_file_content_handler(body, content_type="application/pdf",
                                             filename="doc.pdf"),
                       base_url="http://webui.example.test", output_format="json")
    data = json.loads(await tools.get_file_content("f1", FakeRequest()))
    assert data["content_type"] == "application/pdf"
    for key in ("real_format", "width", "height"):
        assert key not in data
