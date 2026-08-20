"""
Engine tests: token extraction/forwarding, Content-Type validation (SPA HTML
trap), HTTP error mapping, truncation, base-URL resolution and the
transport-error fallback retry (DESIGN §4.2, §4.3, §7.2, §8.4).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from helpers import (
    FakeRequest,
    Recorder,
    bearer_credentials,
    json_response,
    make_tools,
)


async def test_profile_token_echo_never_reaches_model():
    # REGRESSION (security): v0.10.2 GET /api/v1/auths/ (get_session_user)
    # ECHOES the request token back in the body (token/token_type/expires_at)
    # to support the frontend session refresh. The tool must strip it before
    # serialization — in BOTH output formats. json mode used to dump the raw
    # body, leaking the user's session credential into the model context.
    secret = "sk-user-secret-token"
    body = {
        "token": secret, "token_type": "Bearer", "expires_at": 9999999999,
        "id": "u1", "email": "a@b.c", "name": "Abel", "role": "user",
        "permissions": {"chat": {"controls": True}},
    }

    def handler(request):
        return json_response(body)

    # markdown mode
    tools = make_tools(handler, base_url="http://open-webui.private", output_format="markdown")
    out = await tools.get_my_profile(FakeRequest(token=secret))
    assert secret not in out
    assert "expires_at" not in out

    # json mode (the raw body would previously be dumped verbatim)
    import json

    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    out = await tools.get_my_profile(FakeRequest(token=secret))
    assert secret not in out
    payload = json.loads(out)
    assert payload["name"] == "Abel"
    assert "token" not in payload
    assert "token_type" not in payload
    assert "expires_at" not in payload


async def test_token_from_http_authorization_credentials_object():
    # Regression: v0.10.2 AuthTokenMiddleware stores an
    # HTTPAuthorizationCredentials OBJECT in request.state.token, not a
    # string. This is what a real authenticated session produces.
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return json_response({"id": "u1", "name": "Abel"})

    tools = make_tools(Recorder(handler), base_url="http://open-webui.private")
    request = FakeRequest(token=bearer_credentials("sk-real-session-token"))
    out = await tools.get_my_profile(request)
    assert seen["auth"] == "Bearer sk-real-session-token"
    assert "Abel" in out


async def test_token_forwarded_as_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        return json_response({"id": "u1", "name": "Abel"})

    tools = make_tools(Recorder(handler), base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest(token="sk-test-abc"))
    assert seen["auth"] == "Bearer sk-test-abc"
    assert "application/json" in (seen["accept"] or "")
    assert "Abel" in out


async def test_missing_token_returns_clear_error_without_network_call():
    def handler(request):
        raise AssertionError("no request should be made without a token")

    tools = make_tools(Recorder(handler), base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest(token=None))
    assert "No authentication token available" in out
    assert "error" in out.lower()


async def test_token_never_appears_in_output():
    def handler(request):
        return json_response({"id": "u1", "blob": "x" * 3000})

    tools = make_tools(handler, base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest(token="sk-super-secret-999"))
    assert "sk-super-secret-999" not in out


async def test_spa_html_trap_not_trusted():
    def handler(request):
        return httpx.Response(
            200,
            text="<!DOCTYPE html><html><body>SPA shell</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    tools = make_tools(handler, base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest())
    assert "Expected JSON" in out
    assert "SPA shell" not in out


@pytest.mark.parametrize("status,expected", [
    (401, "Not authenticated"),
    (403, "Forbidden"),
    (404, "does not exist"),
    (500, "HTTP 500"),
    (503, "HTTP 503"),
])
async def test_http_error_mapping(status, expected):
    def handler(request):
        return json_response({"detail": "x"}, status=status)

    tools = make_tools(handler, base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest())
    assert expected in out


async def test_truncation_applies_to_output():
    def handler(request):
        # profile body with a large whitelisted field (permissions)
        return json_response({
            "id": "u1", "name": "Abel", "role": "user",
            "permissions": {"chat": {"controls": True, "blob": "x" * 5000}},
        })

    tools = make_tools(handler, base_url="http://open-webui.private", output_format="json")
    tools.valves.max_response_chars = 500
    out = await tools.get_my_profile(FakeRequest())
    assert "truncated" in out
    assert len(out) <= 500 + 5  # marker keeps the total at max_chars


async def test_truncation_applies_to_markdown():
    # A chat snippet with long messages is rendered as Markdown; truncation
    # still applies.
    long_chat = {
        "id": "c1", "title": "Big", "folder_id": None, "meta": {},
        "pinned": False, "archived": False, "created_at": 1, "updated_at": 2,
        "chat": {"models": [], "history": {"currentId": "m10", "messages": {
            f"m{i}": {"id": f"m{i}", "role": "user", "content": "y" * 3000,
                       "parentId": None if i == 1 else f"m{i-1}", "timestamp": i}
            for i in range(1, 11)
        }}},
    }

    def handler(request):
        return json_response(long_chat)

    tools = make_tools(handler, base_url="http://open-webui.private")
    tools.valves.max_response_chars = 500
    out = await tools.get_chat("c1", head=10, tail=0, __request__=FakeRequest())
    assert "truncated" in out
    assert len(out) <= 500 + 5


async def test_base_url_from_env_var(monkeypatch):
    def handler(request):
        assert request.url.host == "env.example"
        return json_response({"id": "u1"})

    monkeypatch.setenv("WEBUI_URL", "http://env.example")
    tools = make_tools(handler)
    out = await tools.get_my_profile(FakeRequest())
    assert "u1" in out


class FakeConfig:
    """Stand-in for open_webui.models.config.Config with the v0.10.2 async API."""

    value = "http://config.example"

    @staticmethod
    async def get(key, default=None):
        return FakeConfig.value


async def test_base_url_from_admin_config(monkeypatch):
    # Regression: v0.10.2 Config.get is async and must be awaited; the admin
    # config store (webui.url) is the canonical base URL source (§4.2).
    def handler(request):
        assert request.url.host == "config.example"
        return json_response({"id": "u1"})

    monkeypatch.delenv("WEBUI_URL", raising=False)
    tools = make_tools(handler)  # make_tools resets owui_meta.Config to None
    import owui_meta

    owui_meta.Config = FakeConfig
    out = await tools.get_my_profile(FakeRequest())
    assert "u1" in out


async def test_base_url_from_admin_config_ignores_non_string(monkeypatch):
    def handler(request):
        # Config.get returned a non-string (e.g. dict); the tool must not
        # crash and must fall through to the valve.
        assert request.url.host == "localhost"
        return json_response({"id": "u1"})

    monkeypatch.delenv("WEBUI_URL", raising=False)
    tools = make_tools(handler, fallback_base_url="http://localhost:9000")
    import owui_meta

    owui_meta.Config = FakeConfig
    FakeConfig.value = {"nested": True}
    out = await tools.get_my_profile(FakeRequest())
    assert "u1" in out


async def test_base_url_from_admin_config_errors_fall_through(monkeypatch):
    def handler(request):
        assert request.url.host == "localhost"
        return json_response({"id": "u1"})

    class BrokenConfig:
        @staticmethod
        async def get(key, default=None):
            raise RuntimeError("db down")

    monkeypatch.delenv("WEBUI_URL", raising=False)
    tools = make_tools(handler, fallback_base_url="http://localhost:9000")
    import owui_meta

    owui_meta.Config = BrokenConfig
    out = await tools.get_my_profile(FakeRequest())
    assert "u1" in out


async def test_base_url_from_valve_when_env_unset(monkeypatch):
    def handler(request):
        assert request.url.host == "localhost"
        return json_response({"id": "u1"})

    monkeypatch.delenv("WEBUI_URL", raising=False)
    tools = make_tools(handler, fallback_base_url="http://localhost:9000")
    out = await tools.get_my_profile(FakeRequest())
    assert "u1" in out


async def test_retry_with_fallback_on_transport_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "unreachable.invalid":
            raise httpx.ConnectError("connection refused", request=request)
        return json_response({"id": "u1", "name": "Abel"})

    monkeypatch.setenv("WEBUI_URL", "http://unreachable.invalid")
    tools = make_tools(handler, fallback_base_url="http://localhost:8080", output_format="json")
    out = await tools.get_my_profile(FakeRequest())
    assert calls == ["unreachable.invalid", "localhost"]
    assert "Abel" in out


async def test_no_retry_when_fallback_equals_primary():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        raise httpx.ConnectError("connection refused", request=request)

    # base == fallback (valve used directly), so no second attempt.
    tools = make_tools(handler, fallback_base_url="http://localhost:8080")
    out = await tools.get_my_profile(FakeRequest())
    assert len(calls) == 1
    assert "Could not reach the internal API" in out


async def test_no_retry_on_http_error():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return json_response({"detail": "nope"}, status=500)

    tools = make_tools(handler, base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest())
    assert len(calls) == 1
    assert "HTTP 500" in out


async def test_unexpected_exception_becomes_safe_error():
    def handler(request):
        raise RuntimeError("boom")

    tools = make_tools(handler, base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest())
    assert "Unexpected internal error" in out
    assert "boom" not in out
