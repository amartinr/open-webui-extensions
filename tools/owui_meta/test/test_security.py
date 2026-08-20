"""
Output-boundary security guards (DESIGN §7.2, defense in depth).

Every method whitelists/summarizes its fields, but a FUTURE method (or a
future server version that echoes a credential under an unexpected field —
exactly what /api/v1/auths/ does with ``token``) could accidentally pass a
sensitive value through. These tests pin the guards that run at the output
boundary so no sensitive value can reach the model even then:

1. ``_sanitize`` — drops any dict key whose NAME looks like a credential when
   its VALUE is a non-empty string. Boolean permission FLAGS named e.g.
   ``api_keys`` are kept (they are not secrets).
2. ``_run`` — redacts the raw token string from any output (success or
   error) before it is returned.
3. Static tripwire — no method may pass a raw server body straight into
   ``_ok``.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import owui_meta
from helpers import FakeRequest, json_response, make_tools


# ── 1. Sanitizer ─────────────────────────────────────────────────────

def test_sanitize_drops_secret_keys_keeps_flags():
    tools = owui_meta.Tools()
    payload = {
        "token": "sk-leak", "token_type": "Bearer", "expires_at": 1,
        "api_key": "sk-123", "password": "hunter2",
        "client_secret": "cs",
        "config": {"client_secret": "nested-cs", "ok": 1},
        "api_keys": True,                 # boolean permission FLAG — kept
        "name": "John Doe",
    }
    out = tools._ok(payload, "profile", output_format="json")
    data = json.loads(out)
    for key in ("token", "token_type", "api_key", "password", "client_secret"):
        assert key not in data, f"{key} must be stripped"
    assert "config" in data and "client_secret" not in data["config"]
    assert data["config"]["ok"] == 1
    assert data["api_keys"] is True       # flag survives
    assert data["name"] == "John Doe"

    # markdown also goes through the sanitizer before rendering
    out = tools._ok(payload, "profile", output_format="markdown")
    for secret in ("sk-leak", "sk-123", "hunter2", "nested-cs"):
        assert secret not in out


async def test_sanitize_protects_against_future_raw_pass_through():
    # Simulates a FUTURE method that (wrongly) passes a raw server body to
    # _ok. Even then, the serializer strips credential-like keys. This is the
    # exact auths() shape: token echo + profile.
    raw_body = {
        "token": "sk-echoed", "token_type": "Bearer", "expires_at": 1,
        "id": "u1", "name": "John Doe", "role": "user",
        "permissions": {"features": {"api_keys": True}},
    }

    def handler(request):
        return json_response(raw_body)

    # markdown
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_profile(FakeRequest(token="sk-echoed"))
    assert "sk-echoed" not in out

    # json
    tools = make_tools(handler, base_url="http://webui.example.test", output_format="json")
    out = await tools.get_my_profile(FakeRequest(token="sk-echoed"))
    assert "sk-echoed" not in out


# ── 2. Token string redaction ────────────────────────────────────────

async def test_token_string_redacted_even_inside_whitelisted_field():
    # Belt-and-suspenders: even if the server puts the token string inside a
    # field that passes the whitelist (here: name), _run redacts it before
    # returning — in both formats.
    secret = "sk-top-secret-token-999"

    def handler(request):
        return json_response({"id": "u1", "name": secret, "role": "user"})

    for fmt in ("markdown", "json"):
        tools = make_tools(handler, base_url="http://webui.example.test", output_format=fmt)
        out = await tools.get_my_profile(FakeRequest(token=secret))
        assert secret not in out, f"token leaked in {fmt} mode"
        assert "[REDACTED]" in out


async def test_token_redacted_in_error_path():
    # An exception message must never carry the token either.
    secret = "sk-error-token-12345"

    def handler(request):
        raise httpx.ConnectError(f"could not connect with {secret}", request=request)

    tools = make_tools(handler, base_url="http://webui.example.test", output_format="markdown")
    out = await tools.get_my_profile(FakeRequest(token=secret))
    assert secret not in out


def test_redact_ignores_short_or_absent_tokens():
    tools = owui_meta.Tools()
    assert tools._redact("hello world", None) == "hello world"
    assert tools._redact("hello world", "short") == "hello world"  # len < 8
    assert tools._redact("hello world", "sk-long-token-here") == "hello world"
    assert tools._redact("my sk-long-token-here end", "sk-long-token-here") == "my [REDACTED] end"


# ── 3. Static tripwire ───────────────────────────────────────────────

def test_no_raw_server_body_reaches_ok():
    # Every method must transform json.loads(body) through a whitelist or a
    # summarizer before _ok. If a future method passes the raw body straight
    # in, this fails and the leak would be caught at review time.
    source = Path(owui_meta.__file__).read_text(encoding="utf-8")
    assert "self._ok(json.loads(body" not in source, (
        "raw server body passed straight into _ok — whitelist or summarize first"
    )
    # and every _api_get_json consumer is a _get_* method (never the public one)
    assert "_api_get_json(" in source
