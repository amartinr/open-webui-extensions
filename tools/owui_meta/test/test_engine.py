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
    assert '"Abel"' in out


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
    assert '"Abel"' in out


async def test_missing_token_returns_clear_error_without_network_call():
    def handler(request):
        raise AssertionError("no request should be made without a token")

    tools = make_tools(Recorder(handler), base_url="http://open-webui.private")
    out = await tools.get_my_profile(FakeRequest(token=None))
    assert "No authentication token available" in out
    assert "error" in out


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
        return json_response({"blob": "x" * 5000})

    tools = make_tools(handler, base_url="http://open-webui.private")
    tools.valves.max_response_chars = 500
    out = await tools.get_my_profile(FakeRequest())
    assert "truncated" in out
    assert len(out) <= 500 + 5  # marker keeps the total at max_chars


async def test_base_url_from_env_var(monkeypatch):
    def handler(request):
        assert request.url.host == "env.example"
        return json_response({"id": "u1"})

    monkeypatch.setenv("WEBUI_URL", "http://env.example")
    tools = make_tools(handler)
    out = await tools.get_my_profile(FakeRequest())
    assert '"u1"' in out


async def test_base_url_from_valve_when_env_unset(monkeypatch):
    def handler(request):
        assert request.url.host == "localhost"
        return json_response({"id": "u1"})

    monkeypatch.delenv("WEBUI_URL", raising=False)
    tools = make_tools(handler, fallback_base_url="http://localhost:9000")
    out = await tools.get_my_profile(FakeRequest())
    assert '"u1"' in out


async def test_retry_with_fallback_on_transport_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "unreachable.invalid":
            raise httpx.ConnectError("connection refused", request=request)
        return json_response({"ok": True})

    monkeypatch.setenv("WEBUI_URL", "http://unreachable.invalid")
    tools = make_tools(handler, fallback_base_url="http://localhost:8080")
    out = await tools.get_my_profile(FakeRequest())
    assert calls == ["unreachable.invalid", "localhost"]
    assert '"ok"' in out


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
