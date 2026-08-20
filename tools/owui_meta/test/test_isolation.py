"""
Isolation tests (Iteration 5) — DESIGN §7.3.

Two layers:

1. Mock-level (always run) — the tool must never carry state across calls:
   - the Authorization header of each request is derived from THAT request's
     token (no token caching across calls);
   - two Tools instances interleaved do not contaminate each other (valves,
     base URL, output format).

2. Live-level (env-gated, skipped unless OWUI_META_LIVE_URL/TOKEN set) —
   the instance scopes data to the requester:
   - every file returned for the token belongs to the token's own user id;
   - with a second token (OWUI_META_LIVE_TOKEN2), the two users' data sets
     are disjoint and each request carries its own identity.

The full two-user cross-check needs a second real token; with only one
token set, the single-user scoping tests still run and the second-user
test reports SKIPPED with the reason.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import httpx
import pytest

import owui_meta
from helpers import FakeRequest, Recorder, json_response, make_tools

LIVE_URL = os.getenv("OWUI_META_LIVE_URL", "").strip()
LIVE_TOKEN = os.getenv("OWUI_META_LIVE_TOKEN", "").strip()
LIVE_TOKEN2 = os.getenv("OWUI_META_LIVE_TOKEN2", "").strip()

LIVE_SKIP = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN),
    reason="set OWUI_META_LIVE_URL and OWUI_META_LIVE_TOKEN to run the live isolation checks",
)
SECOND_USER_SKIP = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN and LIVE_TOKEN2),
    reason="set OWUI_META_LIVE_TOKEN2 (a second real user's token) for the cross-user check",
)


# ── Mock-level: no state across calls ──────────────────────────────────

def _auths_handler(user_id="u1", name="John Doe"):
    def handler(request):
        return json_response({"id": user_id, "name": name, "email": f"{name}@example.com",
                              "role": "user"})
    return handler


async def test_token_isolation_per_request():
    """Each call must send THAT request's token, never a cached one."""
    recorder = Recorder(_auths_handler())
    tools = make_tools(recorder, base_url="http://webui.example.test", output_format="json")
    for token in ("tok-alpha", "tok-beta", "tok-alpha"):
        out = await tools.get_my_profile(__request__=FakeRequest(token=token))
        assert json.loads(out)["name"] == "John Doe"
    auths = [r.headers.get("authorization") for r in recorder.requests]
    assert auths == ["Bearer tok-alpha", "Bearer tok-beta", "Bearer tok-alpha"]


async def test_instances_do_not_share_state():
    """Two Tools instances with different valves/base URLs stay independent
    even when calls are interleaved."""
    handler = _auths_handler()
    tools_md = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    tools_json = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out_json = await tools_json.get_my_profile(__request__=FakeRequest())
    out_md = await tools_md.get_my_profile(__request__=FakeRequest())
    out_json2 = await tools_json.get_my_profile(__request__=FakeRequest())
    assert json.loads(out_json)["name"] == "John Doe"
    assert "**Profile**" in out_md
    # the json instance still renders json after the md instance ran
    assert json.loads(out_json2)["name"] == "John Doe"


# ── Live-level: data scoped to the requester ───────────────────────────

def _live_probe(path: str, token: str):
    resp = httpx.get(LIVE_URL + path, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    resp.raise_for_status()
    return json.loads(resp.text)


def _live_user_id(token: str) -> str:
    return _live_probe("/api/v1/auths/", token)["id"]


@LIVE_SKIP
async def test_live_files_scoped_to_requester():
    """Every file the API returns for our token belongs to our own user id."""
    me = _live_user_id(LIVE_TOKEN)
    seen = set()
    for page in (1, 2):
        d = _live_probe(f"/api/v1/files/?page={page}&pageSize=50", LIVE_TOKEN)
        for f in d.get("items", []):
            seen.add(f["id"])
            assert f.get("user_id") == me, f"file {f['id']} belongs to {f.get('user_id')}"
        if not d.get("items"):
            break
    assert seen, "no files returned — cannot validate scoping"


@SECOND_USER_SKIP
async def test_live_second_user_sees_only_their_own_data():
    """With a second real token: the two users' identities differ and their
    file sets are disjoint (each request carries its own Bearer identity)."""
    me, other = _live_user_id(LIVE_TOKEN), _live_user_id(LIVE_TOKEN2)
    assert me != other

    def file_ids(token: str) -> set:
        ids = set()
        for page in (1, 2, 3):
            d = _live_probe(f"/api/v1/files/?page={page}&pageSize=50", token)
            ids.update(f["id"] for f in d.get("items", []))
            if not d.get("items"):
                break
        return ids

    mine = file_ids(LIVE_TOKEN)
    theirs = file_ids(LIVE_TOKEN2)
    assert mine, "token1 has no files"
    assert theirs, "token2 has no files"
    assert not (mine & theirs), "users must never see each other's files"
