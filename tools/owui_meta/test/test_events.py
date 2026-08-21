"""Status-event and error-event tests (Iteration 4).

UX contract (DESIGN §8.5, PLAN.md Iteration 4, decision 2026-08-03):

- Progress is shown via ``status`` events (done=False while running, a final
  done=True stops the shimmer). These are gated by the ``verbose`` valve
  (per-user choice, else admin), so quiet users are not spammed.
- Errors are shown via ``chat:message:error`` (the error block the frontend
  renders in the message). They are ALWAYS emitted, never gated by verbose,
  and consolidated: at most ONE error event per tool call — a batch
  delete_files with several failures emits a single \"N of M failed\" summary
  instead of one error per file, so the user is never flooded.
- Events never carry the token.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeRequest, binary_response, json_response, make_tools

FILE_ID = "643f81c9-2bc8-44d7-b4a1-994cdb1c503b"
FILE_ID_2 = "aaaaaaaa-2bc8-44d7-b4a1-994cdb1c503b"


class FakeEmitter:
    """Records every event the tool emits (best-effort callable)."""

    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def files_handler():
    def handler(request):
        path = request.url.path
        if path == "/api/v1/chats/":
            return json_response([{"id": "c1", "title": "Chat", "created_at": 1, "updated_at": 2}])
        if path == "/api/v1/files/":
            return json_response({"items": [
                {"id": FILE_ID, "filename": "a.txt", "meta": {"content_type": "text/plain", "size": 4},
                 "created_at": 1, "updated_at": 2},
            ], "total": 1})
        return json_response({"unexpected": path}, status=404)

    return handler


async def test_success_emits_start_and_done_status():
    emitter = FakeEmitter()
    tools = make_tools(files_handler(), base_url="http://webui.example.test")
    out = await tools.get_files(__request__=FakeRequest(), __event_emitter__=emitter)

    statuses = [e for e in emitter.events if e["type"] == "status"]
    assert len(statuses) == 2
    assert statuses[0]["data"]["description"] == "Listing your files…"
    assert statuses[0]["data"]["done"] is False
    assert statuses[1]["data"]["done"] is True
    assert "**Files: 1" in out


async def test_no_emitter_skips_events():
    tools = make_tools(files_handler(), base_url="http://webui.example.test")
    out = await tools.get_files(__request__=FakeRequest())  # no emitter
    assert "**Files: 1" in out  # still works, no events


async def test_verbose_false_suppresses_status_events():
    emitter = FakeEmitter()
    tools = make_tools(files_handler(), base_url="http://webui.example.test")
    tools.valves.verbose = False
    out = await tools.get_files(__request__=FakeRequest(), __event_emitter__=emitter)

    assert [e for e in emitter.events if e["type"] == "status"] == []
    assert "**Files: 1" in out


async def test_user_valve_verbose_overrides_admin():
    emitter = FakeEmitter()
    tools = make_tools(files_handler(), base_url="http://webui.example.test")
    tools.valves.verbose = False
    tools.user_valves.verbose = True  # per-user override

    class FakeUser(dict):
        pass

    user = FakeUser(id="u1", valves=tools.user_valves)  # __user__["valves"]
    await tools.get_files(__request__=FakeRequest(), __user__=user, __event_emitter__=emitter)

    statuses = [e for e in emitter.events if e["type"] == "status"]
    assert len(statuses) == 2


async def test_failure_emits_single_error_event():
    def handler(request):
        return json_response({"unexpected": request.url.path}, status=500)

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://webui.example.test")
    out = await tools.get_files(__request__=FakeRequest(), __event_emitter__=emitter)

    errors = [e for e in emitter.events if e["type"] == "chat:message:error"]
    assert len(errors) == 1
    assert "HTTP 500" in errors[0]["data"]["error"]["content"]
    # error is shown even though the request failed
    assert "Error:" in out


async def test_error_event_shown_even_when_verbose_false():
    def handler(request):
        return json_response({"unexpected": request.url.path}, status=500)

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://webui.example.test")
    tools.valves.verbose = False
    await tools.get_files(__request__=FakeRequest(), __event_emitter__=emitter)

    errors = [e for e in emitter.events if e["type"] == "chat:message:error"]
    assert len(errors) == 1  # errors are never gated by verbose


async def test_batch_delete_failures_emit_single_consolidated_error():
    # Two files: one ok, one fails at the DELETE stage -> ONE error event,
    # never two toasts, even though a failure happened per file.
    file_map = {
        FILE_ID: ("a.pdf", True),
        FILE_ID_2: ("b.pdf", False),  # shared / no write access
    }

    def handler(request):
        path = request.url.path
        fid = path[len("/api/v1/files/"):].split("/")[0]
        if request.method == "GET":
            return json_response({"id": fid, "filename": file_map[fid][0],
                                  "meta": {"content_type": "application/pdf", "size": 10}})
        if request.method == "DELETE":
            if not file_map[fid][1]:
                return json_response({"detail": "nope"}, status=403)
            return json_response({"message": "File deleted successfully"})
        return json_response({"unexpected": path}, status=404)

    emitter = FakeEmitter()
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.delete_files([FILE_ID, FILE_ID_2], __request__=FakeRequest(),
                                   __event_emitter__=emitter)

    errors = [e for e in emitter.events if e["type"] == "chat:message:error"]
    assert len(errors) == 1
    assert "1 of 2 file(s) could not be deleted" in errors[0]["data"]["error"]["content"]

    payload = json.loads(out)
    assert payload["deleted_count"] == 1
    assert payload["failed_count"] == 1


async def test_events_never_contain_the_token():
    secret = "sk-super-secret-token-123"
    emitter = FakeEmitter()

    def handler(request):
        return json_response({"unexpected": request.url.path}, status=500)

    tools = make_tools(handler, base_url="http://webui.example.test")
    await tools.get_files(__request__=FakeRequest(token=secret), __event_emitter__=emitter)

    blob = json.dumps([e for e in emitter.events])
    assert secret not in blob
