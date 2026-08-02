"""
User-role method tests (DESIGN §6.1): each tool method resolves to the
correct allowlisted route, sends the right query parameters, summarizes the
response, and validates its arguments before making any request.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from helpers import FakeRequest, Recorder, binary_response, json_response, make_tools

CHAT_ID = "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e"
FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"


def api_handler(request):
    """Mock of the Open WebUI internal API for the verified endpoint map."""
    path = request.url.path
    if path == "/api/v1/auths/":
        return json_response({
            "id": "16dcaa6d-7122-4cd5-bc01-823064998d75",
            "name": "Abel",
            "email": "amartinr@lowendlab.com",
            "role": "user",
            "permissions": {"chat": {"controls": True}},
        })
    if path == "/api/models":
        return json_response({"data": [
            {"id": "deepseek-v4-coding-assistant", "name": "DeepSeek V4", "owned_by": "openai",
             "info": {"big": "x" * 500}},
        ]})
    if path == "/api/v1/chats/":
        return json_response([
            {"id": CHAT_ID, "title": "Budget planning", "created_at": 1785457944, "updated_at": 1785458000},
            {"id": "aaaa", "title": "Ideas", "created_at": 1, "updated_at": 2},
        ])
    if path == f"/api/v1/chats/{CHAT_ID}":
        return json_response({
            "id": CHAT_ID, "title": "Budget planning",
            "messages": [{"role": "user", "content": "hi"}],
        })
    if path == "/api/v1/chats/search":
        return json_response([
            {"id": CHAT_ID, "title": "Budget planning", "created_at": 1, "updated_at": 2},
        ])
    if path == "/api/v1/chats/shared":
        return json_response([{"id": "sh1", "title": "Shared chat", "created_at": 1, "updated_at": 2}])
    if path == "/api/v1/chats/pinned":
        return json_response([{"id": "pn1", "title": "Pinned chat", "created_at": 1, "updated_at": 2}])
    if path == "/api/v1/files/":
        return json_response({
            "items": [
                {"id": FILE_ID, "user_id": "16dcaa6d", "filename": "generated-image.png",
                 "meta": {"name": "generated-image.png", "content_type": "image/png", "size": 8796,
                          "data": {"chat_id": CHAT_ID, "message_id": "m1"}},
                 "created_at": 1785457944, "updated_at": 1785457944},
            ],
            "total": 104,
        })
    if path == f"/api/v1/files/{FILE_ID}/content":
        return binary_response(b"hello file content", "text/plain")
    if path == "/api/v1/prompts/":
        return json_response([
            {"id": "pr1", "command": "/news", "name": "Get current news", "content": "Summarize today's news"},
        ])
    if path == "/api/v1/tools/":
        return json_response([
            {"id": "tl1", "name": "Enhance Image", "meta": {"description": "Upscales an image"}},
        ])
    if path == "/api/v1/knowledge/":
        return json_response({"items": [
            {"id": "kb1", "name": "Company docs", "description": "Internal wiki", "created_at": 123},
        ], "total": 1})
    if path == "/api/v1/skills/":
        return json_response([
            {"id": "sk1", "name": "RAG summarizer", "description": "Summarizes documents",
             "is_active": True, "content": "You summarize documents.", "meta": {},
             "created_at": 1, "updated_at": 2},
            {"id": "sk2", "name": "Meeting notes", "description": None, "is_active": False,
             "content": "You take meeting notes.", "meta": {}, "created_at": 3, "updated_at": 4},
        ])
    if path == "/api/v1/skills/id/sk1":
        return json_response({
            "id": "sk1", "name": "RAG summarizer", "description": "Summarizes documents",
            "is_active": True, "content": "You summarize documents.",
            "meta": {"model": "deepseek"}, "created_at": 1, "updated_at": 2, "write_access": True,
            # a shared skill embeds the OWNER's user object — must never leak
            "user_id": "owner-123",
            "user": {"id": "owner-123", "email": "owner@example.com", "name": "Owner", "role": "user"},
            "access_grants": [{"principal_id": "*", "permission": "read"}],
        })
    return json_response({"unexpected": path}, status=404)


async def test_get_my_profile():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_profile(FakeRequest())
    payload = json.loads(out)
    assert payload["name"] == "Abel"
    assert payload["role"] == "user"


async def test_get_models_summarizes():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_models(FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["models"][0]["id"] == "deepseek-v4-coding-assistant"
    # heavy model definitions are not dumped into the context
    assert "info" not in json.dumps(payload)


async def test_get_my_chats_sends_page_size_and_summarizes():
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_chats(limit=5, __request__=FakeRequest())
    # transparent pagination: page_size = min(max(limit, 20), 50)
    assert recorder.requests[0].url.params["pageSize"] == "20"
    assert recorder.requests[0].url.params["page"] == "1"
    payload = json.loads(out)
    assert payload["count"] == 2
    assert payload["chats"][0]["id"] == CHAT_ID
    assert "messages" not in json.dumps(payload)


async def test_get_chat_returns_full_chat():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_chat(CHAT_ID, __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["id"] == CHAT_ID
    assert payload["messages"][0]["content"] == "hi"


async def test_get_chat_invalid_id_rejected_without_request():
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_chat("../../etc/passwd", __request__=FakeRequest())
    assert "Invalid chat_id" in out
    assert recorder.requests == []


async def test_search_chats_sends_text_param():
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://open-webui.private", output_format="json")
    out = await tools.search_chats("budget", __request__=FakeRequest())
    assert recorder.requests[0].url.path == "/api/v1/chats/search"
    assert recorder.requests[0].url.params["text"] == "budget"
    payload = json.loads(out)
    assert payload["query"] == "budget"
    assert payload["chats"][0]["id"] == CHAT_ID


async def test_search_chats_requires_text():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.search_chats("   ", __request__=FakeRequest())
    assert "non-empty 'text'" in out


async def test_get_shared_and_pinned_chats():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    shared = json.loads(await tools.get_shared_chats(__request__=FakeRequest()))
    pinned = json.loads(await tools.get_pinned_chats(__request__=FakeRequest()))
    assert shared["chats"][0]["id"] == "sh1"
    assert pinned["chats"][0]["id"] == "pn1"


async def test_get_my_files_includes_meta_and_total():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_files(__request__=FakeRequest())
    payload = json.loads(out)
    assert payload["total"] == 104
    f = payload["files"][0]
    assert f["filename"] == "generated-image.png"
    assert f["content_type"] == "image/png"
    assert f["size"] == 8796
    assert f["origin_chat_id"] == CHAT_ID


async def test_get_file_content_text_file():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["content_type"] == "text/plain"
    assert "hello file content" in payload["content"]


async def test_get_file_content_binary_file_returns_note():
    def handler(request):
        return binary_response(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png")

    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_file_content(FILE_ID, __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["content_type"] == "image/png"
    assert "Binary content" in payload["note"]
    assert "content" not in payload


async def test_get_my_prompts():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_prompts(FakeRequest())
    payload = json.loads(out)
    assert payload["prompts"][0]["command"] == "/news"


async def test_get_my_tools():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_tools(FakeRequest())
    payload = json.loads(out)
    assert payload["tools"][0]["name"] == "Enhance Image"
    assert payload["tools"][0]["description"] == "Upscales an image"


async def test_get_knowledge_bases():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_knowledge_bases(FakeRequest())
    payload = json.loads(out)
    assert payload["total"] == 1
    assert payload["knowledge"][0]["name"] == "Company docs"


async def test_get_my_skills_summarizes_without_content():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_skills(FakeRequest())
    payload = json.loads(out)
    assert payload["count"] == 2
    s = payload["skills"][0]
    assert s["name"] == "RAG summarizer"
    assert s["is_active"] is True
    # the listing stays light: the skill's content (its instructions) is not dumped
    assert "content" not in json.dumps(payload)


async def test_get_skill_returns_full_detail():
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_skill("sk1", __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["id"] == "sk1"
    assert payload["content"] == "You summarize documents."
    assert payload["meta"] == {"model": "deepseek"}


async def test_get_skill_strips_owner_and_bookkeeping():
    # SECURITY (whitelist): the raw SkillAccessResponse embeds the OWNER's
    # UserResponse (email!) for shared skills, plus access_grants/write_access
    # bookkeeping. None of it must reach the model — in any output format.
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_skill("sk1", __request__=FakeRequest())
    assert "owner@example.com" not in out
    payload = json.loads(out)
    assert "user" not in payload
    assert "user_id" not in payload
    assert "access_grants" not in payload
    assert "write_access" not in payload

    # markdown mode also clean
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="markdown")
    out = await tools.get_skill("sk1", __request__=FakeRequest())
    assert "owner@example.com" not in out
    assert "owner-123" not in out


async def test_get_chat_strips_bookkeeping_fields():
    # SECURITY (whitelist): the full ChatResponse carries bookkeeping noise
    # (user_id, meta, tasks, summary, folder_id) that json mode used to dump
    # raw. The conversation itself is kept (that is the feature).
    def handler(request):
        return json_response({
            "id": CHAT_ID, "title": "Budget planning", "user_id": "u1",
            "folder_id": "f1", "meta": {"profile": "x"}, "tasks": [],
            "summary": "a summary", "last_read_at": 123,
            "messages": [{"role": "user", "content": "hola"}],
            "created_at": 1, "updated_at": 2, "pinned": False, "archived": False,
        })

    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_chat(CHAT_ID, __request__=FakeRequest())
    payload = json.loads(out)
    assert payload["messages"][0]["content"] == "hola"  # conversation kept
    for noise in ("user_id", "folder_id", "meta", "tasks", "summary", "last_read_at"):
        assert noise not in payload, f"{noise} should be stripped"

    # markdown mode shows the conversation as before
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="markdown")
    out = await tools.get_chat(CHAT_ID, __request__=FakeRequest())
    assert "**user**" in out
    assert "hola" in out
    assert "a summary" not in out


async def test_get_skill_invalid_id_rejected_without_request():
    recorder = Recorder(api_handler)
    tools = make_tools(recorder, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_skill("../../etc/passwd", __request__=FakeRequest())
    assert "Invalid skill_id" in out
    assert recorder.requests == []


async def test_methods_work_without_request_object_when_token_present():
    # __request__ is injected by the harness; a plain dict-like object with
    # state.token is the only thing the engine actually needs.
    tools = make_tools(api_handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_models(FakeRequest(token="sk-x"))
    assert json.loads(out)["count"] == 1
