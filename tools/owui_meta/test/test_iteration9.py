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
