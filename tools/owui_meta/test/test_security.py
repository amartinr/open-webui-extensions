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
import re
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


# ── 4. Fail-loud sanitizer (Iteration 9 task 9.4) ────────────────────

def test_fail_loud_logs_dropped_key_name_never_value(caplog):
    # A leaked credential-like field is stripped as before AND the key name
    # appears in the server log (value never).
    tools = owui_meta.Tools()
    payload = {
        "token": "sk-should-not-leak-123",
        "name": "John Doe",
        "nested": {"client_secret": "cs-should-not-leak"},
    }
    with caplog.at_level("WARNING", logger="owui_meta"):
        out = tools._ok(payload, "profile", output_format="json")
    data = json.loads(out)
    assert "token" not in data and "client_secret" not in data["nested"]
    assert "name" in data
    # fail-loud: both dropped keys are named in the log
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "token" in joined and "client_secret" in joined
    # the VALUES are never logged
    assert "sk-should-not-leak-123" not in joined
    assert "cs-should-not-leak" not in joined


def test_fail_loud_is_silent_when_nothing_dropped(caplog):
    tools = owui_meta.Tools()
    with caplog.at_level("WARNING", logger="owui_meta"):
        tools._ok({"name": "ok", "api_keys": True}, "profile", output_format="json")
    assert not caplog.records  # no credential-like key dropped → no warning


# ── 5. Allowlist tripwire (Iteration 9 task 9.4) ──────────────────────

# Secret-bearing route patterns from DESIGN §6.3 — the list of routes that
# must NEVER appear in the allowlist (they return credentials):
#   /api/v1/auths/api_key, /api/v1/tools/id/{id}/valves(+/user),
#   /api/v1/tools/id/{id} (source code), /api/v1/knowledge/external/connections*,
#   any */admin/* config route (LDAP/OAuth secrets).
_SECRET_ROUTE_PATTERNS = (
    re.compile(r"/api/v1/auths/api_key(?:$|/)"),
    re.compile(r"/api/v1/tools/id/"),
    re.compile(r"/api/v1/knowledge/external/connections"),
    re.compile(r"/api/v1/(?:auths|users|config)/admin/"),
    re.compile(r"/admin/config(?:$|/)"),
)


def _is_secret_bearing_route(route: str) -> bool:
    return any(p.search(route) for p in _SECRET_ROUTE_PATTERNS)


def test_secret_route_patterns_positive_and_negative_controls():
    # Positive control: the patterns must match the exact DESIGN §6.3 list.
    for bad in (
        "/api/v1/auths/api_key",
        "/api/v1/auths/api_key/",
        "/api/v1/tools/id/abc123",
        "/api/v1/tools/id/abc123/valves",
        "/api/v1/tools/id/abc123/valves/user",
        "/api/v1/knowledge/external/connections",
        "/api/v1/knowledge/external/connections/1",
        "/api/v1/auths/admin/config",
        "/api/v1/users/admin/config",
    ):
        assert _is_secret_bearing_route(bad), f"pattern missed {bad}"
    # Negative control: every legitimate allowlist shape must NOT match.
    for good in (
        "/api/v1/auths/",
        "/api/models",
        "/api/v1/chats/",
        "/api/v1/chats/{chat_id}",
        "/api/v1/chats/search",
        "/api/v1/chats/tags",
        "/api/v1/chats/stats/usage",
        "/api/v1/files/",
        "/api/v1/files/{file_id}/content",
        "/api/v1/prompts/",
        "/api/v1/tools/",
        "/api/v1/knowledge/",
        "/api/v1/skills/",
        "/api/v1/skills/id/{skill_id}",
        "/api/v1/folders/",
    ):
        assert not _is_secret_bearing_route(good), f"false positive on {good}"


def test_allowlist_route_tripwire_blocks_secret_bearing_routes():
    # Every _ROUTE_* constant in the module must be read-safe. A future
    # developer adding a credential route to the allowlist is blocked at
    # test/review time — "blocked by default" (DESIGN §8.9.4).
    source = Path(owui_meta.__file__).read_text(encoding="utf-8")
    routes = re.findall(r'^_ROUTE_\w+\s*=\s*"([^"]+)"', source, re.MULTILINE)
    assert routes, "no _ROUTE_* constants found — tripwire is inert"
    bad = [r for r in routes if _is_secret_bearing_route(r)]
    assert not bad, f"secret-bearing route(s) in the allowlist: {bad}"


# ── 6. Static tripwire ────────────────────────────────────────────────

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
