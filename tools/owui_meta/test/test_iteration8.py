"""
Iteration 8 items 2-6 tests: tags, search prefixes + snippet, archived
chats, chat usage stats (EXPERIMENTAL), folders.

Backend facts verified live 2026-08-20 against the instance:

- ``GET /api/v1/chats/all/tags`` (no slash) -> TagModel list (id, name,
  user_id, meta) — the tool exposes only id/name.
- ``GET /api/v1/chats/archived`` (no slash) -> ChatTitleIdResponse list.
- ``GET /api/v1/chats/stats/usage`` (no slash) -> {items, total}. The route
  IGNORES ``pageSize`` (always up to 50 rows/page, irregular sizes: live
  50/49/49 then an empty page with declared total 149), so the tool must
  iterate until an empty page or the declared total, NOT stop on a short
  page (``short_page_stops=False``).
- ``GET /api/v1/folders/`` (WITH slash) -> FolderNameIdResponse list; gated
  by the folders feature (403 on disabled instances -> readable error).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from helpers import FakeRequest, Recorder, json_response, make_tools

CHAT_ID = "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e"


def _tag(name):
    return {"id": name, "name": name, "user_id": "u-owner", "meta": {"count": 7}}


def api_handler(request):
    """Mock of the Open WebUI internal API for the Iteration 8 endpoints."""
    path = request.url.path
    if path == "/api/v1/chats/all/tags":
        return json_response([_tag(t) for t in ("comfyui", "images", "meta", "tool")])
    if path == "/api/v1/chats/archived":
        return json_response([
            {"id": "a1", "title": "Old project", "created_at": 100, "updated_at": 200},
            {"id": "a2", "title": "Draft notes", "created_at": 300, "updated_at": 400},
        ])
    if path == "/api/v1/chats/search":
        return json_response([
            {"id": CHAT_ID, "title": "Budget planning", "created_at": 1, "updated_at": 2,
             "snippet": "...the Presupuestos Generales del Estado..."},
        ])
    if path == "/api/v1/chats/stats/usage":
        return json_response({"items": [], "total": 0})
    if path == "/api/v1/chats/tags":
        return json_response([
            {"id": "c1", "title": "Tagged chat one", "created_at": 100, "updated_at": 200},
            {"id": "c2", "title": "Tagged chat two", "created_at": 300, "updated_at": 400},
        ])
    if path == "/api/v1/folders/":
        return json_response([
            {"id": "f1", "name": "Budget folder", "meta": {"icon": "sparkles"},
             "parent_id": None, "is_expanded": False, "created_at": 5, "updated_at": 6},
            {"id": "f2", "name": "Meta", "meta": {"icon": "robot"},
             "parent_id": "f1", "is_expanded": True, "created_at": 7, "updated_at": 8},
        ])
    return json_response({"unexpected": path}, status=404)


# ── get_my_chats(tag=...) — pure tag filter (user decision 2026-08-20) ────

async def test_get_my_chats_with_tag_uses_post_tags():
    # get_my_chats(tag=...) must call POST /api/v1/chats/tags with the typed
    # body {name, skip, limit} — NOT the chat listing, NOT search.
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_chats(tag="tool", __request__=FakeRequest())
    assert len(recorder.requests) == 1
    req = recorder.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/chats/tags"
    import json as _json
    assert _json.loads(req.content) == {"name": "tool", "skip": 0, "limit": 50}
    payload = _json.loads(out)
    assert payload["count"] == 2
    # default sort is updated_at desc -> c2 (400) before c1 (200)
    assert payload["chats"][0]["id"] == "c2"
    assert payload["total"] == 2


async def test_get_my_chats_blank_tag_falls_back_to_listing():
    # A blank tag is not a tag filter: the normal listing is used.
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    await tools.get_my_chats(tag="   ", __request__=FakeRequest())
    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/api/v1/chats/"


async def test_get_my_chats_tag_paginates_with_skip():
    # >50 tagged chats -> a second POST with skip=50 (bounded by MAX_PAGES).
    def handler(request):
        if request.url.path != "/api/v1/chats/tags":
            return json_response({"unexpected": request.url.path}, status=404)
        import json as _json
        body = _json.loads(request.content)
        skip = body["skip"]
        if skip == 0:
            items = [{"id": f"t{i:02d}", "title": f"Tag {i}", "created_at": i, "updated_at": i} for i in range(50)]
        else:
            items = [{"id": f"t{i:02d}", "title": f"Tag {i}", "created_at": i, "updated_at": i} for i in range(50, 53)]
        return json_response(items)

    recorder = Recorder(handler)
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_chats(tag="many", limit=100, __request__=FakeRequest())
    assert [json.loads(r.content)["skip"] for r in recorder.requests] == [0, 50]
    payload = json.loads(out)
    assert payload["count"] == 53
    assert payload["total"] == 53


async def test_get_my_chats_tag_markdown_table():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_chats(tag="tool", __request__=FakeRequest())
    assert "**Chats: 2" in out
    assert "| Title | Updated | ID |" in out
    assert "| Tagged chat one" in out


# ── get_my_tags (item 2) ────────────────────────────────────────────────

async def test_get_my_tags_summarizes_id_and_name_only():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_tags(__request__=FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 4
    assert payload["tags"][0] == {"id": "comfyui", "name": "comfyui"}
    dumped = json.dumps(payload)
    # TagModel's user_id/meta bookkeeping must never reach the model
    assert "user_id" not in dumped
    assert '"meta":' not in dumped


async def test_get_my_tags_markdown_table():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_tags(__request__=FakeRequest())
    assert "**Tags: 4**" in out
    assert "| Name | ID |" in out
    assert "| tool | tool |" in out


# ── search_chats prefixes + snippet (item 3) ────────────────────────────

async def test_search_chats_surfaces_snippet():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("Presupuestos", __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["chats"][0]["snippet"] == "...the Presupuestos Generales del Estado..."
    assert payload["chats"][0]["id"] == CHAT_ID


async def test_search_chats_markdown_renders_snippet_column():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.search_chats("Presupuestos", __request__=FakeRequest())
    assert "**Search results for 'Presupuestos': 1**" in out
    assert "| Title | Updated | ID | Snippet |" in out
    assert "Presupuestos Generales" in out


# ── get_archived_chats (item 4) ─────────────────────────────────────────

async def test_get_archived_chats_summarizes_with_label():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_archived_chats(__request__=FakeRequest())
    payload = json.loads(out)
    assert payload["label"] == "Archived chats"
    assert payload["count"] == 2
    assert payload["chats"][0]["id"] == "a1"
    # no message content
    assert "messages" not in json.dumps(payload)


async def test_get_archived_chats_applies_limit():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_archived_chats(limit=1, __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["chats"][0]["id"] == "a1"


async def test_get_archived_chats_markdown_header():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_archived_chats(__request__=FakeRequest())
    assert "**Archived chats: 2**" in out


# ── get_chat_stats (item 5, EXPERIMENTAL) ───────────────────────────────

def _stats_item(cid, tags=None, count=14):
    return {
        "id": cid, "models": {"deepseek-v4-flash": 7}, "message_count": count,
        "history_models": {"deepseek-v4-flash": 7},
        "history_message_count": count, "history_user_message_count": count // 2,
        "history_assistant_message_count": count // 2,
        "average_response_time": 0.42,
        "average_user_message_content_length": 63.2,
        "average_assistant_message_content_length": 0.0,
        "tags": tags or [], "last_message_at": 1787251624,
        "updated_at": 1787251632, "created_at": 1787251244,
    }


def irregular_stats_handler():
    """Mirror the live stats/usage pagination quirk (verified 2026-08-20):

    pageSize is IGNORED: pages come back 50/49/49 rows then an empty page,
    while ``total`` stays 149. A naive short-page heuristic would stop after
    page 2 and never see the target chat on page 3.
    """
    def handler(request):
        if request.url.path != "/api/v1/chats/stats/usage":
            return json_response({"unexpected": request.url.path}, status=404)
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            items = [_stats_item(f"c{i:03d}") for i in range(1, 51)]
        elif page == 2:
            items = [_stats_item(f"c{i:03d}") for i in range(51, 100)]
        elif page == 3:
            items = [_stats_item(f"c{i:03d}") for i in range(100, 148)]
            items.append(_stats_item(CHAT_ID, tags=["budget", "q1"], count=52))
        else:
            items = []
        return json_response({"items": items, "total": 149})
    return handler


async def test_get_chat_stats_finds_chat_across_irregular_pages():
    recorder = Recorder(irregular_stats_handler())
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_chat_stats(CHAT_ID, __request__=FakeRequest())
    # short pages (49 < 50) must NOT stop the iteration: pages 1, 2, 3 are
    # fetched, page 4 is empty (the declared total 149 > 148 accumulated).
    # (Iteration 9 added a best-effort chat fetch for the recomputed length
    # averages — filter the recorder to the stats/usage route.)
    stats_requests = [r for r in recorder.requests if r.url.path == "/api/v1/chats/stats/usage"]
    assert [r.url.params["page"] for r in stats_requests] == ["1", "2", "3", "4"]
    payload = json.loads(out)
    assert payload["id"] == CHAT_ID
    assert payload["message_count"] == 52
    assert payload["tags"] == ["budget", "q1"]
    assert payload["models"] == {"deepseek-v4-flash": 7}
    assert payload["average_response_time"] == 0.42
    # redundant/redundant bookkeeping never exposed
    for key in ("history_models", "user_id"):
        assert key not in payload


async def test_get_chat_stats_markdown_renders_bullets():
    recorder = Recorder(irregular_stats_handler())
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_chat_stats(CHAT_ID, __request__=FakeRequest())
    assert "**Chat stats**" in out
    assert "- Messages: 52" in out
    assert "- Models: deepseek-v4-flash (×7)" in out
    assert "- Tags: budget, q1" in out
    assert "- Avg response time (s): 0.42" in out


async def test_get_chat_stats_not_found_clean_error():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    # api_handler's stats/usage returns an empty items list
    out = await tools.get_chat_stats("does-not-exist", __request__=FakeRequest())
    payload = json.loads(out)
    assert "error" in payload
    assert "EXPERIMENTAL" in payload["error"]


async def test_get_chat_stats_invalid_id_rejected_without_request():
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_chat_stats("../../etc/passwd", __request__=FakeRequest())
    assert "Invalid chat_id" in out
    assert recorder.requests == []


# ── get_my_folders (item 6) ─────────────────────────────────────────────

async def test_get_my_folders_whitelists_fields():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_folders(__request__=FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 2
    folder = payload["folders"][0]
    assert folder == {
        "id": "f1", "name": "Budget folder", "parent_id": None,
        "is_expanded": False, "created_at": 5, "updated_at": 6,
    }
    # FolderNameIdResponse meta (icon) is bookkeeping the model does not need
    dumped = json.dumps(payload)
    assert '"meta":' not in dumped
    assert "user_id" not in dumped


async def test_get_my_folders_markdown_table_with_parent_and_expanded():
    tools = make_tools(api_handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_folders(__request__=FakeRequest())
    assert "**Folders: 2**" in out
    assert "| Name | Parent | Expanded | Created | ID |" in out
    assert "| Meta | f1 | yes |" in out
    assert "| Budget folder | — | no |" in out


async def test_get_my_folders_403_maps_to_readable_error():
    def forbidden(request):
        return json_response({"detail": "folders disabled"}, status=403)

    tools = make_tools(forbidden, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_folders(__request__=FakeRequest())
    assert "Forbidden" in out
