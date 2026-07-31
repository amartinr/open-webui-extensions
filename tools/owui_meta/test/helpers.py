"""Shared helpers for the owui_meta test suite."""

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
for p in (TOOL_DIR, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import httpx

import owui_meta

try:
    from fastapi.security import HTTPAuthorizationCredentials as _FakeHTTPAuthCred
except Exception:  # fastapi may be absent in bare test environments
    _FakeHTTPAuthCred = None


def bearer_credentials(token: str):
    """Return the exact shape v0.10.2 AuthTokenMiddleware stores in
    ``request.state.token``: an HTTPAuthorizationCredentials object
    (``scheme``/``credentials``). Falls back to an equivalent object when
    fastapi is not installed.
    """
    if _FakeHTTPAuthCred is not None:
        return _FakeHTTPAuthCred(scheme="Bearer", credentials=token)
    from types import SimpleNamespace

    return SimpleNamespace(scheme="Bearer", credentials=token)


class FakeState:
    def __init__(self, token=None):
        self.token = token


class FakeRequest:
    """Stand-in for the Open WebUI Request injected as __request__."""

    def __init__(self, token="test-token-123"):
        self.state = FakeState(token)


def json_response(payload, status=200, content_type="application/json"):
    return httpx.Response(
        status,
        json=payload,
        headers={"content-type": content_type},
    )


def binary_response(content: bytes, content_type: str, status=200):
    return httpx.Response(
        status,
        content=content,
        headers={"content-type": content_type},
    )


class Recorder:
    """Wraps a MockTransport handler and records every request it receives."""

    def __init__(self, handler):
        self.requests = []
        self.handler = handler

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def make_tools(handler, *, fallback_base_url="http://localhost:8080", base_url=None):
    """Build a Tools instance wired to a mocked transport.

    Deterministic base-URL resolution: the real admin config store is never
    touched (``owui_meta.Config = None``). Tests control WEBUI_URL via
    monkeypatch when they need the env-var resolution path.
    """
    tools = owui_meta.Tools()
    tools.valves.fallback_base_url = fallback_base_url
    tools._transport = httpx.MockTransport(handler)
    tools._base_url_override = base_url
    owui_meta.Config = None
    return tools
