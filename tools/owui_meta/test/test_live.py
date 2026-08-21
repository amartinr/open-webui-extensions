"""
Live integration suite (Iteration 5) — env-gated.

Re-validates the verified endpoint map (§5) against the REAL instance using a
real (non-persisted) API key. Skipped unless OWUI_META_LIVE_URL and
OWUI_META_LIVE_TOKEN are set (e.g. ``source /tmp/owui_live.env``).

What mocks cannot cover, and this suite pins:

- the SPA HTML catch-all trap: listing routes WITHOUT their trailing slash
  return HTTP 200 ``text/html`` (the reason the route map exists);
- admin-only routes blocked for a user role (``/api/v1/users/`` → 401/403);
- the real ``/api/v1/auths/`` echo: the response body carries the request
  token — the tool must never surface it;
- the ``stats/usage`` pageSize quirk against live data (pageSize ignored);
- field-whitelist leaks against real data (tags ``user_id``, folders
  ``meta``, chat bookkeeping);
- end-to-end token plumbing: real Bearer auth on every allowlisted route.

Assertions are deliberately tolerant of instance data changes (counts vary):
they pin SHAPES, whitelists, error mappings and security invariants, not
specific numbers. ``delete_files`` is NEVER exercised (the only write).
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
from helpers import FakeRequest

LIVE_URL = os.getenv("OWUI_META_LIVE_URL", "").strip()
LIVE_TOKEN = os.getenv("OWUI_META_LIVE_TOKEN", "").strip()

pytestmark = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN),
    reason="set OWUI_META_LIVE_URL and OWUI_META_LIVE_TOKEN to run the live suite",
)

_TIMEOUT = 20


def live_tools(output_format: str = "markdown") -> owui_meta.Tools:
    """A Tools instance pointed at the live instance (no config store, no mocks)."""
    tools = owui_meta.Tools()
    tools.valves.output_format = output_format
    tools._base_url_override = LIVE_URL
    owui_meta.Config = None
    return tools


def live_request(token: str = LIVE_TOKEN) -> FakeRequest:
    return FakeRequest(token=token)


def probe(path: str, token: str = LIVE_TOKEN):
    """Direct API probe (bypasses the tool) for route-map / auth checks."""
    resp = httpx.get(
        LIVE_URL + path,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )
    return resp.status_code, resp.headers.get("content-type", "").split(";")[0].strip().lower(), resp.text


def load_json(out: str) -> dict:
    import json
    payload = json.loads(out)
    assert "error" not in payload, f"unexpected error payload: {payload.get('error')}"
    return payload


async def first_chat_id(tools) -> str:
    out = await tools.get_chats(limit=1, __request__=live_request())
    payload = load_json(out)
    assert payload["chats"], "instance has no chats — cannot run chat tests"
    return payload["chats"][0]["id"]


# ── Authentication & the /auths/ token echo ────────────────────────────

async def test_live_profile_never_echoes_token():
    # The REAL /api/v1/auths/ echoes the request token in its body. The tool's
    # field whitelist must drop it — this is the highest-value live check.
    tools = live_tools(output_format="json")
    out = await tools.get_profile(__request__=live_request())
    payload = load_json(out)
    assert payload["email"], "profile must carry the requester's email"
    assert "token" not in out
    assert LIVE_TOKEN not in out


# ── Endpoint map (§5), one method per read route ───────────────────────

async def test_live_models():
    payload = load_json(await live_tools("json").get_models(__request__=live_request()))
    assert payload["count"] >= 1
    assert all(m.get("id") for m in payload["models"])
    # heavy model definitions are not dumped into the context
    assert "info" not in str(payload)


async def test_live_chats_list():
    payload = load_json(await live_tools("json").get_chats(limit=5, __request__=live_request()))
    assert payload["total"] >= 1
    assert payload["count"] == 5
    for chat in payload["chats"]:
        assert chat.get("id") and chat.get("title")
        assert chat.get("updated_at")


async def test_live_chats_tag_filter_matches_search_prefix():
    # get_chats(tag=...) (pure filter via POST /chats/tags) and a text+tag
    # search must be consistent: the search result (text AND tag) is a
    # subset of the pure tag filter. 9.8: lone "tag:..." is an error, so
    # the search uses a real term (a title from the tag's own chats).
    tools = live_tools("json")
    by_tag = json.loads(await tools.get_chats(limit=50, tag="tool", __request__=live_request()))
    ids_tag = {c["id"] for c in by_tag["chats"]}
    if not ids_tag:
        pytest.skip("instance has no chats with tag 'tool'")
    title = by_tag["chats"][0]["title"]
    by_search = json.loads(await tools.search_chats(f"{title} tag:tool", __request__=live_request()))
    ids_search = {c["id"] for c in by_search["chats"]}
    # text+tag is AND: every search hit carries the tag.
    assert ids_search <= ids_tag, f"search {ids_search} not subset of tag {ids_tag}"
    # the source chat (its title is the term) is in the result.
    assert by_tag["chats"][0]["id"] in ids_search


async def test_live_chat_metadata_and_summary():
    tools = live_tools("json")
    chat_id = await first_chat_id(tools)
    meta = load_json(await tools.get_chat_metadata(chat_id, __request__=live_request()))
    assert meta["id"] == chat_id
    assert isinstance(meta["message_count"], int)
    # metadata must never carry message content
    for key in ("head", "tail", "skipped", "messages"):
        assert key not in meta
    summary = load_json(await tools.get_chat_summary(chat_id, __request__=live_request()))
    assert summary["id"] == chat_id
    assert summary["message_count"] == meta["message_count"]
    # the summary IS the head/tail snippet — it must be present in json too
    assert isinstance(summary.get("head"), list)
    assert summary["skipped"] >= 0


async def test_live_search_text_and_prefixes():
    # UI filter prefixes are parsed server-side. Markdown mode is used for
    # the prefix loop: big result sets (e.g. tag:none -> 60 chats) exceed
    # max_response_chars and are truncated, which would break a strict JSON
    # parse — the truncation note is expected behavior, not a failure.
    # 9.8: every term below has a real text token; prefixes narrow it.
    tools = live_tools("markdown")
    for term in ("meta", "meta tag:tool", "meta pinned:true",
                 "meta archived:true", "meta tag:none"):
        out = await tools.search_chats(term, __request__=live_request())
        assert "Error:" not in out, f"term {term!r} failed: {out[:200]}"
        assert "Search results for" in out, f"term {term!r} missing header"
    # 9.8: pure-prefix calls are errors, never silent full listings.
    for term in ("tag:tool", "pinned:true", "archived:true", "tag:none"):
        out = await tools.search_chats(term, __request__=live_request())
        assert "Error:" in out and "requires a text term" in out, term
    # json mode passes the query through (lenient to truncation on big sets)
    out = await live_tools("json").search_chats("meta", __request__=live_request())
    try:
        payload = json.loads(out)
        assert payload["query"] == "meta"
    except json.JSONDecodeError:
        assert "truncated" in out


async def test_live_search_folder_name_resolution():
    # 9.7: a real folder NAME (with spaces) resolves to the canonical
    # underscore form and returns exactly that folder's chats — no text
    # leak, no silent no-filter. A folder ID is not a name → clean error
    # listing the valid names.
    tools = live_tools("json")
    folders = json.loads(await tools.get_folders(__request__=live_request()))
    names = [f["name"] for f in folders.get("folders", [])]
    if not names:
        pytest.skip("instance has no folders")
    name = names[0]
    out = await tools.search_chats(f"folder:{name}", __request__=live_request())
    assert "Error:" not in out, f"folder {name!r} failed: {out[:300]}"
    payload = json.loads(out)
    expected = "folder:" + name.replace(" ", "_")
    assert payload["query"] == expected, f"query {payload['query']!r} != {expected!r}"
    # A folder UUID is not a valid folder name → clean error with the list.
    fid = folders["folders"][0]["id"]
    out2 = await tools.search_chats(f"folder:{fid}", __request__=live_request())
    assert "Error:" in out2 and "Unknown folder" in out2, out2[:300]


async def test_live_search_snippet_surfaced():
    tools = live_tools("json")
    payload = load_json(await tools.search_chats("meta", __request__=live_request()))
    # if any result carries a snippet, the markdown must render the column
    if any(c.get("snippet") for c in payload["chats"]):
        md = await live_tools("markdown").search_chats("meta", __request__=live_request())
        assert "Snippet" in md


async def test_live_tags_no_leak():
    out = await live_tools("json").get_tags(__request__=live_request())
    payload = load_json(out)
    assert payload["count"] >= 1
    for tag in payload["tags"]:
        assert tag.get("name") and tag.get("id")
    assert "user_id" not in out


async def test_live_archived():
    payload = load_json(await live_tools("json").get_chats(scope="archived", __request__=live_request()))
    assert payload["label"] == "Archived chats"
    assert isinstance(payload["count"], int)
    assert isinstance(payload["chats"], list)


async def test_live_folders_works_or_readable_403():
    # folders is feature-gated: on disabled instances the backend 403s.
    out = await live_tools("markdown").get_folders(__request__=live_request())
    if out.startswith("Error:"):
        assert "Forbidden" in out
        return
    assert "**Folders:" in out
    assert '"meta":' not in out


async def test_live_chat_stats_quirk():
    # pageSize is IGNORED by stats/usage (verified live): requesting 10 must
    # still return up to 50 rows. Direct probe first, then the tool method.
    status, ct, body = probe("/api/v1/chats/stats/usage?page=1&pageSize=10")
    assert status == 200 and ct == "application/json"
    import json
    d = json.loads(body)
    assert d["total"] >= 1
    assert len(d["items"]) > 10, "pageSize=10 must NOT limit rows (pageSize ignored)"

    tools = live_tools("json")
    chat_id = await first_chat_id(tools)
    out = await tools.get_chat_stats(chat_id, __request__=live_request())
    payload = json.loads(out)
    if "error" in payload:
        # EXPERIMENTAL route may not cover every chat — but the error must be clean
        assert "stats" in payload["error"].lower() or "EXPERIMENTAL" in payload["error"]
        return
    assert payload["id"] == chat_id
    assert isinstance(payload["message_count"], int)


async def test_live_files_and_content():
    tools = live_tools("json")
    payload = load_json(await tools.get_files(limit=50, __request__=live_request()))
    assert payload["total"] >= 1
    files = payload["files"]
    assert all(f.get("id") for f in files)
    # pick a text-ish file to exercise the snippet path
    text_file = next(
        (f for f in files if (f.get("content_type") or "").startswith("text/")),
        files[0],
    )
    result = load_json(await tools.get_file_content(text_file["id"], __request__=live_request()))
    if "content" in result:  # text file -> snippet
        assert len(result["content"]) <= 100
        assert result["total_chars"] >= len(result["content"])
    else:  # binary -> note
        assert "note" in result


async def test_live_workspace_resources():
    tools = live_tools("json")
    calls = (
        (tools.get_prompts, "prompts"),
        (tools.get_tools, "tools"),
        (tools.get_knowledge_bases, "knowledge"),
        (tools.get_skills, "skills"),
    )
    for method, key in calls:
        payload = load_json(await method(__request__=live_request()))
        assert key in payload
        assert isinstance(payload.get("count"), int)


async def test_live_shared_and_pinned():
    tools = live_tools("json")
    for out in (
        await tools.get_chats(scope="shared", __request__=live_request()),
        await tools.get_chats(scope="pinned", __request__=live_request()),
    ):
        payload = load_json(out)
        assert isinstance(payload["count"], int)


# ── Traps only live data exposes ───────────────────────────────────────

async def test_live_spa_html_trap():
    # The no-slash variant of a listing route NEVER returns JSON. Today nginx
    # answers the SPA catch-all (HTTP 200 text/html) or a 301 redirect to the
    # canonical slash path — either way the tool's Content-Type gate rejects
    # it. This is WHY the route map must be exact, and only a live probe can
    # catch a backend change here.
    status, ct, body = probe("/api/v1/files")
    assert ct != "application/json", f"no-slash must never be JSON, got {status} {ct}"
    assert status in (200, 301, 302, 307, 308), f"unexpected status {status}"
    # canonical with-slash variant is the real JSON listing
    status, ct, body = probe("/api/v1/files/")
    assert status == 200 and ct == "application/json"
    assert body.lstrip().startswith(("{", "["))


async def test_live_users_blocked_for_user_role():
    # admin-only router: a user-role key must NOT list users (no escalation).
    status, ct, _ = probe("/api/v1/users/")
    assert status in (401, 403), f"expected 401/403 for user role, got {status}"
    assert ct == "application/json"


# ── Output-boundary guards on real data ────────────────────────────────

async def test_live_no_token_in_any_output():
    tools = live_tools("json")
    outputs = []
    outputs.append(await tools.get_profile(__request__=live_request()))
    outputs.append(await tools.get_chats(limit=3, __request__=live_request()))
    outputs.append(await tools.get_tags(__request__=live_request()))
    outputs.append(await tools.get_folders(__request__=live_request()))
    outputs.append(await tools.get_prompts(__request__=live_request()))
    outputs.append(await tools.get_files(limit=3, __request__=live_request()))
    outputs.append(await tools.get_skills(__request__=live_request()))
    for out in outputs:
        assert LIVE_TOKEN not in out
