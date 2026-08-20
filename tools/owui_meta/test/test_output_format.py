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
    return make_tools(handler, base_url="http://webui.example.test", output_format="markdown")


async def test_profile_is_bullets():
    def handler(request):
        return json_response({
            "id": "16dcaa6d-7122-4cd5-bc01-823064998d75",
            "name": "John Doe",
            "email": "john.doe@example.com",
            "role": "user",
            "permissions": {"chat": {"controls": True}},
        })

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert "**Profile**" in out
    assert "- Name: John Doe" in out
    assert "- Email: john.doe@example.com" in out
    assert "- Role: user" in out
    assert "- ID: 16dcaa6d-7122-4cd5-bc01-823064998d75" in out
    assert "**Permissions**" in out
    # permissions rendered as a hierarchy, NOT raw JSON
    assert "- Chat" in out
    assert "  - Controls: true" in out
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
        return json_response({"id": "u1", "name": "John Doe", "role": "user", "permissions": perms})

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert "- Workspace" in out
    assert "  - Models: false" in out
    assert "  - Knowledge: true" in out
    assert "- Features" in out
    assert "  - Api Keys: true" in out  # snake_case key humanized
    assert "{" not in out
    assert "}" not in out


async def test_multimodal_chat_content_renders_hierarchy():
    # Chat message content can be a list of parts (multimodal); the snippet
    # extracts the text parts and drops the rest (images cannot render in a
    # text snippet).
    def handler(request):
        return json_response({
            "id": CHAT_ID, "title": "Media",
            "chat": {"models": [], "history": {"currentId": "m1", "messages": {
                "m1": {"id": "m1", "role": "assistant", "parentId": None, "timestamp": 1,
                       "content": [
                           {"type": "text", "text": "here is the result"},
                           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                       ]},
            }}},
            "meta": {}, "folder_id": None, "pinned": False, "archived": False,
            "created_at": 1, "updated_at": 2,
        })

    out = await md_tools(handler).get_chat_summary(CHAT_ID, __request__=FakeRequest())
    assert "**Chat: Media**" in out
    assert "**Assistant**: here is the result" in out
    assert "data:image" not in out  # non-text parts dropped from the snippet
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

    out = await md_tools(handler).get_my_files(__request__=FakeRequest())
    # summary line carries counts (matched = returned; total = on server)
    assert "**Files: 2 matched (104 total on server)**" in out
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
            "chat": {"models": [], "history": {"currentId": "m2", "messages": {
                "m1": {"id": "m1", "role": "user", "content": "hola", "parentId": None, "timestamp": 1},
                "m2": {"id": "m2", "role": "assistant", "content": "hola, ¿qué tal?", "parentId": "m1", "timestamp": 2},
            }}},
            "meta": {}, "folder_id": None, "pinned": False, "archived": False,
            "created_at": 1, "updated_at": 2,
        })

    out = await md_tools(handler).get_chat_summary(CHAT_ID, __request__=FakeRequest())
    assert "**Chat: Budget planning**" in out
    assert "- Messages: 2" in out
    assert "**User**: hola" in out
    assert "**Assistant**: hola, ¿qué tal?" in out
    # small chat -> no ellipsis line
    assert "… (" not in out


async def test_chat_metadata_renders_bullets_without_content():
    def handler(request):
        return json_response({
            "id": CHAT_ID, "title": "Budget planning",
            "chat": {"models": ["m1"], "history": {"currentId": "m1", "messages": {
                "m1": {"id": "m1", "role": "user", "content": "hola", "parentId": None, "timestamp": 1},
            }}},
            "meta": {"tags": ["budget"]}, "folder_id": None, "pinned": False, "archived": False,
            "created_at": 1, "updated_at": 2,
        })

    out = await md_tools(handler).get_chat_metadata(CHAT_ID, __request__=FakeRequest())
    assert "**Chat: Budget planning**" in out
    assert "- Messages: 1" in out
    assert "Models: m1" in out
    assert "Tags: budget" in out
    # no message content in the metadata view
    assert "**User**" not in out
    assert "hola" not in out


async def test_snippet_skips_intermediate_messages():
    def handler(request):
        msgs = {
            f"m{i}": {"id": f"m{i}", "role": "user" if i % 2 else "assistant",
                      "content": f"msg {i}", "parentId": None if i == 1 else f"m{i-1}", "timestamp": i}
            for i in range(1, 11)
        }
        return json_response({
            "id": CHAT_ID, "title": "Long chat",
            "chat": {"models": [], "history": {"currentId": "m10", "messages": msgs}},
            "meta": {}, "folder_id": None, "pinned": False, "archived": False,
            "created_at": 1, "updated_at": 10,
        })

    out = await md_tools(handler).get_chat_summary(CHAT_ID, __request__=FakeRequest())
    assert "**Chat: Long chat**" in out
    assert "- Messages: 10" in out
    assert "**User**: msg 1" in out
    assert "**Assistant**: msg 2" in out
    assert "**User**: msg 3" in out
    # head=3 + tail=3 of 10 -> 4 skipped (fixed constants, user decision)
    assert "… ( 4 messages skipped ) …" in out
    assert "**Assistant**: msg 8" in out
    assert "**User**: msg 9" in out
    assert "**Assistant**: msg 10" in out
    # intermediate messages never appear
    assert "msg 5" not in out


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
    assert "embedded in the conversation" in out
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


