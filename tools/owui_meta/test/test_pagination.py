"""
Iteration 3 — Pagination, sorting and typed filters (DESIGN §8.6).

The API caps at 50 items/page and exposes ``total``. The tool iterates pages
transparently (bounded by MAX_PAGES) and applies sorting and filtering
CLIENT-SIDE over the fetched items, because the files/chats APIs do not
expose those criteria as query parameters. The agent only ever sees the
final filtered+sorted+truncated result.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, Recorder, json_response, make_tools

CHAT_ID = "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e"


def make_file(fid, name, ctype, size, created):
    return {
        "id": fid, "filename": name, "user_id": "u1",
        "meta": {"name": name, "content_type": ctype, "size": size, "data": {}},
        "created_at": created, "updated_at": created,
    }


def paginated_files_handler(total, items_per_page, factory):
    """Mimic /api/v1/files/: {items: [...], total: N} with page/pageSize."""

    def handler(request):
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * items_per_page
        page_items = [factory(i) for i in range(start, min(start + items_per_page, total))]
        return json_response({"items": page_items, "total": total})

    return handler


# ── Pagination ─────────────────────────────────────────────────────

async def test_pagination_iterates_pages_until_total():
    # 104 files, 50 per page → 3 requests (50+50+4); all returned.
    def factory(i):
        return make_file(f"f{i:03d}", f"file-{i}.txt", "text/plain", 100 + i, 1700000000 + i)

    recorder = Recorder(paginated_files_handler(104, 50, factory))
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    tools.valves.max_response_chars = 100_000
    out = await tools.get_my_files(limit=200, __request__=FakeRequest())
    payload = json.loads(out)
    assert len(recorder.requests) == 3
    assert payload["count"] == 104
    assert payload["total"] == 104


async def test_pagination_stops_on_short_page():
    # 12 items with page_size 20 → single request, then short page.
    def factory(i):
        return make_file(f"f{i}", f"file-{i}.txt", "text/plain", i, 1700000000 + i)

    recorder = Recorder(paginated_files_handler(12, 20, factory))
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(limit=50, __request__=FakeRequest())
    assert len(recorder.requests) == 1
    assert json.loads(out)["count"] == 12


async def test_pagination_respects_max_pages_cap():
    # Huge dataset: 5000 items, 50/page → would need 100 pages; capped at MAX_PAGES.
    def factory(i):
        return make_file(f"f{i}", f"file-{i}.txt", "text/plain", i, 1700000000 + i)

    recorder = Recorder(paginated_files_handler(5000, 50, factory))
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    tools.valves.max_response_chars = 100_000
    out = await tools.get_my_files(limit=50, __request__=FakeRequest())
    assert len(recorder.requests) == 5  # MAX_PAGES
    payload = json.loads(out)
    assert payload["count"] == 50
    assert payload["total"] == 5000


# ── Sorting (files) ─────────────────────────────────────────────────

async def test_files_sort_by_size_asc():
    items = [
        make_file("a", "big.bin", "application/octet-stream", 5000, 1),
        make_file("b", "small.txt", "text/plain", 10, 2),
        make_file("c", "mid.png", "image/png", 500, 3),
    ]

    def handler(request):
        return json_response({"items": items, "total": 3})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(limit=10, sort_by="size", sort_order="asc", __request__=FakeRequest())
    payload = json.loads(out)
    assert [f["filename"] for f in payload["files"]] == ["small.txt", "mid.png", "big.bin"]


async def test_files_sort_by_filename_desc():
    items = [
        make_file("a", "apple.txt", "text/plain", 1, 1),
        make_file("b", "banana.txt", "text/plain", 1, 2),
        make_file("c", "cherry.txt", "text/plain", 1, 3),
    ]

    def handler(request):
        return json_response({"items": items, "total": 3})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(limit=10, sort_by="filename", sort_order="desc", __request__=FakeRequest())
    assert [f["filename"] for f in json.loads(out)["files"]] == ["cherry.txt", "banana.txt", "apple.txt"]


async def test_chats_sort_by_created_at_asc():
    def handler(request):
        return json_response([
            {"id": "c1", "title": "old", "created_at": 100, "updated_at": 400},
            {"id": "c2", "title": "new", "created_at": 300, "updated_at": 500},
            {"id": "c3", "title": "mid", "created_at": 200, "updated_at": 300},
        ])

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_chats(limit=10, sort_by="created_at", sort_order="asc", __request__=FakeRequest())
    assert [c["id"] for c in json.loads(out)["chats"]] == ["c1", "c3", "c2"]


# ── Filtering (files) ───────────────────────────────────────────────

async def test_files_filter_by_content_type_wildcard():
    items = [
        make_file("a", "img.png", "image/png", 10, 1),
        make_file("b", "img.jpg", "image/jpeg", 10, 2),
        make_file("c", "doc.pdf", "application/pdf", 10, 3),
    ]

    def handler(request):
        return json_response({"items": items, "total": 3})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(
        limit=10, content_type="image/*", sort_order="asc", __request__=FakeRequest()
    )
    names = [f["filename"] for f in json.loads(out)["files"]]
    assert names == ["img.png", "img.jpg"]
    assert "doc.pdf" not in names


async def test_files_filter_by_size_range_and_name():
    items = [
        make_file("a", "report-q1.pdf", "application/pdf", 5000, 1),
        make_file("b", "report-q2.pdf", "application/pdf", 7000, 2),
        make_file("c", "image.png", "image/png", 9000, 3),
    ]

    def handler(request):
        return json_response({"items": items, "total": 3})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(
        limit=10, min_size=6000, max_size=8000, filename="report", __request__=FakeRequest()
    )
    names = [f["filename"] for f in json.loads(out)["files"]]
    assert names == ["report-q2.pdf"]  # q1 (5000) < min; image.png not "report" and > max


async def test_files_filter_matched_count_reported():
    items = [
        make_file("a", "img.png", "image/png", 10, 1),
        make_file("b", "doc.pdf", "application/pdf", 10, 2),
    ]

    def handler(request):
        return json_response({"items": items, "total": 2})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_files(limit=10, content_type="image/png", __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["matched"] == 1


async def test_invalid_sort_by_falls_back_to_default():
    items = [
        make_file("a", "b.txt", "text/plain", 1, 100),
        make_file("b", "a.txt", "text/plain", 1, 200),
    ]

    def handler(request):
        return json_response({"items": items, "total": 2})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    # invalid sort_by → defaults to created_at desc → b (created 200) first
    out = await tools.get_my_files(limit=10, sort_by="bogus", __request__=FakeRequest())
    assert json.loads(out)["files"][0]["filename"] == "a.txt"


async def test_markdown_render_shows_matched_and_top():
    items = [
        make_file("a", "img1.png", "image/png", 10, 1),
        make_file("b", "img2.png", "image/png", 10, 2),
        make_file("c", "img3.png", "image/png", 10, 3),
    ]

    def handler(request):
        return json_response({"items": items, "total": 3})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_files(limit=2, sort_by="created_at", sort_order="asc", __request__=FakeRequest())
    assert "**Files: 3** (showing top 2)" in out
