"""
title: Open WebUI Meta-Tool
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Queries Open WebUI's own internal API to answer questions about the requesting user's data (chats, files, prompts, tools, models, knowledge), plus explicit user-authorized file deletion for cleanup. Authenticates automatically with the requesting user's token — no credentials to configure. Allowlisted endpoints only.
required_open_webui_version: 0.9.0
requirements: httpx
version: 0.9.0
licence: MIT
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Defensive import: ``open_webui`` is only importable inside the Open WebUI
# backend process. Outside of it (unit tests, CLI import checks) Config stays
# None and the tool falls back to the WEBUI_URL env var and the
# fallback_base_url valve.
try:
    from open_webui.models.config import Config
except Exception:  # pragma: no cover - environment-dependent
    Config = None

DEFAULT_FALLBACK_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RESPONSE_CHARS = 8000

# Length of the text snippet returned to the model by get_file_content.
# The full file is attached to the conversation via the ``files`` event, so
# the snippet is only for the model to recognize what the file is about —
# never a full dump (DESIGN §8.6.5, PLAN.md §7 2026-08-03).
FILE_SNIPPET_CHARS = 100

# Hard cap on how many files one delete_files() call may delete in a single
# pass. Prevents a runaway tool call from wiping a large library at once.
MAX_DELETE_FILES = 50

# Transparent page iteration: the API caps at 50 items/page and exposes
# ``total`` (DESIGN §8.6). ``MAX_PAGES`` bounds how many pages the tool will
# fetch for filtering/sorting so a huge dataset cannot cost an unbounded
# number of internal calls.
MAX_PAGES = 5
DEFAULT_PAGE_SIZE = 50

# ── Canonical route paths (allowlist) ─────────────────────────────────
# Trailing slashes are SIGNIFICANT in this deployment (v0.10.2):
#   - Listings (auths, chats, files, prompts, tools, knowledge, skills,
#     users) are registered with a trailing slash:  GET /api/v1/<resource>/
#     -> WITHOUT the slash they fall into the SPA HTML catch-all (HTTP 200).
#   - Sub-resources (search, pinned, shared, id/{id}, {id}, …) and
#     /api/models are registered WITHOUT a trailing slash.
# Verified live against the instance (2026-08-01): see PLAN.md §iteration-1.
# Skills verified 2026-08-01: /api/v1/skills/ -> 401 JSON (real route),
# /api/v1/skills -> 200 text/html (SPA catch-all).
_ROUTE_PROFILE = "/api/v1/auths/"
_ROUTE_MODELS = "/api/models"
_ROUTE_CHATS = "/api/v1/chats/"
_ROUTE_CHAT = "/api/v1/chats/{chat_id}"
_ROUTE_CHATS_SEARCH = "/api/v1/chats/search"
_ROUTE_CHATS_SHARED = "/api/v1/chats/shared"
_ROUTE_CHATS_PINNED = "/api/v1/chats/pinned"
_ROUTE_FILES = "/api/v1/files/"
_ROUTE_FILE = "/api/v1/files/{file_id}"
_ROUTE_FILE_CONTENT = "/api/v1/files/{file_id}/content"
_ROUTE_PROMPTS = "/api/v1/prompts/"
_ROUTE_TOOLS = "/api/v1/tools/"
_ROUTE_KNOWLEDGE = "/api/v1/knowledge/"
_ROUTE_SKILLS = "/api/v1/skills/"
_ROUTE_SKILL = "/api/v1/skills/id/{skill_id}"

# Content types that are useful as text when reading a file's content.
_TEXT_CONTENT_TYPES = frozenset({
    "application/json", "application/xml", "application/x-yaml",
    "application/javascript", "application/x-sh", "application/csv",
    "application/sql", "application/x-httpd-php",
})

# IDs only: letters, digits, dash, underscore. No slashes or whitespace, so
# a parameter can never smuggle a path segment or a new route.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ToolError(Exception):
    """A user-facing error whose message is safe to return to the model."""


class Tools:
    """Read-only queries against the Open WebUI internal API.

    Authenticates with the token of the requesting user, read from
    ``__request__.state.token`` (extracted by ``AuthTokenMiddleware`` from the
    Bearer header, session cookie or API key). No credentials are configured
    or stored anywhere. All routes come from an internal allowlist and every
    method takes typed parameters only — there is no URL-taking parameter, so
    there is no SSRF surface.
    """

    class Valves(BaseModel):
        """Server-side configuration (admin-set).

        Deliberately contains **no credential valves**: authentication is
        automatic via ``request.state.token``.
        """

        fallback_base_url: str = Field(
            DEFAULT_FALLBACK_BASE_URL,
            description=(
                "Last-resort base URL for the internal API. Used only when the "
                "global admin config (webui.url) and the WEBUI_URL env var are "
                "empty, or when that URL is unreachable (DNS/connection/timeout)."
            ),
        )
        timeout: int = Field(
            DEFAULT_TIMEOUT,
            description="HTTP timeout in seconds for internal API calls.",
            ge=1,
            le=120,
        )
        max_response_chars: int = Field(
            DEFAULT_MAX_RESPONSE_CHARS,
            description=(
                "Maximum characters of a response returned to the model "
                "(truncated with a marker)."
            ),
            ge=500,
            le=100_000,
        )
        output_format: Literal["markdown", "json"] = Field(
            "markdown",
            description=(
                "Format of the response returned to the model: 'markdown' (default, "
                "tables/bullets — easier for models to read) or 'json' (structured "
                "objects)."
            ),
        )
        verbose: bool = Field(
            True,
            description=(
                "Emit progress status events in the UI while the tool runs "
                "(e.g. \"Querying your chats…\"). Errors are always shown, "
                "regardless of this setting."
            ),
        )

    class UserValves(BaseModel):
        """Per-user overrides, configurable from the chat session.

        ``output_format`` lets each user choose the response format they
        prefer for their own chats (there is no universal winner — it depends
        on the model and task). Defaults to ``markdown`` (the tool's default).
        """

        output_format: Literal["markdown", "json"] = Field(
            "markdown",
            description=(
                "Response format for this user: 'markdown' (default, tables/bullets) "
                "or 'json' (structured objects)."
            ),
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "markdown", "label": "Markdown"},
                        {"value": "json", "label": "JSON"},
                    ],
                }
            },
        )
        verbose: bool = Field(
            True,
            description=(
                "Show progress status events for this user while the tool runs."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        self.user_valves = self.UserValves()
        # Test seams — never set in production.
        self._transport: Optional[httpx.AsyncBaseTransport] = None
        self._base_url_override: Optional[str] = None

    @staticmethod
    def _get_user_valves(__user__: Optional[Any]) -> Optional[Any]:
        """Extract the UserValves object from the __user__ dict if available."""
        if __user__ is None:
            return None
        try:
            if isinstance(__user__, dict):
                return __user__.get("valves")
        except Exception:
            pass
        return None

    def _resolve_output_format(self, __user__: Optional[Any]) -> str:
        """Effective output format: user's choice (if provided) else admin valve."""
        uv = self._get_user_valves(__user__)
        if uv is not None and uv.output_format:
            return uv.output_format
        return self.valves.output_format

    def _resolve_verbose(self, __user__: Optional[Any]) -> bool:
        """Effective status-event verbosity: user's choice (if provided) else
        the admin valve. Errors are shown regardless (see ``_run``)."""
        uv = self._get_user_valves(__user__)
        if uv is not None and uv.verbose is not None:
            return bool(uv.verbose)
        return bool(self.valves.verbose)

    # ──────────────────────────────────────────────
    #  Base URL resolution (DESIGN §4.2)
    # ──────────────────────────────────────────────

    async def _resolve_base_url(self) -> str:
        """Resolve the API base URL: webui.url → WEBUI_URL → valve.

        ``Config.get`` is async in Open WebUI (``open_webui/models/config.py``
        v0.10.2: ``async def get(key, default=None)``), so this coroutine
        awaits it. Any failure of the admin config store falls through to the
        env var and the valve.
        """
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        configured = ""
        if Config is not None:
            try:
                value = await Config.get("webui.url")
                if isinstance(value, str):
                    configured = value.strip()
            except Exception:
                configured = ""
        if not configured:
            configured = os.getenv("WEBUI_URL", "").strip()
        if not configured:
            configured = self.valves.fallback_base_url
        return configured.rstrip("/")

    # ──────────────────────────────────────────────
    #  Token extraction (DESIGN §3.1)
    # ──────────────────────────────────────────────

    def _require_token(self, request: Any) -> str:
        """Extract the requesting user's token from ``request.state.token``.

        In Open WebUI v0.10.2 ``AuthTokenMiddleware`` stores an
        ``HTTPAuthorizationCredentials`` object (``.scheme``/``.credentials``)
        in ``request.state.token`` — not a plain string (verified against
        ``backend/open_webui/utils/asgi_middleware.py`` @ v0.10.2). A plain
        string is also accepted for robustness across versions.

        The token is never logged and never included in any output.
        """
        token = self._extract_token_quiet(request)
        if token is None:
            raise ToolError(
                "No authentication token available. This tool must run inside an "
                "authenticated Open WebUI session or API request (it reads "
                "__request__.state.token)."
            )
        return token

    @staticmethod
    def _extract_token_quiet(request: Any) -> Optional[str]:
        """Tolerant token read used for output redaction (never raises).

        Same extraction as ``_require_token`` but returns ``None`` instead of
        raising, so the output boundary can redact the token string without
        disturbing the normal auth flow.
        """
        try:
            state = getattr(request, "state", None) if request is not None else None
            token = getattr(state, "token", None) if state is not None else None
            if token is None:
                return None
            if isinstance(token, str):
                credentials = token
            else:
                credentials = getattr(token, "credentials", None)
            if isinstance(credentials, str) and credentials.strip():
                return credentials.strip()
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────
    #  HTTP engine (DESIGN §8.4, §7.2)
    # ──────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self.valves.timeout),
            follow_redirects=False,
            trust_env=False,
        )

    async def _fetch(self, url: str, token: str, params: Optional[dict] = None,
                     accept: str = "application/json",
                     method: str = "GET") -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "owui_meta/0.1.0 (Open WebUI internal tool)",
        }
        async with self._client() as client:
            return await client.request(method, url, headers=headers, params=params)

    async def _fetch_with_retry(self, token: str, path: str,
                                params: Optional[dict] = None,
                                accept: str = "application/json",
                                method: str = "GET") -> httpx.Response:
        """Call an allowlisted route, retrying once against the fallback URL.

        Retry only on transport errors (DNS/connection/timeout — DESIGN §4.3).
        Never retries on API 4xx/5xx responses.
        """
        base = await self._resolve_base_url()
        primary_url = base + path
        try:
            resp = await self._fetch(primary_url, token, params, accept, method)
        except httpx.RequestError as exc:
            fallback = self.valves.fallback_base_url.rstrip("/")
            if fallback and fallback != base:
                try:
                    resp = await self._fetch(fallback + path, token, params, accept, method)
                except httpx.RequestError as exc2:
                    raise ToolError(
                        f"Could not reach the internal API at {fallback + path}: {exc2}"
                    ) from exc2
            else:
                raise ToolError(
                    f"Could not reach the internal API at {primary_url}: {exc}"
                ) from exc
        return resp

    def _validate_status(self, resp: httpx.Response) -> tuple[int, str]:
        """Map HTTP errors to readable, non-leaking messages (DESIGN §7.2)."""
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code >= 500:
            raise ToolError(f"The internal API returned an error (HTTP {resp.status_code}).")
        if resp.status_code == 404:
            raise ToolError("Not found: the resource does not exist or does not belong to you.")
        if resp.status_code == 403:
            raise ToolError("Forbidden: you do not have permission to access this resource.")
        if resp.status_code == 401:
            raise ToolError(
                "Not authenticated: the token was rejected or invalid, or the resource "
                "does not exist (Open WebUI returns 401 for missing resources)."
            )
        if resp.status_code >= 400:
            raise ToolError(f"The internal API returned an error (HTTP {resp.status_code}).")
        return resp.status_code, content_type

    async def _api_get_json(self, token: str, path: str,
                            params: Optional[dict] = None,
                            allow_ndjson: bool = False) -> tuple[int, str, str]:
        """JSON GET: validates Content-Type (SPA HTML catch-all returns 200).

        ``allow_ndjson`` permits ``application/x-ndjson`` (one JSON object per
        line, e.g. ``/api/v1/chats/all``) when the consumer handles it.
        """
        resp = await self._fetch_with_retry(token, path, params)
        status, content_type = self._validate_status(resp)
        allowed = {"application/json"}
        if allow_ndjson:
            allowed.add("application/x-ndjson")
        if content_type not in allowed:
            raise ToolError(
                f"Expected JSON from the internal API but got "
                f"'{content_type or 'no content type'}' (HTTP {status}) — the route "
                "may not exist or may have changed."
            )
        return status, content_type, resp.text

    async def _api_get_raw(self, token: str, path: str) -> tuple[int, str, bytes]:
        """Raw GET (binary allowed) for file content."""
        resp = await self._fetch_with_retry(token, path, accept="*/*")
        status, content_type = self._validate_status(resp)
        return status, content_type, resp.content

    async def _api_delete_json(self, token: str, path: str) -> tuple[int, str, str]:
        """JSON DELETE for explicit, user-authorized deletions (e.g. files).

        Same content-type validation as GET (the SPA HTML catch-all returns
        200), and the same non-leaking error mapping from ``_validate_status``.
        """
        resp = await self._fetch_with_retry(token, path, method="DELETE")
        status, content_type = self._validate_status(resp)
        allowed = {"application/json"}
        if content_type not in allowed:
            raise ToolError(
                f"Expected JSON from the internal API but got "
                f"'{content_type or 'no content type'}' (HTTP {status}) — the route "
                "may not exist or may have changed."
            )
        return status, content_type, resp.text

    # ──────────────────────────────────────────────
    #  Output formatting (DESIGN §8.6.5, §7.2)
    # ──────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        max_chars = self.valves.max_response_chars
        if len(text) <= max_chars:
            return text
        note = f"\n… [truncated from {len(text)} to {max_chars} characters]"
        return text[: max(0, max_chars - len(note))] + note

    def _ok(self, payload: Any, kind: str = "generic",
            output_format: Optional[str] = None) -> str:
        """Serialize a successful result in the configured output format.

        ``kind`` selects the Markdown renderer (tables for lists, bullets for
        details, fenced blocks for content). In ``json`` mode every kind is
        the same structured dump. ``output_format`` defaults to the admin
        valve; callers pass the per-user effective format when available.
        """
        fmt = output_format or self.valves.output_format
        payload = self._sanitize(payload)  # output-boundary guard (DESIGN §7.2)
        if fmt == "json":
            return self._truncate(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            )
        return self._truncate(self._render(kind, payload))

    def _error(self, message: str, output_format: Optional[str] = None) -> str:
        """Serialize an error: plain-text one-liner in markdown, JSON object in json."""
        fmt = output_format or self.valves.output_format
        if fmt == "json":
            return json.dumps({"error": message}, ensure_ascii=False, indent=2)
        return f"Error: {message}"

    # ──────────────────────────────────────────────
    #  Output-boundary guards (DESIGN §7.2, defense in depth)
    #
    #  Every method whitelists/summarizes its fields, but a FUTURE method (or
    #  a future server version that echoes a credential under a field we did
    #  not expect — exactly what /api/v1/auths/ does with ``token``) could
    #  accidentally pass a sensitive value through. These guards run at the
    #  output boundary so no sensitive value can reach the model even then:
    #
    #   1. _sanitize   — drops any dict key whose NAME looks like a credential
    #                    when its VALUE is a non-empty string. Boolean
    #                    permission FLAGS named e.g. ``api_keys`` are kept.
    #   2. _run        — redacts the raw token string from any output
    #                    (success or error) before it is returned.
    #  A static tripwire test (test_security.py) pins that no method passes a
    #  raw server body straight into _ok.
    # ──────────────────────────────────────────────

    _SENSITIVE_KEY_RE = re.compile(
        r"(token|api[_-]?key|apikey|password|passwd|secret|credential|"
        r"authorization|private[_-]?key|access[_-]?key|client[_-]?secret|"
        r"connection[_-]?string|x[_-]?api[_-]?key)",
        re.IGNORECASE,
    )

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        """Recursively drop credential-looking keys (string values only)."""
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                if cls._SENSITIVE_KEY_RE.search(str(key)) and isinstance(val, str) and val.strip():
                    continue  # credential-like key with a non-empty string value
                out[key] = cls._sanitize(val)
            return out
        if isinstance(value, list):
            return [cls._sanitize(v) for v in value]
        return value

    @staticmethod
    def _redact(text: str, secret: Optional[str]) -> str:
        """Replace the request token string if it ever appears in output."""
        if not secret or len(secret) < 8 or secret not in text:
            return text
        return text.replace(secret, "[REDACTED]")

    # ──────────────────────────────────────────────
    #  Markdown renderers (DESIGN §8.8)
    # ──────────────────────────────────────────────

    @staticmethod
    def _esc_md(value: Any) -> str:
        """Escape a cell for a Markdown table (pipe + newlines)."""
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    @staticmethod
    def _fmt_ts(epoch: Any) -> str:
        """Render an epoch timestamp as a readable UTC date/time."""
        if isinstance(epoch, (int, float)):
            return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        return "—"

    @staticmethod
    def _summary_header(label: str, count: int, total: Any = None) -> str:
        """Header line: count, plus total when the server reports more."""
        if total is not None and total != count:
            return f"**{label}: {count} ({total} total on server)**"
        return f"**{label}: {count}**"

    def _md_table(self, headers: list[str], rows: list[list[Any]]) -> str:
        if not rows:
            return "(none)"
        lines = ["| " + " | ".join(headers) + " |"]
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            lines.append("| " + " | ".join(self._esc_md(c) for c in row) + " |")
        return "\n".join(lines)

    # ──────────────────────────────────────────────
    #  Hierarchical JSON → Markdown (research-based, DESIGN §8.8)
    #
    #  Strategy per llm-md / research (1000+ cases):
    #    - shallow objects → key-value bullet list (bold keys)
    #    - uniform arrays of objects → Markdown table
    #    - deep nesting (depth > 3) → indented hierarchy / YAML-ish bullets
    #    - never embed raw JSON in a bullet (hurts comprehension)
    #  Keys are humanized (snake_case → Title Case); scalar values stay raw
    #  (true/false/null, numbers without unit prefixes).
    # ──────────────────────────────────────────────

    @staticmethod
    def _humanize_key(key: Any) -> str:
        s = str(key).strip()
        if not s:
            return ""
        words = s.replace("_", " ").replace("-", " ").split()
        return " ".join(w[:1].upper() + w[1:] if w else w for w in words)

    @staticmethod
    def _md_scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _md_uniform_keys(self, items: list) -> Optional[list[str]]:
        """Return the shared keys when >80% of the dicts share them."""
        if len(items) < 2:
            return None
        from collections import Counter

        key_sets = [tuple(sorted(d.keys())) for d in items if isinstance(d, dict)]
        if not key_sets or len(key_sets) < 2:
            return None
        common = Counter(key_sets).most_common(1)[0]
        if common[1] / len(key_sets) >= 0.8:
            return list(common[0])
        return None

    def _md_hierarchy(self, value: Any, depth: int = 0) -> str:
        """Render arbitrary JSON as hierarchical Markdown bullets."""
        pad = "  " * depth
        if isinstance(value, dict):
            if not value:
                return f"{pad}- (empty)"
            lines = []
            for key, val in value.items():
                label = self._humanize_key(key)
                if isinstance(val, dict) and val:
                    lines.append(f"{pad}- {label}")
                    lines.append(self._md_hierarchy(val, depth + 1))
                elif isinstance(val, list):
                    if val and self._md_uniform_keys(val):
                        keys = self._md_uniform_keys(val)
                        rows = [[item.get(k) for k in keys] for item in val]
                        table = self._md_table([self._humanize_key(k) for k in keys], rows)
                        for tl in table.split("\n"):
                            lines.append(pad + "  " + tl)
                    else:
                        lines.append(
                            f"{pad}- {label}: " + ", ".join(self._md_scalar(x) for x in val)
                        )
                else:
                    lines.append(f"{pad}- {label}: {self._md_scalar(val)}")
            return "\n".join(lines)
        if isinstance(value, list):
            if not value:
                return f"{pad}- (empty)"
            if self._md_uniform_keys(value):
                keys = self._md_uniform_keys(value)
                rows = [[item.get(k) for k in keys] for item in value]
                return self._md_table([self._humanize_key(k) for k in keys], rows)
            lines = []
            for i, item in enumerate(value, 1):
                if isinstance(item, dict) and item:
                    inner = self._md_hierarchy(item, depth)
                    inner_lines = inner.split("\n")
                    head = inner_lines[0]
                    prefix = f"{pad}{i}. "
                    if head.startswith(pad + "- "):
                        head = prefix + head[len(pad + "- "):]
                    elif head.startswith(pad):
                        head = prefix + head[len(pad):]
                    else:
                        head = prefix + head
                    out_lines = [head]
                    for line in inner_lines[1:]:
                        out_lines.append("  " + line if line.strip() else line)
                    lines.append("\n".join(out_lines))
                else:
                    lines.append(f"{pad}{i}. {self._md_scalar(item)}")
            return "\n".join(lines)
        return f"{pad}- {self._md_scalar(value)}"

    def _render(self, kind: str, payload: Any) -> str:
        if kind == "profile":
            return self._render_profile(payload)
        if kind == "models":
            return self._render_models(payload)
        if kind == "chats":
            return self._render_chats(payload)
        if kind == "chat":
            return self._render_chat(payload)
        if kind == "files":
            return self._render_files(payload)
        if kind == "files_deleted":
            return self._render_files_deleted(payload)
        if kind == "file_text":
            return self._render_file_text(payload)
        if kind == "file_binary":
            return self._render_file_binary(payload)
        if kind == "prompts":
            return self._render_prompts(payload)
        if kind == "tools":
            return self._render_tools(payload)
        if kind == "knowledge":
            return self._render_knowledge(payload)
        if kind == "skills":
            return self._render_skills(payload)
        if kind == "skill":
            return self._render_skill(payload)
        # Unknown kind: structured fallback (defensive).
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    def _render_profile(self, p: dict) -> str:
        lines = ["**Profile**", ""]
        for key, label in (("name", "Name"), ("email", "Email"), ("role", "Role"), ("id", "ID")):
            value = p.get(key)
            if value is not None:
                lines.append(f"- {label}: {value}")
        perms = p.get("permissions")
        if isinstance(perms, dict) and perms:
            lines.append("")
            lines.append("**Permissions**")
            lines.append(self._md_hierarchy(perms))
        return "\n".join(lines)

    def _render_models(self, p: dict) -> str:
        items = p.get("models", [])
        head = self._summary_header("Models", p.get("count", len(items)))
        table = self._md_table(
            ["ID", "Name", "Owned by"],
            [[m.get("id"), m.get("name"), m.get("owned_by")] for m in items],
        )
        return head + "\n\n" + table

    def _render_chats(self, p: dict) -> str:
        items = p.get("chats", [])
        count = p.get("count", len(items))
        query = p.get("query")
        head = (
            f"**Search results for '{query}': {count}**"
            if query
            else self._summary_header("Chats", count, p.get("total"))
        )
        table = self._md_table(
            ["Title", "Updated", "ID"],
            [[m.get("title"), self._fmt_ts(m.get("updated_at")), m.get("id")] for m in items],
        )
        return head + "\n\n" + table

    def _render_chat(self, p: dict) -> str:
        lines = [
            f"**Chat: {p.get('title', '(untitled)')}** (id: {p.get('id', '')})",
            "",
        ]
        for message in p.get("messages", []):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if content is None:
                continue
            role = message.get("role", "message")
            lines.append(f"**{role}**")
            if isinstance(content, str):
                lines.append(content)
            else:
                lines.append(self._md_hierarchy(content))
            lines.append("")
        return "\n".join(lines).rstrip()

    def _render_files(self, p: dict) -> str:
        items = p.get("files", [])
        count = p.get("count", len(items))
        matched = p.get("matched", count)
        total = p.get("total")
        if total is not None and matched != total:
            head = f"**Files: {matched} matched ({total} total on server)**"
        else:
            head = self._summary_header("Files", matched, total)
        if count < matched:
            head += f" (showing top {count})"
        table = self._md_table(
            ["Filename", "Type", "Size (bytes)", "Created", "Origin chat", "ID"],
            [
                [
                    f.get("filename"),
                    f.get("content_type"),
                    f.get("size"),
                    self._fmt_ts(f.get("created_at")),
                    f.get("origin_chat_id") or "—",
                    f.get("id"),
                ]
                for f in items
            ],
        )
        return head + "\n\n" + table

    _LANG_HINTS = {
        "application/json": "json",
        "application/xml": "xml",
        "application/x-yaml": "yaml",
        "application/javascript": "javascript",
        "application/x-sh": "sh",
        "application/csv": "csv",
        "application/sql": "sql",
        "application/x-httpd-php": "php",
        "text/plain": "text",
        "text/csv": "csv",
        "text/markdown": "markdown",
        "text/html": "html",
        "text/yaml": "yaml",
        "text/xml": "xml",
        "text/css": "css",
        "text/javascript": "javascript",
    }

    def _lang_hint(self, ct: str) -> str:
        if ct in self._LANG_HINTS:
            return self._LANG_HINTS[ct]
        if ct.startswith("text/"):
            sub = ct.split("/", 1)[1].strip()
            if re.fullmatch(r"[A-Za-z0-9_+.-]+", sub):
                return sub
        return "text"

    def _render_file_text(self, p: dict) -> str:
        ct = p.get("content_type", "text")
        lang = self._lang_hint(ct)
        ident = p.get("filename") or p.get("file_id")
        head = f"**File: {ident}** ({ct}, {p.get('size')} bytes)"
        if p.get("filename"):
            head += f" (id: {p.get('file_id')})"
        lines = [head, "", f"```{lang}\n{p.get('content', '')}\n```"]
        if p.get("truncated"):
            total = p.get("total_chars")
            note = p.get("note")
            if total and note:
                note = f"{note} ({total} chars total)"
            elif total:
                note = f"File truncated to the first {FILE_SNIPPET_CHARS} characters ({total} chars total)."
            else:
                note = f"File truncated to the first {FILE_SNIPPET_CHARS} characters."
            lines += ["", note]
        return "\n".join(lines)

    def _render_file_binary(self, p: dict) -> str:
        ident = p.get("filename") or p.get("file_id")
        head = f"**File: {ident}** ({p.get('content_type')}, {p.get('size')} bytes)"
        if p.get("filename"):
            head += f" (id: {p.get('file_id')})"
        return head + "\n\n" + p.get("note", "Binary content not returned inline.")

    def _render_files_deleted(self, p: dict) -> str:
        """Render the batch deletion summary: per-file rows + counts."""
        deleted = p.get("deleted", [])
        failed = p.get("failed", [])
        lines = [
            f"**Deleted {p.get('deleted_count', len(deleted))} of "
            f"{p.get('requested', len(deleted) + len(failed))} files**",
            "",
        ]
        rows = []
        for d in deleted:
            ident = d.get("filename") or d.get("file_id")
            rows.append(["deleted", ident, d.get("content_type") or "", d.get("file_id")])
        for f in failed:
            rows.append(["failed", f.get("file_id"), f.get("error", ""), ""])
        if rows:
            lines.append(self._md_table(
                ["Status", "File", "Type / error", "ID"],
                rows,
            ))
        return "\n".join(lines)

    def _render_prompts(self, p: dict) -> str:
        items = p.get("prompts", [])
        head = self._summary_header("Prompts", p.get("count", len(items)))
        table = self._md_table(
            ["Command", "Name", "ID"],
            [[f.get("command"), f.get("name"), f.get("id")] for f in items],
        )
        return head + "\n\n" + table

    def _render_tools(self, p: dict) -> str:
        items = p.get("tools", [])
        head = self._summary_header("Tools", p.get("count", len(items)))
        table = self._md_table(
            ["Name", "Description", "ID"],
            [[t.get("name"), t.get("description"), t.get("id")] for t in items],
        )
        return head + "\n\n" + table

    def _render_knowledge(self, p: dict) -> str:
        items = p.get("knowledge", [])
        head = self._summary_header("Knowledge bases", p.get("count", len(items)), p.get("total"))
        table = self._md_table(
            ["Name", "Description", "ID"],
            [[k.get("name"), k.get("description"), k.get("id")] for k in items],
        )
        return head + "\n\n" + table

    def _render_skills(self, p: dict) -> str:
        items = p.get("skills", [])
        head = self._summary_header("Skills", p.get("count", len(items)))
        table = self._md_table(
            ["Name", "Description", "Active", "ID"],
            [
                [s.get("name"), s.get("description"), self._md_scalar(s.get("is_active")), s.get("id")]
                for s in items
            ],
        )
        return head + "\n\n" + table

    def _render_skill(self, p: dict) -> str:
        lines = [
            f"**Skill: {p.get('name', '(untitled)')}** (id: {p.get('id', '')})",
            "",
        ]
        for key, label, fmt in (
            ("description", "Description", None),
            ("is_active", "Active", self._md_scalar),
            ("created_at", "Created", self._fmt_ts),
            ("updated_at", "Updated", self._fmt_ts),
        ):
            value = p.get(key)
            if value is None:
                continue
            shown = fmt(value) if fmt else value
            lines.append(f"- {label}: {shown}")
        content = p.get("content")
        if content:
            lines.append("")
            lines.append("**Content**")
            lines.append("")
            lines.append(f"```text\n{content}\n```")
        meta = p.get("meta")
        if isinstance(meta, dict) and meta:
            lines.append("")
            lines.append("**Meta**")
            lines.append(self._md_hierarchy(meta))
        return "\n".join(lines)

    async def _run(self, coro: Any, output_format: Optional[str] = None,
                   request: Any = None, emitter: Any = None,
                   action: Optional[str] = None,
                   verbose: Optional[bool] = None) -> str:
        """Execute a private implementation, converting failures to safe output.

        ``request`` is used to redact the request token from any output
        (defense in depth — the token must never reach the model even if it
        accidentally appears in a field or an error message).

        When ``emitter`` is provided, ``action`` (a short progress label such
        as "Querying your chats…") drives the UI status events: a ``status``
        ``done=False`` at start and ``done=True`` at completion, both gated by
        ``verbose`` (the user's choice, else the admin valve). On failure a
        single ``chat:message:error`` is emitted — ALWAYS visible, never gated
        by verbose, and at most one per call (callers consolidate batch
        failures into one summary instead of flooding the user).
        """
        secret = self._extract_token_quiet(request)
        show_status = emitter is not None and action is not None
        if verbose is None:
            verbose = self.valves.verbose
        if show_status and verbose:
            await self._emit_status(emitter, action, done=False)
        try:
            result = await coro
        except ToolError as exc:
            if emitter is not None:
                await self._emit_error(emitter, str(exc))
            return self._redact(self._error(str(exc), output_format), secret)
        except Exception as exc:  # pragma: no cover - defensive
            # The token is never part of an exception message (it travels in a
            # header only), so logging the traceback cannot leak it.
            logger.exception("owui_meta: unexpected error")
            message = (
                f"Unexpected internal error ({type(exc).__name__}); see server logs."
            )
            if emitter is not None:
                await self._emit_error(emitter, message)
            return self._redact(
                self._error(message, output_format),
                secret,
            )
        if show_status and verbose:
            await self._emit_status(emitter, action, done=True)
        return self._redact(result, secret)

    # ──────────────────────────────────────────────
    #  Validation helpers
    # ──────────────────────────────────────────────

    def _require_id(self, value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise ToolError(f"Invalid {name}: expected a string.")
        value = value.strip()
        if not _ID_RE.fullmatch(value):
            raise ToolError(
                f"Invalid {name}: only letters, digits, '-' and '_' are allowed "
                "(no slashes or spaces)."
            )
        return value

    def _coerce_limit(self, value: Any, default: int = 10, cap: int = 100) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(1, min(number, cap))

    @staticmethod
    def _coerce_sort_order(value: Any) -> str:
        try:
            return "asc" if str(value).strip().lower() == "asc" else "desc"
        except Exception:
            return "desc"

    @staticmethod
    def _coerce_optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _fetch_all_pages(self, token: str, path: str, page_size: int = DEFAULT_PAGE_SIZE,
                               params: Optional[dict] = None,
                               max_pages: int = MAX_PAGES) -> tuple[list, int]:
        """Fetch a paginated listing transparently, up to ``max_pages``.

        Iterates pages until the server reports everything (``total``) or a
        short page (fewer items than ``page_size``), bounded by ``max_pages``.
        Returns ``(all_items, total)`` where ``total`` is the server's declared
        total when known, otherwise the number of items fetched.
        """
        all_items: list = []
        server_total: Optional[int] = None
        for page in range(1, max_pages + 1):
            p = dict(params or {})
            p["page"] = page
            p["pageSize"] = page_size
            _s, _ct, body = await self._api_get_json(token, path, p)
            payload = json.loads(body)
            items, total = self._extract_items(payload)
            if isinstance(payload, dict) and "total" in payload:
                server_total = total
            all_items.extend(items)
            if not items or len(items) < page_size:
                break
            if server_total is not None and len(all_items) >= server_total:
                break
        return all_items, server_total if server_total is not None else len(all_items)

    @staticmethod
    def _filter_files(items: list, content_type: Any = None, min_size: Any = None,
                      max_size: Any = None, filename: Any = None) -> list:
        """Client-side file filtering (DESIGN §8.6 point 3): the files API does
        not expose these criteria, so we filter locally over fetched pages.
        All criteria are optional and applied conjunctively.
        """
        min_size = Tools._coerce_optional_int(min_size)
        max_size = Tools._coerce_optional_int(max_size)
        out = []
        for f in items:
            if not isinstance(f, dict):
                continue
            meta = f.get("meta") or {}
            ct = (meta.get("content_type") or "").strip().lower()
            size = meta.get("size")
            name = (f.get("filename") or "").strip().lower()
            if content_type:
                pat = str(content_type).strip().lower()
                if not (ct == pat or (pat.endswith("/*") and ct.startswith(pat[:-1]))):
                    continue
            if min_size is not None and (size is None or size < min_size):
                continue
            if max_size is not None and (size is None or size > max_size):
                continue
            if filename and str(filename).strip().lower() not in name:
                continue
            out.append(f)
        return out

    @staticmethod
    def _sorted_files(items: list, sort_by: Any = "created_at", sort_order: Any = "desc") -> list:
        """Client-side file sorting: size / created_at / filename."""
        if not items:
            return items
        reverse = Tools._coerce_sort_order(sort_order) != "asc"
        key = sort_by if isinstance(sort_by, str) else ""
        if key not in ("size", "created_at", "filename"):
            key = "created_at"

        def k(f: Any):
            if key == "size":
                v = (f.get("meta") or {}).get("size") if isinstance(f, dict) else None
            elif key == "filename":
                v = (f.get("filename") or "").lower() if isinstance(f, dict) else None
            else:
                v = f.get("created_at") if isinstance(f, dict) else None
            return (v is None, v)

        return sorted(items, key=k, reverse=reverse)

    @staticmethod
    def _sorted_chats(items: list, sort_by: Any = "updated_at", sort_order: Any = "desc") -> list:
        """Client-side chat sorting: updated_at / created_at."""
        if not items:
            return items
        reverse = Tools._coerce_sort_order(sort_order) != "asc"
        key = sort_by if isinstance(sort_by, str) else ""
        if key not in ("updated_at", "created_at"):
            key = "updated_at"

        def k(c: Any):
            v = c.get(key) if isinstance(c, dict) else None
            return (v is None, v)

        return sorted(items, key=k, reverse=reverse)

    def _extract_items(self, payload: Any) -> tuple[list, int]:
        """Normalize list-shaped responses (array, {items,total}, {data})."""
        if isinstance(payload, list):
            return payload, len(payload)
        if isinstance(payload, dict):
            for key in ("items", "data"):
                items = payload.get(key)
                if isinstance(items, list):
                    total = len(items)
                    raw = payload.get("total")
                    if raw is not None:
                        try:
                            total = int(raw)
                        except (TypeError, ValueError):
                            pass
                    return items, total
        return [], 0

    # ──────────────────────────────────────────────
    #  Summarizers
    # ──────────────────────────────────────────────

    def _summarize_chats(self, items: list) -> list[dict]:
        return [
            {k: item.get(k) for k in ("id", "title", "created_at", "updated_at")}
            for item in items
            if isinstance(item, dict)
        ]

    def _summarize_models(self, items: list) -> list[dict]:
        return [
            {"id": item.get("id"), "name": item.get("name"), "owned_by": item.get("owned_by")}
            for item in items
            if isinstance(item, dict)
        ]

    def _summarize_skills(self, items: list) -> list[dict]:
        return [
            {k: item.get(k) for k in ("id", "name", "description", "is_active", "created_at", "updated_at")}
            for item in items
            if isinstance(item, dict)
        ]

    def _summarize_files(self, items: list) -> list[dict]:
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") or {}
            meta_data = meta.get("data") or {}
            out.append({
                "id": item.get("id"),
                "filename": item.get("filename"),
                "content_type": meta.get("content_type"),
                "size": meta.get("size"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "origin_chat_id": meta_data.get("chat_id"),
                "origin_message_id": meta_data.get("message_id"),
            })
        return out

    # ──────────────────────────────────────────────
    #  Tool methods — user role (DESIGN §6.1)
    # ──────────────────────────────────────────────

    async def get_my_profile(self, __request__: Any = None, __user__: dict = None,
                             __event_emitter__: Any = None) -> str:
        """Get the requesting user's own profile: id, name, email, role and permissions.

        Use this to learn who you are talking to and what they are allowed to do.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_profile(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading your profile…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_profile(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_PROFILE)
        raw = json.loads(body)
        # SECURITY: GET /api/v1/auths/ (get_session_user, v0.10.2) ECHOES the
        # request token back in the body (token/token_type/expires_at) to
        # support the frontend's session refresh. The tool must NEVER
        # serialize that raw body — in json mode it would leak the user's
        # session credential into the model context. Field whitelist only;
        # the token fields are never included.
        profile = {
            k: raw.get(k)
            for k in (
                "id", "email", "name", "role", "profile_image_url",
                "permissions", "created_at", "updated_at", "last_active_at",
            )
            if k in raw
        }
        return self._ok(profile, "profile", output_format=output_format)

    async def get_models(self, __request__: Any = None, __user__: dict = None,
                         __event_emitter__: Any = None) -> str:
        """List the models available to the requesting user (id, name, owner).

        Only lightweight metadata is returned, not the full model definitions.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_models(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading available models…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_models(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_MODELS)
        payload = json.loads(body)
        items = payload.get("data") if isinstance(payload, dict) else payload
        models = self._summarize_models(items if isinstance(items, list) else [])
        return self._ok({"count": len(models), "models": models}, "models", output_format=output_format)

    async def get_my_chats(
        self,
        limit: int = 10,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        __request__: Any = None,
        __user__: dict = None,
        __event_emitter__: Any = None,
    ) -> str:
        """List the requesting user's recent chats (id, title, dates).

        :param limit: how many chats to return (default 10, max 100).
        :param sort_by: "updated_at" or "created_at" (default "updated_at").
        :param sort_order: "asc" or "desc" (default "desc").
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_chats(
                limit, __request__, sort_by=sort_by, sort_order=sort_order,
                __user__=__user__, output_format=output_format,
            ),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Querying your chats…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_chats(self, limit: Any, request: Any, sort_by: Any = "updated_at",
                            sort_order: Any = "desc", __user__: Optional[dict] = None,
                            output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        sort_order = self._coerce_sort_order(sort_order)
        page_size = min(max(limit, 20), DEFAULT_PAGE_SIZE)
        all_items, total = await self._fetch_all_pages(token, _ROUTE_CHATS, page_size=page_size)
        sorted_items = self._sorted_chats(all_items, sort_by, sort_order)
        chats = self._summarize_chats(sorted_items[:limit])
        return self._ok({"count": len(chats), "total": total, "chats": chats}, "chats", output_format=output_format)

    async def get_chat(self, chat_id: str, __request__: Any = None, __user__: dict = None,
                       __event_emitter__: Any = None) -> str:
        """Get the full content of one chat (all its messages) by id.

        :param chat_id: the chat's UUID.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_chat(chat_id, __request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading chat…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_chat(self, chat_id: Any, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        chat_id = self._require_id(chat_id, "chat_id")
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHAT.format(chat_id=chat_id)
        )
        raw = json.loads(body)
        # Field whitelist (defense in depth): the full ChatResponse carries
        # bookkeeping noise (user_id, meta, tasks, summary, folder_id) that
        # the model does not need. Keep the conversation (chat/messages),
        # title and the user-facing flags only. No token or credential field
        # exists here (verified in v0.10.2), but whitelisting keeps json mode
        # consistent with the markdown renderer.
        chat = {
            k: raw.get(k)
            for k in ("id", "title", "chat", "messages", "created_at",
                      "updated_at", "share_id", "pinned", "archived")
            if k in raw
        }
        return self._ok(chat, "chat", output_format=output_format)

    async def search_chats(self, text: str, __request__: Any = None, __user__: dict = None,
                           __event_emitter__: Any = None) -> str:
        """Search the requesting user's chats for a text fragment.

        :param text: the search term (matched against chat titles and messages).
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._search_chats(text, __request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Searching your chats…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _search_chats(self, text: Any, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ToolError("search_chats requires a non-empty 'text' parameter.")
        text = text.strip()[:200]
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_SEARCH, {"text": text}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"query": text, "count": len(chats), "total": total, "chats": chats}, "chats", output_format=output_format)

    async def get_shared_chats(self, limit: int = 10, __request__: Any = None, __user__: dict = None,
                               __event_emitter__: Any = None) -> str:
        """List chats the requesting user has shared with others.

        :param limit: how many chats to return (default 10, max 100).
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_shared_chats(limit, __request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading shared chats…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_shared_chats(self, limit: Any, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_SHARED, {"pageSize": limit}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"count": len(chats), "total": total, "chats": chats}, "chats", output_format=output_format)

    async def get_pinned_chats(self, limit: int = 10, __request__: Any = None, __user__: dict = None,
                               __event_emitter__: Any = None) -> str:
        """List chats the requesting user has pinned.

        :param limit: how many chats to return (default 10, max 100).
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_pinned_chats(limit, __request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading pinned chats…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_pinned_chats(self, limit: Any, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_PINNED, {"pageSize": limit}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"count": len(chats), "total": total, "chats": chats}, "chats", output_format=output_format)

    async def get_my_files(
        self,
        limit: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        content_type: str = None,
        min_size: int = None,
        max_size: int = None,
        filename: str = None,
        __request__: Any = None,
        __user__: dict = None,
        __event_emitter__: Any = None,
    ) -> str:
        """List the requesting user's files with optional sorting and filtering.

        Returns filename, content type, size (bytes), dates and the origin
        chat/message when the file was generated by a chat. Binary content is
        not fetched — use get_file_content() to read a specific file.

        :param limit: how many files to return after sorting/filtering (default 50, max 500).
        :param sort_by: "size", "created_at" or "filename" (default "created_at").
        :param sort_order: "asc" or "desc" (default "desc").
        :param content_type: filter by MIME type, e.g. "image/png" or "image/*".
        :param min_size: minimum file size in bytes.
        :param max_size: maximum file size in bytes.
        :param filename: partial filename match, case-insensitive.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_files(
                __request__,
                limit=limit, sort_by=sort_by, sort_order=sort_order,
                content_type=content_type, min_size=min_size, max_size=max_size,
                filename=filename, __user__=__user__, output_format=output_format,
            ),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your files…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_files(self, request: Any, limit: Any = 50, sort_by: Any = "created_at",
                            sort_order: Any = "desc", content_type: Any = None,
                            min_size: Any = None, max_size: Any = None, filename: Any = None,
                            __user__: Optional[dict] = None,
                            output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit, default=50, cap=500)
        sort_order = self._coerce_sort_order(sort_order)
        page_size = min(max(limit, 20), DEFAULT_PAGE_SIZE)
        all_items, total = await self._fetch_all_pages(token, _ROUTE_FILES, page_size=page_size)
        filtered = self._filter_files(all_items, content_type, min_size, max_size, filename)
        sorted_items = self._sorted_files(filtered, sort_by, sort_order)
        files = self._summarize_files(sorted_items[:limit])
        return self._ok({
            "count": len(files),
            "matched": len(filtered),
            "total": total,
            "files": files,
        }, "files", output_format=output_format)

    async def get_file_content(self, file_id: str, __request__: Any = None,
                               __user__: dict = None,
                               __event_emitter__: Any = None) -> str:
        """Read a file by id and attach it to the conversation.

        Text files return a 100-character snippet; images and other binary
        files return metadata only. In both cases the file is attached to
        the assistant message via the ``files`` event so the user can
        preview and download it from the UI.

        :param file_id: the file's UUID.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_file_content(file_id, __request__, __user__=__user__,
                                   output_format=output_format,
                                   __event_emitter__=__event_emitter__),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading file…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_file_content(self, file_id: Any, request: Any, __user__: Optional[dict] = None,
                                output_format: Optional[str] = None,
                                __event_emitter__: Optional[Any] = None) -> str:
        token = self._require_token(request)
        file_id = self._require_id(file_id, "file_id")

        # Best-effort metadata fetch for the attachment's display name. The
        # body is never serialized raw — only ``filename`` is extracted (the
        # static no-raw-body tripwire pins this). A failure here (route
        # changed, file missing, unexpected content type) must not block the
        # content fetch below, so it is swallowed and the id doubles as name.
        filename = None
        try:
            _status, _ct, body = await self._api_get_json(token, _ROUTE_FILE.format(file_id=file_id))
            meta = json.loads(body)
            if isinstance(meta, dict):
                filename = meta.get("filename")
        except Exception:
            filename = None

        path = _ROUTE_FILE_CONTENT.format(file_id=file_id)
        _status, content_type, body = await self._api_get_raw(token, path)
        ct = content_type.split(";")[0].strip().lower()
        size = len(body)

        # Attach the file to the assistant message (UI preview + download).
        # The event is emitted BEFORE the response so the attachment shows up
        # as the model starts replying.
        await self._emit_files(
            __event_emitter__, [self._file_attachment(file_id, ct, size, filename)]
        )

        if ct.startswith("text/") or ct in _TEXT_CONTENT_TYPES or ct.endswith(("+json", "+xml")):
            text = body.decode("utf-8", errors="replace")
            snippet = text[:FILE_SNIPPET_CHARS]
            truncated = len(text) > FILE_SNIPPET_CHARS
            return self._ok({
                "file_id": file_id,
                "filename": filename,
                "content_type": ct,
                "size": size,
                "content": snippet,
                "truncated": truncated,
                "total_chars": len(text),
                "note": f"Showing the first {FILE_SNIPPET_CHARS} characters; "
                        "the full file is attached to the conversation.",
            }, "file_text", output_format=output_format)

        return self._ok({
            "file_id": file_id,
            "filename": filename,
            "content_type": ct,
            "size": size,
            "note": f"Binary content ({ct}) is not returned inline; the file is "
                    "attached to the conversation (preview and download available).",
        }, "file_binary", output_format=output_format)

    def _file_attachment(self, file_id: str, content_type: str, size: int,
                         filename: Optional[str]) -> dict:
        """Build the ``files`` event item for a downloaded file.

        Mirrors what the Open WebUI frontend renders (verified against main's
        ResponseMessage.svelte / FileItem.svelte / FileItemModal.svelte):

        - images -> ``type: "image"`` + a '/'-prefixed path URL: Image.svelte
          prefixes ``WEBUI_BASE_URL`` for paths starting with '/' and renders
          an inline preview (mirrors generate_image).
        - everything else -> ``type: "file"`` + the bare file id as ``url``:
          FileItem.svelte opens ``/files/{url}/content`` (session cookie),
          and FileItemModal reads ``meta.content_type`` for the preview.

        A bare id as ``url`` with ``type: "file"`` would break inline image
        preview (ResponseMessage renders images from ``file.url`` directly),
        hence the special-casing above.
        """
        name = filename or file_id
        if content_type.startswith("image/"):
            return {
                "type": "image",
                "url": f"/api/v1/files/{file_id}/content",
                "name": name,
                "size": size,
                "content_type": content_type,
            }
        return {
            "type": "file",
            "url": file_id,
            "name": name,
            "size": size,
            "content_type": content_type,
            "meta": {"content_type": content_type},
        }

    async def _emit_files(self, emitter: Optional[Any], files: list) -> None:
        """Emit a ``files`` event attaching files to the assistant message.

        Native Open WebUI event (verified in backend/open_webui/socket/main.py):
        the backend re-broadcasts it to the UI in real time AND persists the
        items into the assistant message's ``files`` field — no extra
        persistence code needed. Best-effort: a failed UI event must never
        break the tool call (the returned text still carries the snippet /
        metadata).
        """
        if emitter is None or not files:
            return
        try:
            await emitter({"type": "files", "data": {"files": files}})
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

    async def _emit_status(self, emitter: Optional[Any], description: str,
                           done: bool = False) -> None:
        """Emit a real-time progress ``status`` event (the UI shimmer).

        Mirrors the smart_fetch_url UX pattern (DESIGN §8.5): ``done=False``
        starts the progress indicator, a final ``done=True`` stops it. Gated
        by the ``verbose`` valve in ``_run`` — the caller decides. Best-effort:
        a failed UI event never breaks the tool call.
        """
        if emitter is None:
            return
        try:
            await emitter({
                "type": "status",
                "data": {
                    "description": description,
                    "done": done,
                    "hidden": False,
                },
            })
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

    async def _emit_error(self, emitter: Optional[Any], message: str) -> None:
        """Emit a single visible error event for the message.

        Uses ``chat:message:error`` (the error block the frontend renders in
        the assistant message — Error.svelte) rather than a status event, so
        failures stand out from progress. Errors are NOT gated by ``verbose``
        (they must always be visible), but callers consolidate: at most ONE
        error event per tool call — e.g. delete_files reports a batch failure
        as a single "N of M files failed" instead of one event per file, so
        the user is never flooded.
        """
        if emitter is None:
            return
        try:
            await emitter({
                "type": "chat:message:error",
                "data": {"error": {"content": message}},
            })
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

    async def delete_files(self, file_ids: list, __request__: Any = None,
                           __user__: dict = None,
                           __event_emitter__: Any = None) -> str:
        """Delete several files permanently in one pass (storage + vector index).

        Destructive and irreversible. Each file is reported with its name;
        failures (missing / not yours / backend error) are reported per id
        without aborting the rest. The backend only allows deleting your
        own files (or files you have write access to).

        :param file_ids: the UUIDs of the files to delete.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._delete_files(file_ids, __request__, __user__=__user__,
                               output_format=output_format,
                               __event_emitter__=__event_emitter__),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Deleting files…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _delete_files(self, file_ids: Any, request: Any, __user__: Optional[dict] = None,
                            output_format: Optional[str] = None,
                            __event_emitter__: Optional[Any] = None) -> str:
        token = self._require_token(request)

        # Validate the WHOLE list up front: one invalid id rejects the call
        # before any request is made, so nothing is deleted.
        if not isinstance(file_ids, (list, tuple, set)) or not file_ids:
            raise ToolError("Invalid file_ids: expected a non-empty list of file ids.")
        ids = list(file_ids)
        if len(ids) > MAX_DELETE_FILES:
            raise ToolError(
                f"Too many file ids: at most {MAX_DELETE_FILES} per call "
                f"({len(ids)} given)."
            )
        cleaned = [self._require_id(v, "file_id") for v in ids]
        # Drop duplicates so the same file is never deleted twice.
        seen = set()
        unique = []
        for c in cleaned:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        deleted, failed = [], []
        for file_id in unique:
            try:
                deleted.append(await self._delete_one_file(token, file_id))
            except ToolError as exc:
                failed.append({"file_id": file_id, "error": str(exc)})
            except Exception as exc:  # defensive — per-file isolation
                failed.append({"file_id": file_id, "error": f"Unexpected error ({type(exc).__name__})"})

        # A batch with failures emits ONE consolidated error event (the
        # per-id detail stays in the returned text), so a multi-file cleanup
        # never floods the user with one error per failed file.
        if failed and __event_emitter__ is not None:
            await self._emit_error(
                __event_emitter__,
                f"{len(failed)} of {len(unique)} file(s) could not be deleted.",
            )

        return self._ok({
            "requested": len(unique),
            "deleted_count": len(deleted),
            "failed_count": len(failed),
            "deleted": deleted,
            "failed": failed,
        }, "files_deleted", output_format=output_format)

    async def _delete_one_file(self, token: str, file_id: str) -> dict:
        """Delete a single file and return its report.

        Fetch metadata first: (1) it gives the user a clear report of what is
        about to disappear, and (2) a 404 here means the DELETE would fail
        too — report it without touching anything.
        """
        _status, _ct, body = await self._api_get_json(token, _ROUTE_FILE.format(file_id=file_id))
        meta = json.loads(body)
        filename = meta.get("filename") if isinstance(meta, dict) else None
        meta_info = meta.get("meta") if isinstance(meta, dict) else None
        content_type = (meta_info or {}).get("content_type") if isinstance(meta_info, dict) else None
        size = (meta_info or {}).get("size") if isinstance(meta_info, dict) else None

        _status, _ct, resp_body = await self._api_delete_json(token, _ROUTE_FILE.format(file_id=file_id))
        message = "File deleted successfully"
        try:
            resp = json.loads(resp_body)
            if isinstance(resp, dict) and resp.get("message"):
                message = resp["message"]
        except Exception:
            pass  # response body is informational only

        return {
            "file_id": file_id,
            "filename": filename,
            "content_type": content_type,
            "size": size,
            "message": message,
        }

    async def get_my_prompts(self, __request__: Any = None, __user__: dict = None,
                             __event_emitter__: Any = None) -> str:
        """List the requesting user's custom prompts (command, name, content)."""
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_prompts(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your prompts…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_prompts(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_PROMPTS)
        items, _total = self._extract_items(json.loads(body))
        prompts = [
            {k: item.get(k) for k in ("id", "command", "name", "content")}
            for item in items
            if isinstance(item, dict)
        ]
        return self._ok({"count": len(prompts), "prompts": prompts}, "prompts", output_format=output_format)

    async def get_my_tools(self, __request__: Any = None, __user__: dict = None,
                           __event_emitter__: Any = None) -> str:
        """List the tools available to the requesting user (id, name, description)."""
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_tools(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your tools…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_tools(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_TOOLS)
        items, _total = self._extract_items(json.loads(body))
        tools = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") or {}
            tools.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "description": meta.get("description"),
            })
        return self._ok({"count": len(tools), "tools": tools}, "tools", output_format=output_format)

    async def get_knowledge_bases(self, __request__: Any = None, __user__: dict = None,
                                  __event_emitter__: Any = None) -> str:
        """List the knowledge bases available to the requesting user (id, name, description)."""
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_knowledge_bases(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing knowledge bases…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_knowledge_bases(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_KNOWLEDGE)
        items, total = self._extract_items(json.loads(body))
        knowledge = [
            {k: item.get(k) for k in ("id", "name", "description", "created_at")}
            for item in items
            if isinstance(item, dict)
        ]
        return self._ok({"count": len(knowledge), "total": total, "knowledge": knowledge}, "knowledge", output_format=output_format)

    async def get_my_skills(self, __request__: Any = None, __user__: dict = None,
                            __event_emitter__: Any = None) -> str:
        """List the skills available to the requesting user (id, name, description, active state).

        Skills are workspace resources like tools and prompts: the list includes
        the user's own skills and skills shared with them via access grants.
        The skill's content (its instructions) is not included here to keep the
        listing light — use get_skill() for the full detail.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_my_skills(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your skills…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_my_skills(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_SKILLS)
        items, _total = self._extract_items(json.loads(body))
        skills = self._summarize_skills(items)
        return self._ok({"count": len(skills), "skills": skills}, "skills", output_format=output_format)

    async def get_skill(self, skill_id: str, __request__: Any = None, __user__: dict = None,
                        __event_emitter__: Any = None) -> str:
        """Get one skill's full detail by id, including its content (the skill's instructions).

        :param skill_id: the skill's id (letters, digits, '-' and '_').
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_skill(skill_id, __request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading skill…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_skill(self, skill_id: Any, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        skill_id = self._require_id(skill_id, "skill_id")
        _status, _ct, body = await self._api_get_json(token, _ROUTE_SKILL.format(skill_id=skill_id))
        raw = json.loads(body)
        # Field whitelist: the raw SkillAccessResponse embeds the OWNER's
        # UserResponse (id, email, …) — for a shared skill that is another
        # user's contact info — plus access_grants/write_access bookkeeping.
        # None of it is needed to answer queries about the skill.
        skill = {
            k: raw.get(k)
            for k in ("id", "name", "description", "content", "is_active",
                      "created_at", "updated_at", "meta")
            if k in raw
        }
        return self._ok(skill, "skill", output_format=output_format)