async def test_skills_list_table():
    def handler(request):
        return json_response([
            {"id": "sk1", "name": "RAG summarizer", "description": "Summarizes documents",
             "is_active": True, "content": "x" * 100, "meta": {}, "created_at": 1, "updated_at": 2},
            {"id": "sk2", "name": "Meeting notes", "description": None, "is_active": False,
             "content": "y" * 100, "meta": {}, "created_at": 3, "updated_at": 4},
        ])

    out = await md_tools(handler).get_my_skills(FakeRequest())
    assert "**Skills: 2**" in out
    assert "| Name | Description | Active | ID |" in out
    assert "| RAG summarizer | Summarizes documents | true | sk1 |" in out
    assert "| Meeting notes | None | false | sk2 |" in out
    # the listing does not dump the skill's content (its instructions)
    assert "x" * 100 not in out


async def test_single_skill_detail_with_content():
    def handler(request):
        return json_response({
            "id": "sk1", "name": "RAG summarizer", "description": "Summarizes documents",
            "is_active": True, "content": "You summarize documents concisely.",
            "meta": {"model": "deepseek"}, "created_at": 1785457944, "updated_at": 1785458000,
        })

    out = await md_tools(handler).get_skill("sk1", __request__=FakeRequest())
    assert "**Skill: RAG summarizer** (id: sk1)" in out
    assert "- Description: Summarizes documents" in out
    assert "- Active: true" in out
    # readable UTC dates, not epoch ints
    assert "- Created: 2026-07-31 00:32" in out
    assert "- Updated: 2026-07-31 00:33" in out
    assert "1785458000" not in out
    # content in a fenced block, meta as hierarchy — never embedded JSON
    assert "**Content**" in out
    assert "```text" in out
    assert "You summarize documents concisely." in out
    assert "**Meta**" in out
    assert "- Model: deepseek" in out
    assert "{" not in out


async def test_error_is_plain_text_not_json():
    def handler(request):
        return json_response({"detail": "nope"}, status=403)

    out = await md_tools(handler).get_my_profile(FakeRequest())
    assert out.startswith("Error: Forbidden")
    assert "{" not in out


async def test_json_mode_still_works():
    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    import json as _json

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_profile(FakeRequest())
    payload = _json.loads(out)
    assert payload["name"] == "John Doe"


async def test_no_token_in_markdown_output():
    def handler(request):
        return json_response({"id": "u1", "name": "John Doe", "blob": "x" * 200})

    tools = md_tools(handler)
    out = await tools.get_my_profile(FakeRequest(token="sk-secret-abc"))
    assert "sk-secret-abc" not in out


async def test_user_valve_overrides_admin_markdown_default():
    # Admin default is markdown; a user choosing json must get json.
    import json as _json

    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    uv = tools.UserValves(output_format="json")
    user = {"valves": uv}
    out = await tools.get_my_profile(FakeRequest(), __user__=user)
    payload = _json.loads(out)  # json output is parseable
    assert payload["name"] == "John Doe"


async def test_user_valve_default_is_markdown():
    # UserValves defaults to markdown (the tool's default) — no 'inherit' concept.
    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    uv = tools.UserValves()  # default markdown
    user = {"valves": uv}
    out = await tools.get_my_profile(FakeRequest(), __user__=user)
    assert "**Profile**" in out  # markdown


async def test_user_valve_json_overrides_default_markdown():
    # A user choosing json must get json even when the tool default is markdown.
    import json as _json

    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    uv = tools.UserValves(output_format="json")
    out = await tools.get_my_profile(FakeRequest(), __user__={"valves": uv})
    payload = _json.loads(out)
    assert payload["name"] == "John Doe"


async def test_user_valve_json_overrides_admin_markdown_in_errors():
    # Errors must also respect the user's format choice.
    def handler(request):
        return json_response({"detail": "nope"}, status=403)

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    uv = tools.UserValves(output_format="json")
    out = await tools.get_my_profile(FakeRequest(), __user__={"valves": uv})
    import json as _json

    payload = _json.loads(out)
    assert "error" in payload
    assert "Forbidden" in payload["error"]


async def test_user_valve_markdown_overrides_admin_json():
    # Admin default json; user chooses markdown -> markdown.
    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    uv = tools.UserValves(output_format="markdown")
    out = await tools.get_my_profile(FakeRequest(), __user__={"valves": uv})
    assert "**Profile**" in out


async def test_no_user_valves_uses_admin_default():
    def handler(request):
        return json_response({"id": "u1", "name": "John Doe"})

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_profile(FakeRequest(), __user__=None)
    import json as _json

    assert _json.loads(out)["name"] == "John Doe"
