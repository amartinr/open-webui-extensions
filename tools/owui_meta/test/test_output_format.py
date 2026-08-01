"""
Markdown output rendering tests (DESIGN §8.8).

The tool returns Markdown by default (tables for lists, bullets for
details, fenced blocks for content) because models read plain text better
than nested JSON. Raw numeric values (byte sizes) are passed through
unformatted so the model never has to parse unit prefixes; IDs are always
present so the model can call follow-up methods.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, binary_response, json_response, make_tools

CHAT_ID = "b5d844f0-85c5-4cdc-8cf3-4f2366bc249e"
FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"


def md_tools(handler):
    return make_tools(handler, base_url="http://open-webui.private", output_format="markdown")


async def test_profile_is_bullets():
    def handler(request):
        return json_response({
            "id": "16dcaa6d-7122-4cd5-bc01-823064998d75",
            "name": "Abel",
            "email": "amartinr@lowendlab.com",
            "role": "user",
            "permissions": {"chat": {"controls": True}},
        })

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert "**Profile**" in out
    assert "- Name: Abel" in out
    assert "- Email: amartinr@lowendlab.com" in out
    assert "- Role: user" in out
    assert "- ID: 16dcaa6d-7122-4cd5-bc01-823064998d75" in out
    assert "**Permissions**" in out
    # permissions rendered as a hierarchy, NOT raw JSON
    assert "- **Chat**" in out
    assert "  - **Controls**: true" in out
    assert "{" not in out
    assert "}" not in out
    assert not out.lstrip().startswith("{")  # not JSON


async def test_profile_permissions_hierarchy_full():
    # A permissions object like the real v0.10.2 profile: deeply nested, must
    # render as indented bullets (hybrid strategy), never embedded JSON.
    perms = {
        "workspace": {"models": False, "knowledge": True, "prompts": True},
        "chat": {"controls": True, "file_upload": True, "delete": True},
        "features": {"api_keys": True, "web_search": True},
        "settings": {"interface": True},
    }

    def handler(request):
        return json_response({"id": "u1", "name": "Abel", "role": "user", "permissions": perms})

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert "- **Workspace**" in out
    assert "  - **Models**: false" in out
    assert "  - **Knowledge**: true" in out
    assert "- **Features**" in out
    assert "  - **Api Keys**: true" in out  # snake_case key humanized
    assert "{" not in out
    assert "}" not in out


async def test_multimodal_chat_content_renders_hierarchy():
    # Chat message content can be a list of parts (multimodal); it must render
    # as hierarchy, not Python repr / JSON.
    def handler(request):
        return json_response({
            "id": CHAT_ID, "title": "Media",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "here is the result"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ]},
            ],
        })

    out = await md_tools(handler).get_chat(CHAT_ID, __request__=FakeRequest())
    assert "**assistant**" in out
    assert "1. **Type**: text" in out
    assert "  - **Text**: here is the result" in out
    assert "2. **Type**: image_url" in out
    assert "  - **Image Url**" in out
    assert "    - **Url**: data:image/png;base64,AAA" in out
    assert "{" not in out


async def test_files_table_with_raw_bytes():
    def handler(request):
        return json_response({
            "items": [
                {"id": FILE_ID, "filename": "generated-image.png",
                 "meta": {"name": "generated-image.png", "content_type": "image/png", "size": 8796,
                          "data": {"chat_id": CHAT_ID, "message_id": "m1"}},
                 "created_at": 1785457944, "updated_at": 1785457944},
                {"id": "f2", "filename": "budget.csv",
                 "meta": {"name": "budget.csv", "content_type": "text/csv", "size": 152340, "data": {}},
                 "created_at": 1785440000, "updated_at": 1785440000},
            ],
            "total": 104,
        })

    out = await md_tools(handler).get_my_files(FakeRequest())
    # summary line carries counts
    assert "**Files: 2 (104 total on server)**" in out
    # header + separator
    assert "| Filename | Type | Size (bytes) | Created | Origin chat | ID |" in out
    # raw byte sizes, no unit prefixes
    assert "| 8796 |" in out
    assert "| 152340 |" in out
    assert "KB" not in out
    # IDs always present
    assert FILE_ID in out
    # origin cross-referencing: generated file shows the chat id, plain file shows a dash
    assert "| " + CHAT_ID + " |" in out
    assert "| — |" in out


async def test_chats_table():
    def handler(request):
        return json_response([
            {"id": CHAT_ID, "title": "Budget planning", "created_at": 1785457944, "updated_at": 1785458000},
        ])

    out = await md_tools(handler).get_my_chats(__request__=FakeRequest())
    assert "**Chats: 1**" in out
    assert "| Title | Updated | ID |" in out
    # epoch 1785458000 == 2026-07-31 00:33 UTC
    assert "| Budget planning | 2026-07-31 00:33 | " + CHAT_ID + " |" in out
    # readable date, not epoch
    assert "1785458000" not in out


async def test_search_chats_header_includes_query():
    def handler(request):
        return json_response([{"id": CHAT_ID, "title": "Gastos de ayer", "created_at": 1, "updated_at": 2}])

    out = await md_tools(handler).search_chats("gastos", __request__=FakeRequest())
    assert "**Search results for 'gastos': 1**" in out
    assert "| Gastos de ayer |" in out


async def test_single_chat_renders_messages():
    def handler(request):
        return json_response({
            "id": CHAT_ID, "title": "Budget planning",
            "messages": [
                {"role": "user", "content": "hola"},
                {"role": "assistant", "content": "hola, ¿qué tal?"},
            ],
        })

    out = await md_tools(handler).get_chat(CHAT_ID, __request__=FakeRequest())
    assert "**Chat: Budget planning** (id: " + CHAT_ID + ")" in out
    assert "**user**\nhola" in out
    assert "**assistant**\nhola, ¿qué tal?" in out


async def test_file_text_content_is_fenced():
    def handler(request):
        return binary_response(b"a,b,c\n1,2,3", "text/csv")

    out = await md_tools(handler).get_file_content(FILE_ID, __request__=FakeRequest())
    assert "**File: " + FILE_ID + "** (text/csv, 11 bytes)" in out
    assert "```csv" in out
    assert "a,b,c" in out


async def test_file_binary_content_is_a_note():
    def handler(request):
        return binary_response(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "image/png")

    out = await md_tools(handler).get_file_content(FILE_ID, __request__=FakeRequest())
    assert "**File: " + FILE_ID + "** (image/png, 18 bytes)" in out
    assert "Binary content" in out
    assert "PNG" not in out  # no raw bytes leaked


async def test_models_prompts_tools_knowledge_tables():
    def handler(request):
        path = request.url.path
        if path == "/api/models":
            return json_response({"data": [{"id": "m1", "name": "DeepSeek", "owned_by": "openai"}]})
        if path == "/api/v1/prompts/":
            return json_response([{"id": "p1", "command": "/news", "name": "Get news", "content": "..."}])
        if path == "/api/v1/tools/":
            return json_response([{"id": "t1", "name": "Enhance", "meta": {"description": "Upscale"}}])
        if path == "/api/v1/knowledge/":
            return json_response({"items": [{"id": "k1", "name": "Docs", "description": "Wiki", "created_at": 1}], "total": 1})
        return json_response({}, status=404)

    tools = md_tools(handler)
    r = FakeRequest()
    models = await tools.get_models(r)
    prompts = await tools.get_my_prompts(r)
    tools_list = await tools.get_my_tools(r)
    kb = await tools.get_knowledge_bases(r)
    assert "**Models: 1**" in models and "| m1 |" in models
    assert "**Prompts: 1**" in prompts and "| /news |" in prompts
    assert "**Tools: 1**" in tools_list and "| Enhance |" in tools_list
    assert "**Knowledge bases: 1**" in kb and "| Docs |" in kb


async def test_error_is_plain_text_not_json():
    def handler(request):
        return json_response({"detail": "nope"}, status=403)

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert out.startswith("Error: Forbidden")
    assert "{" not in out


async def test_json_mode_still_works():
    def handler(request):
        return json_response({"id": "u1", "name": "Abel"})

    import json as _json

    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_profile(FakeRequest())
    payload = _json.loads(out)
    assert payload["name"] == "Abel"


async def test_no_token_in_markdown_output():
    def handler(request):
        return json_response({"id": "u1", "name": "Abel", "blob": "x" * 200})

    tools = md_tools(handler)
    out = await tools.get_my_profile(FakeRequest(token="sk-secret-abc"))
    assert "sk-secret-abc" not in out
