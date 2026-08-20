"""
Trailing-slash route map regression tests.

Discovered during live integration (2026-08-01): in the deployed Open WebUI
(v0.10.2), LISTING routes are registered WITH a trailing slash
(``GET /api/v1/auths/``, ``/api/v1/chats/``, ``/api/v1/files/``,
``/api/v1/prompts/``, ``/api/v1/tools/``, ``/api/v1/knowledge/``,
``/api/v1/users/``). Calling them WITHOUT the slash falls through to the SPA
HTML catch-all (HTTP 200, ``text/html``) — which is exactly the failure seen
live ("Expected JSON … got 'text/html'"). Sub-resources (``/search``,
``/pinned``, ``/shared``, ``/{id}``) and ``/api/models`` are registered
WITHOUT a trailing slash and must NOT have one.

This suite pins that map: every method must hit exactly the canonical path
(with or without slash as appropriate). If a future Open WebUI version
changes the registration, these tests will flag it immediately.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, json_response, make_tools

CHAT_ID = "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e"
FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"
SKILL_ID = "meeting-notes"


def generic_handler(expected_paths, fallback=None):
    """Return a handler that records request paths and 200-JSONs the expected ones."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path in expected_paths:
            return json_response({"ok": True, "path": request.url.path})
        if fallback is not None:
            return fallback(request)
        return json_response({"unexpected": request.url.path}, status=404)

    handler.seen = seen
    return handler


def assert_route(tools, method, args, expected):
    """Run a method and assert it requested exactly ``expected``."""
    recorder = generic_handler([expected], fallback=lambda r: json_response({"unexpected": r.url.path}, status=200))
    tools._transport = __import__("httpx").MockTransport(recorder)
    tools._base_url_override = "http://open-webui.private"
    out = getattr(tools, method)(*args, __request__=FakeRequest())
    # ensure coroutine runs (async in pytest-asyncio auto mode)
    import asyncio

    asyncio.get_event_loop()
    # The tool runs async; we are already inside async tests, so await it:
    # but assert_route is sync — handled by returning the coroutine in async tests.
    return out, recorder.seen


async def _run_and_record(method, args, expected):
    recorder = generic_handler([expected], fallback=lambda r: json_response({"unexpected": r.url.path}, status=200))
    tools = make_tools(recorder, base_url="http://open-webui.private")
    await getattr(tools, method)(*args, __request__=FakeRequest())
    return recorder.seen


# ── Canonical map (verified live 2026-08-01) ──────────────────────────
WITH_SLASH = {
    "/api/v1/auths/",
    "/api/v1/chats/",
    "/api/v1/files/",
    "/api/v1/prompts/",
    "/api/v1/tools/",
    "/api/v1/knowledge/",
    "/api/v1/skills/",
}
WITHOUT_SLASH = {
    "/api/models",
    "/api/v1/chats/search",
    "/api/v1/chats/pinned",
    "/api/v1/chats/shared",
    f"/api/v1/chats/{CHAT_ID}",
    f"/api/v1/files/{FILE_ID}/content",
    f"/api/v1/skills/id/{SKILL_ID}",
}


async def _run_method(tools, method, *args):
    return await getattr(tools, method)(*args, __request__=FakeRequest())


async def test_listing_routes_have_trailing_slash():
    # Each listing method must hit the WITH-slash path, not the no-slash one.
    cases = [
        ("get_my_profile", (), "/api/v1/auths/"),
        ("get_my_chats", (), "/api/v1/chats/"),
        ("get_my_files", (), "/api/v1/files/"),
        ("get_my_prompts", (), "/api/v1/prompts/"),
        ("get_my_tools", (), "/api/v1/tools/"),
        ("get_knowledge_bases", (), "/api/v1/knowledge/"),
        ("get_my_skills", (), "/api/v1/skills/"),
    ]
    for method, args, route in cases:
        seen = await _run_and_record(method, args, route)
        assert route in seen, f"{method} did not hit {route}; saw {seen}"
        assert route.rstrip("/") not in seen, f"{method} hit no-slash variant"


async def test_subresource_routes_without_trailing_slash():
    # Sub-resources and /api/models must NOT get a trailing slash.
    cases = [
        ("get_models", (), "/api/models"),
        ("search_chats", ("budget",), "/api/v1/chats/search"),
        ("get_pinned_chats", (), "/api/v1/chats/pinned"),
        ("get_shared_chats", (), "/api/v1/chats/shared"),
        ("get_chat_summary", (CHAT_ID,), f"/api/v1/chats/{CHAT_ID}"),
        ("get_chat_metadata", (CHAT_ID,), f"/api/v1/chats/{CHAT_ID}"),
        ("get_file_content", (FILE_ID,), f"/api/v1/files/{FILE_ID}/content"),
        ("get_skill", (SKILL_ID,), f"/api/v1/skills/id/{SKILL_ID}"),
    ]
    for method, args, route in cases:
        seen = await _run_and_record(method, args, route)
        assert route in seen, f"{method} did not hit {route}; saw {seen}"


async def test_no_slash_variants_are_never_used():
    # Guard: the no-slash variants of listing routes (which return SPA HTML
    # in v0.10.2) must never be requested.
    cases = [
        ("get_my_profile", (), "/api/v1/auths/", "/api/v1/auths"),
        ("get_my_chats", (), "/api/v1/chats/", "/api/v1/chats"),
        ("get_my_files", (), "/api/v1/files/", "/api/v1/files"),
        ("get_my_prompts", (), "/api/v1/prompts/", "/api/v1/prompts"),
        ("get_my_tools", (), "/api/v1/tools/", "/api/v1/tools"),
        ("get_knowledge_bases", (), "/api/v1/knowledge/", "/api/v1/knowledge"),
        ("get_my_skills", (), "/api/v1/skills/", "/api/v1/skills"),
    ]
    for method, args, canonical, forbidden in cases:
        seen = await _run_and_record(method, args, canonical)
        assert canonical in seen, f"{method} did not hit {canonical}; saw {seen}"
        assert forbidden not in seen, f"{method} hit forbidden no-slash {forbidden}"
