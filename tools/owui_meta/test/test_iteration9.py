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
  ``search_chats("tag:X")`` and ``get_my_chats(tag="X")``.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

import owui_meta
from helpers import FakeRequest, Recorder, json_response, make_tools

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
    def handler(request):
        assert request.url.params["text"] == "tag:none"
        return json_response([{"id": "c2", "title": "No tags"}])

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.search_chats("tag:none", FakeRequest())
    assert json.loads(out)["query"] == "tag:none"


async def test_get_my_chats_tag_uses_post_tags_route():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/chats/tags"
        body = json.loads(request.content)
        assert body == {"name": "budget", "skip": 0, "limit": 50}
        return json_response([{"id": "c1", "title": "Budget"}])

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_chats(tag="budget", limit=10, __request__=FakeRequest())
    data = json.loads(out)
    assert [c["id"] for c in data["chats"]] == ["c1"]


def test_tag_semantics_documented_in_docstrings():
    # No behavioral guard is added (blocking would break the backend's
    # intended orphan-tag cleanup) — the semantics live in the docstrings.
    def flat(doc: str) -> str:
        return " ".join(doc.split())

    search_doc = flat(owui_meta.Tools.search_chats.__doc__ or "")
    for needle in ("scope limiters", "orphan-tag", "archived chats"):
        assert needle in search_doc, f"search_chats docstring missing {needle!r}"
    list_doc = flat(owui_meta.Tools.get_my_chats.__doc__ or "")
    for needle in ("orphan-tag", "archived chats"):
        assert needle in list_doc, f"get_my_chats docstring missing {needle!r}"


# ── live: decisive AND + consistency (env-gated) ─────────────────────

@live
async def test_live_search_tag_is_scope_limiter_and():
    tools = live_tools()
    tags = (json.loads(await tools.get_my_tags(live_request())) or {}).get("tags") or []
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
async def test_live_search_tag_consistent_with_get_my_chats_tag():
    tools = live_tools()
    tags = (json.loads(await tools.get_my_tags(live_request())) or {}).get("tags") or []
    if not tags:
        pytest.skip("instance has no tags")
    tag = tags[0]["name"]
    by_search = (json.loads(await tools.search_chats(f"tag:{tag}", live_request())) or {}).get("chats", [])
    by_list = (
        json.loads(await tools.get_my_chats(tag=tag, limit=50, __request__=live_request())) or {}
    ).get("chats", [])
    n = min(len(by_search), len(by_list))
    # Both routes order by updated_at desc; the shared head must match.
    assert [c["id"] for c in by_search[:n]] == [c["id"] for c in by_list[:n]]


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
