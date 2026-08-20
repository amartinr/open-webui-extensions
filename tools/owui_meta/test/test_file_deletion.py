"""File deletion tests (batch).

Design (PLAN.md §7, 2026-08-03): Open WebUI keeps chat-attached files when
the chat is deleted (verified in v0.10.2 chats.py — delete_chat_by_id never
touches Files). delete_files(file_ids) is the single write method addressing
the cleanup, deleting several files in one pass:

- delete_files(file_ids) -> per file: GET metadata + DELETE /api/v1/files/{id}
                            (backend verifies owner/admin/write access,
                            removes storage + vector index). One failed file
                            (missing / not yours / backend error) is reported
                            per id without aborting the rest.

Orphan detection (files whose originating chat is gone) is intentionally NOT
a dedicated method: the model derives it from get_my_files() (which exposes
origin_chat_id) + get_my_chats() — no extra surface needed.

Safety: the whole id list is validated up front (one invalid id rejects the
call before any request); deletion is user-authorized by the tool call
itself; no raw server body is ever dumped (tripwire).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, json_response, make_tools

FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"
FILE_ID_2 = "aaaaaaaa-2bc8-44d7-b4a1-994cdb1c503b"
FILE_ID_3 = "bbbbbbbb-2bc8-44d7-b4a1-994cdb1c503b"


def file_meta(fid, filename, ct="application/pdf", size=2048):
    return {"id": fid, "filename": filename,
            "meta": {"content_type": ct, "size": size}}


def make_delete_handler(file_map, delete_status=200, fail_status=404):
    """Handler serving per-id metadata (GET) and deletions (DELETE)."""
    deleted = set()

    def handler(request):
        path = request.url.path
        prefix = "/api/v1/files/"
        if not path.startswith(prefix):
            return json_response({"unexpected": path}, status=404)
        fid = path[len(prefix):]
        if fid.endswith("/content"):
            fid = fid[: -len("/content")]
            if fid in file_map:
                return json_response({"id": fid, "filename": file_map[fid][0]})
            return json_response({"detail": "nope"}, status=404)
        if request.method == "GET":
            if fid in file_map:
                return json_response(file_meta(fid, file_map[fid][0]))
            return json_response({"detail": "nope"}, status=404)
        if request.method == "DELETE":
            if fid not in file_map:
                return json_response({"detail": "nope"}, status=fail_status)
            if fid in file_map and file_map[fid][1] is False:
                # a file the user cannot delete (e.g. shared, no write)
                return json_response({"detail": "nope"}, status=403)
            deleted.add(fid)
            return json_response({"message": "File deleted successfully"})
        return json_response({"unexpected": path}, status=404)

    handler.deleted = deleted
    return handler


async def test_delete_files_batch_uses_delete_method_per_file():
    handler = make_delete_handler({
        FILE_ID: ("a.pdf", True),
        FILE_ID_2: ("b.png", True),
    })
    seen = []

    def recorder(request):
        seen.append((request.method, request.url.path))
        return handler(request)

    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.delete_files([FILE_ID, FILE_ID_2], __request__=FakeRequest())

    # per file: one GET (metadata) + one DELETE (no trailing slash)
    assert seen.count(("GET", f"/api/v1/files/{FILE_ID}")) == 1
    assert seen.count(("DELETE", f"/api/v1/files/{FILE_ID}")) == 1
    assert seen.count(("GET", f"/api/v1/files/{FILE_ID_2}")) == 1
    assert seen.count(("DELETE", f"/api/v1/files/{FILE_ID_2}")) == 1
    for method, path in seen:
        assert not path.endswith("/")

    payload = json.loads(out)
    assert payload["requested"] == 2
    assert payload["deleted_count"] == 2
    assert payload["failed_count"] == 0
    assert [d["file_id"] for d in payload["deleted"]] == [FILE_ID, FILE_ID_2]
    assert payload["deleted"][0]["filename"] == "a.pdf"


async def test_delete_files_partial_failure_reports_per_id():
    handler = make_delete_handler({
        FILE_ID: ("a.pdf", True),
        FILE_ID_2: ("b.png", True),
        FILE_ID_3: ("shared.pdf", True),
    }, fail_status=403)
    # force FILE_ID_3 to fail at the DELETE stage
    file_map = {
        FILE_ID: ("a.pdf", True),
        FILE_ID_2: ("b.png", True),
        FILE_ID_3: ("shared.pdf", False),
    }

    def h(request):
        path = request.url.path
        fid = path[len("/api/v1/files/"):].split("/")[0]
        if request.method == "GET":
            return json_response(file_meta(fid, file_map[fid][0])) if fid in file_map \
                else json_response({"detail": "nope"}, status=404)
        if request.method == "DELETE":
            if not file_map[fid][1]:
                return json_response({"detail": "nope"}, status=403)
            return json_response({"message": "File deleted successfully"})
        return json_response({"unexpected": path}, status=404)

    tools = make_tools(h, base_url="http://webui.example.test", output_format="json")
    out = await tools.delete_files([FILE_ID, FILE_ID_3], __request__=FakeRequest())

    payload = json.loads(out)
    assert payload["requested"] == 2
    assert payload["deleted_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["deleted"][0]["file_id"] == FILE_ID
    assert payload["failed"][0]["file_id"] == FILE_ID_3
    assert "Forbidden" in payload["failed"][0]["error"]


async def test_delete_files_markdown_summary():
    handler = make_delete_handler({FILE_ID: ("a.pdf", True)})
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.delete_files([FILE_ID], __request__=FakeRequest())
    assert "**Deleted 1 of 1 files**" in out
    assert "| deleted | a.pdf |" in out
    assert FILE_ID in out


async def test_delete_files_404_on_metadata_never_calls_delete():
    calls = []

    def handler(request):
        calls.append(request.method)
        return json_response({"detail": "nope"}, status=404)

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.delete_files([FILE_ID], __request__=FakeRequest())

    payload = json.loads(out)
    assert calls == ["GET"]  # never attempted the DELETE
    assert payload["failed_count"] == 1
    assert "does not exist or does not belong to you" in payload["failed"][0]["error"]


async def test_delete_files_403_on_delete_maps_to_forbidden():
    handler = make_delete_handler({FILE_ID: ("a.pdf", False)})
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.delete_files([FILE_ID], __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["failed_count"] == 1
    assert "Forbidden" in payload["failed"][0]["error"]


async def test_delete_files_whole_list_validated_before_any_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return json_response({"ok": True})

    tools = make_tools(handler, base_url="http://webui.example.test")
    # one good id + one invalid id -> the whole call is rejected, nothing runs
    out = await tools.delete_files([FILE_ID, "bad/id/../x"], __request__=FakeRequest())
    assert calls == []
    assert "Invalid file_id" in out


async def test_delete_files_rejects_non_list():
    tools = make_tools(lambda r: json_response({"ok": True}), base_url="http://webui.example.test")
    out = await tools.delete_files(FILE_ID, __request__=FakeRequest())
    assert "expected a non-empty list" in out


async def test_delete_files_rejects_empty_list():
    tools = make_tools(lambda r: json_response({"ok": True}), base_url="http://webui.example.test")
    out = await tools.delete_files([], __request__=FakeRequest())
    assert "expected a non-empty list" in out


async def test_delete_files_dedupes_and_caps():
    handler = make_delete_handler({FILE_ID: ("a.pdf", True)})
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    # duplicates collapse into one DELETE
    out = await tools.delete_files([FILE_ID, FILE_ID, FILE_ID], __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["requested"] == 1
    assert payload["deleted_count"] == 1
