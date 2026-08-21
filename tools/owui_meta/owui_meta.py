"""
title: Open WebUI Meta-Tool
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Queries Open WebUI's own internal API to answer questions about the requesting user's data (chats, files, prompts, tools, models, knowledge), plus explicit user-authorized file deletion for cleanup. Authenticates automatically with the requesting user's token — no credentials to configure. Allowlisted endpoints only.
required_open_webui_version: 0.9.0
requirements: httpx, Pillow
version: 0.21.0
licence: MIT
"""

import asyncio
import io
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

# Defensive import: Pillow is bundled with Open WebUI (image processing),
# but outside that environment it may be absent — the image header metadata
# (Iteration 9 task 9.2) degrades gracefully to no extra fields.
try:
    from PIL import Image
except Exception:  # pragma: no cover - environment-dependent
    Image = None

# Hard cap on how many files one delete_files() call may delete in a single
# pass. Prevents a runaway tool call from wiping a large library at once.
MAX_DELETE_FILES = 50

# Transparent page iteration: the API caps at 50 items/page and exposes
# ``total`` (DESIGN §8.6). ``MAX_PAGES`` bounds how many pages the tool will
# fetch for filtering/sorting so a huge dataset cannot cost an unbounded
# number of internal calls.
MAX_PAGES = 5
DEFAULT_PAGE_SIZE = 50

# Default/cap for the head/tail counts of the get_chat_summary snippet (Iteration 8).
# Fixed at 3+3 by design (user decision 2026-08-20): the model never chooses
# these — the summary always shows the first 3 and last 3 messages.
DEFAULT_SNIPPET_HEAD = 3
DEFAULT_SNIPPET_TAIL = 3

# Per-message character budget for the get_chat_summary snippet. Lines longer than
# this are truncated with an ellipsis; combined with head/tail caps this
# keeps the whole snippet well under max_response_chars.
MAX_SNIPPET_MESSAGE_CHARS = 160

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
_ROUTE_FOLDERS = "/api/v1/folders/"
_ROUTE_CHATS_ALL_TAGS = "/api/v1/chats/all/tags"
_ROUTE_CHATS_ARCHIVED = "/api/v1/chats/archived"
_ROUTE_CHATS_STATS_USAGE = "/api/v1/chats/stats/usage"
# POST /chats/tags filters chats by tag server-side ({name, skip, limit} ->
# bare ChatTitleIdResponse array). It is a QUERY (no side effects) despite
# being POST — the backend uses POST for the JSON body, not for writing.
_ROUTE_CHATS_TAGS = "/api/v1/chats/tags"

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
                     method: str = "GET",
                     json_body: Optional[dict] = None) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "owui_meta/0.1.0 (Open WebUI internal tool)",
        }
        async with self._client() as client:
            return await client.request(
                method, url, headers=headers, params=params, json=json_body
            )

    async def _fetch_with_retry(self, token: str, path: str,
                                params: Optional[dict] = None,
                                accept: str = "application/json",
                                method: str = "GET",
                                json_body: Optional[dict] = None) -> httpx.Response:
        """Call an allowlisted route, retrying once against the fallback URL.

        Retry only on transport errors (DNS/connection/timeout — DESIGN §4.3).
        Never retries on API 4xx/5xx responses. ``json_body`` is only sent
        for methods that take a body (POST); GET/DELETE pass None.
        """
        base = await self._resolve_base_url()
        primary_url = base + path
        try:
            resp = await self._fetch(primary_url, token, params, accept, method, json_body)
        except httpx.RequestError as exc:
            fallback = self.valves.fallback_base_url.rstrip("/")
            if fallback and fallback != base:
                try:
                    resp = await self._fetch(fallback + path, token, params, accept, method, json_body)
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

    async def _api_post_json(self, token: str, path: str,
                             body: dict) -> tuple[int, str, str]:
        """JSON POST for QUERY-ONLY routes (e.g. the tag filter).

        Same content-type validation and non-leaking error mapping as GET.
        Only allowlisted routes reach here; ``body`` is a fixed, typed dict
        built by the caller (never from a user-controlled URL or key).
        """
        resp = await self._fetch_with_retry(token, path, method="POST", json_body=body)
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
        """Recursively drop credential-looking keys (string values only).

        Fail-loud (Iteration 9 task 9.4, 2026-08-21): whenever a
        credential-like key is actually dropped, a warning is logged with
        the KEY NAME ONLY (never the value — it may BE the credential), so a
        future method or server field that accidentally carries a credential
        becomes visible in the server log instead of being silently cleaned.
        """
        if isinstance(value, dict):
            out = {}
            for key, val in value.items():
                if cls._SENSITIVE_KEY_RE.search(str(key)) and isinstance(val, str) and val.strip():
                    # Fail-loud: log the key name; the value is never logged.
                    logger.warning(
                        "owui_meta: dropped credential-like key %r from output "
                        "(value not logged)",
                        str(key),
                    )
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
        if kind == "chat_metadata":
            return self._render_chat_metadata(payload)
        if kind == "tags":
            return self._render_tags(payload)
        if kind == "chat_stats":
            return self._render_chat_stats(payload)
        if kind == "folders":
            return self._render_folders(payload)
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
        if query:
            head = f"**Search results for '{query}': {count}**"
        else:
            head = self._summary_header(p.get("label", "Chats"), count, p.get("total"))
        # Search results carry a ``snippet`` (the matched message fragment);
        # list endpoints never do. Show it as an extra column only when the
        # call was a search AND at least one result has a snippet.
        show_snippet = bool(query) and any(m.get("snippet") for m in items)
        headers = ["Title", "Updated", "ID"]
        rows = [[m.get("title"), self._fmt_ts(m.get("updated_at")), m.get("id")] for m in items]
        if show_snippet:
            headers.append("Snippet")
            rows = [row + [m.get("snippet")] for row, m in zip(rows, items)]
        table = self._md_table(headers, rows)
        return head + "\n\n" + table

    def _render_chat(self, p: dict) -> str:
        """Render the chat snippet: simple header, metadata, head + tail.

        Format (user decision 2026-08-20): bold only on the chat title and
        the role labels, plain metadata lines, an ellipsis line between the
        head and tail blocks. Message lines are pre-normalized (single line,
        backticks escaped) so no fence or table can break the output.
        """
        lines = [f"**Chat: {p.get('title', '(untitled)')}**", ""]
        meta = f"- Messages: {p.get('message_count', 0)}"
        models = p.get("models") or []
        if models:
            meta += " · Models: " + ", ".join(models)
        tags = p.get("tags") or []
        if tags:
            meta += " · Tags: " + ", ".join(tags)
        lines.append(meta)
        folder = p.get("folder_name") or p.get("folder_id")
        if folder:
            lines.append(f"- Folder: {folder}")
        lines.append(
            f"- Created: {self._fmt_ts(p.get('created_at'))} · "
            f"Updated: {self._fmt_ts(p.get('updated_at'))}"
        )
        lines.append("")

        def role_label(role: Any) -> str:
            return "**User**" if role == "user" else "**Assistant**"

        for item in p.get("head", []):
            lines.append(f"{role_label(item.get('role'))}: {item.get('text', '')}")
        if p.get("skipped"):
            lines.append("")
            lines.append(f"… ( {p['skipped']} messages skipped ) …")
            lines.append("")
        for item in p.get("tail", []):
            lines.append(f"{role_label(item.get('role'))}: {item.get('text', '')}")
        return "\n".join(lines).rstrip()

    def _render_chat_metadata(self, p: dict) -> str:
        """Render the organization metadata of a chat as plain bullets.

        No message content — this is the metadata-only view.
        """
        lines = [f"**Chat: {p.get('title', '(untitled)')}**", ""]
        meta = f"- Messages: {p.get('message_count', 0)}"
        models = p.get("models") or []
        if models:
            meta += " · Models: " + ", ".join(models)
        tags = p.get("tags") or []
        if tags:
            meta += " · Tags: " + ", ".join(tags)
        lines.append(meta)
        folder = p.get("folder_name") or p.get("folder_id")
        if folder:
            lines.append(f"- Folder: {folder}")
        lines.append(
            f"- Created: {self._fmt_ts(p.get('created_at'))} · "
            f"Updated: {self._fmt_ts(p.get('updated_at'))}"
        )
        return "\n".join(lines).rstrip()

    def _render_tags(self, p: dict) -> str:
        items = p.get("tags", [])
        head = self._summary_header("Tags", p.get("count", len(items)))
        table = self._md_table(
            ["Name", "ID"],
            [[t.get("name"), t.get("id")] for t in items],
        )
        return head + "\n\n" + table

    def _render_chat_stats(self, p: dict) -> str:
        lines = [f"**Chat stats**", ""]
        lines.append(f"- Messages: {p.get('message_count', 0)}")
        models = p.get("models") or {}
        if models:
            lines.append("- Models: " + ", ".join(f"{k} (×{v})" for k, v in sorted(models.items())))
        tags = p.get("tags") or []
        if tags:
            lines.append("- Tags: " + ", ".join(tags))
        for key, label, no_text_label in (
            ("average_response_time", "Avg response time (s)", None),
            ("average_user_message_content_length", "Avg user msg length (chars)", "(no text)"),
            ("average_assistant_message_content_length", "Avg assistant msg length (chars)", "(no text)"),
            ("last_message_at", "Last message", None),
        ):
            if key not in p:
                continue
            value = p.get(key)
            if value is None:
                if no_text_label is not None:
                    lines.append(f"- {label}: {no_text_label}")
                continue
            rendered = self._fmt_ts(value) if key == "last_message_at" else value
            lines.append(f"- {label}: {rendered}")
        # Corrected-vs-backend note (Iteration 9 task 9.3): the raw
        # stats/usage route reports 0.0 for assistant lengths on v0.10.2;
        # the values above are recomputed from the chat's real message text.
        corrected = []
        for key, label in (
            ("average_user_message_content_length", "user"),
            ("average_assistant_message_content_length", "assistant"),
        ):
            backend = p.get(f"{key}_backend")
            value = p.get(key)
            if backend is not None and value is not None and backend != value:
                corrected.append(f"{label} backend {backend} (0.0 = v0.10.2 bug)")
        if corrected:
            lines.append("- Note: " + "; ".join(corrected) + " — corrected above")
        lines.append(
            f"- Created: {self._fmt_ts(p.get('created_at'))} · "
            f"Updated: {self._fmt_ts(p.get('updated_at'))}"
        )
        return "\n".join(lines).rstrip()

    def _render_folders(self, p: dict) -> str:
        items = p.get("folders", [])
        head = self._summary_header("Folders", p.get("count", len(items)))
        table = self._md_table(
            ["Name", "Parent", "Expanded", "Created", "ID"],
            [
                [
                    f.get("name"),
                    f.get("parent_id") or "—",
                    "yes" if f.get("is_expanded") else "no",
                    self._fmt_ts(f.get("created_at")),
                    f.get("id"),
                ]
                for f in items
            ],
        )
        return head + "\n\n" + table

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

    # ──────────────────────────────────────────────
    #  Image header metadata (Iteration 9 task 9.2, DESIGN §8.9.2)
    #
    #  Uses Pillow (bundled with Open WebUI) — never reimplements image
    #  parsers. Image.open is lazy (header only; pixel data is never
    #  decoded), so the cost is O(1) regardless of file size and no pixel
    #  data ever reaches the output. Any failure (bad/truncated file, Pillow
    #  absent) degrades to an empty dict — the caller omits the fields and
    #  the call never errors.
    # ──────────────────────────────────────────────

    # Bits per channel for Pillow modes (used when ``img.bits`` is not
    # exposed, which Pillow only does for some modes).
    _MODE_BITS = {
        "1": 1, "L": 8, "P": 8, "LA": 8, "PA": 8,
        "RGB": 8, "RGBA": 8, "CMYK": 8, "YCbCr": 8,
        "I;16": 16, "I": 32, "F": 32,
    }

    def _image_header_info(self, body: bytes) -> dict:
        """Best-effort image header metadata via Pillow (width/height/depth).

        Returns ``{"width", "height", "color_mode" (Pillow mode: RGB, RGBA,
        L, P, CMYK, …), "bit_depth" (bits per channel, derived from the
        mode)}`` or ``{}`` on any failure. The lazy ``Image.open`` never
        decodes pixel data.
        """
        if Image is None or not body:
            return {}
        try:
            with Image.open(io.BytesIO(body)) as img:
                info = {"width": img.width, "height": img.height}
                mode = getattr(img, "mode", None)
                if mode:
                    info["color_mode"] = mode
                    info["bit_depth"] = self._MODE_BITS.get(mode) or getattr(img, "bits", None)
                return info
        except Exception:
            return {}

    def _image_meta_lines(self, p: dict) -> list:
        """Markdown lines describing an image's resolution + color depth."""
        if not p.get("width") or not p.get("height"):
            return []
        parts = [f"{p['width']}\u00d7{p['height']} px"]
        mode = p.get("color_mode")
        bits = p.get("bit_depth")
        if mode and bits is not None:
            parts.append(f"{mode} ({bits}-bit)")
        elif mode:
            parts.append(mode)
        elif bits is not None:
            parts.append(f"{bits}-bit")
        return ["- Image: " + ", ".join(parts)]

    def _render_file_binary(self, p: dict) -> str:
        ident = p.get("filename") or p.get("file_id")
        head = f"**File: {ident}** ({p.get('content_type')}, {p.get('size')} bytes)"
        if p.get("filename"):
            head += f" (id: {p.get('file_id')})"
        lines = [head]
        img = self._image_meta_lines(p)
        if img:
            lines += ["", *img]
        note = p.get("note", "Binary content not returned inline.")
        if note:
            lines += ["", note]
        return "\n".join(lines)

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

    # ──────────────────────────────────────────────
    #  Chat snippet helpers (Iteration 8: get_chat_summary)
    # ──────────────────────────────────────────────

    @staticmethod
    def _message_text(message: dict) -> Optional[str]:
        """Extract the readable text of one chat message.

        v0.10.2 stores message text in one of three places:
        - ``content`` as a plain string (most user messages),
        - ``content`` as a list of parts (multimodal legacy),
        - ``output[]`` items of type ``message`` whose ``content[]`` items
          of type ``output_text`` hold the assistant text (assistant
          messages usually have an empty ``content``).
        Returns None when the message has no readable text (e.g. pure
        function-call steps), which are skipped in the snippet.
        """
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                c.get("text")
                for c in content
                if isinstance(c, dict) and isinstance(c.get("text"), str) and c.get("text").strip()
            ]
            if parts:
                return "\n".join(parts).strip()
        output = message.get("output")
        if isinstance(output, list):
            texts = []
            for part in output:
                if not isinstance(part, dict) or part.get("type") != "message":
                    continue
                for c in part.get("content") or []:
                    if (
                        isinstance(c, dict)
                        and c.get("type") == "output_text"
                        and isinstance(c.get("text"), str)
                        and c.get("text").strip()
                    ):
                        texts.append(c["text"].strip())
            if texts:
                return "\n".join(texts).strip()
        return None

    def _main_branch_messages(self, raw: dict) -> list:
        """Return the messages on the main branch of a chat, oldest first.

        The chat is a tree (parentId/childrenIds). The readable conversation
        is the chain from ``currentId`` back through ``parentId``, reversed —
        sorting by timestamp instead mixes branches and produces a broken
        narrative (verified with real data 2026-08-20).
        """
        history = (raw.get("chat") or {}).get("history") or {}
        msgs = history.get("messages") or {}
        if not isinstance(msgs, dict):
            return []
        branch: list = []
        seen = set()
        current = history.get("currentId")
        while current and current in msgs and current not in seen:
            seen.add(current)
            item = msgs[current]
            if isinstance(item, dict):
                branch.append(item)
            current = item.get("parentId") if isinstance(item, dict) else None
        branch.reverse()
        return branch

    @staticmethod
    def _normalize_snippet_text(text: str, max_chars: int = MAX_SNIPPET_MESSAGE_CHARS) -> str:
        """Collapse a message into one markdown-safe line for the snippet.

        Newlines become ' ⏎ ' (visible break without breaking the layout),
        every backtick is escaped so no code fence or code span can open and
        swallow the rest of the output, and the line is truncated. Escaped
        backticks render literally in Markdown (e.g. a triple fence shows as
        '```' instead of starting a code block).
        """
        if not text:
            return ""
        one_line = text.replace("\n", " ⏎ ").strip()
        one_line = one_line.replace("`", "\\`")
        if len(one_line) <= max_chars:
            return one_line
        return one_line[: max_chars - 1].rstrip() + "…"

    async def _folder_name(self, token: str, folder_id: Optional[str]) -> Optional[str]:
        """Resolve a folder id to its display name (best-effort).

        The ChatResponse carries only ``folder_id``; the name lives in the
        folders router. A failure (folders disabled, route changed) falls
        back to None — the renderer then shows the id. Never breaks the
        chat call.
        """
        if not folder_id:
            return None
        try:
            _status, _ct, body = await self._api_get_json(token, _ROUTE_FOLDERS)
            folders = json.loads(body)
            if isinstance(folders, list):
                for folder in folders:
                    if (
                        isinstance(folder, dict)
                        and folder.get("id") == folder_id
                        and folder.get("name")
                    ):
                        return folder["name"]
        except Exception:
            pass
        return None

    async def _fetch_all_pages(self, token: str, path: str, page_size: int = DEFAULT_PAGE_SIZE,
                               params: Optional[dict] = None,
                               max_pages: int = MAX_PAGES,
                               short_page_stops: bool = True) -> tuple[list, int]:
        """Fetch a paginated listing transparently, up to ``max_pages``.

        Iterates pages until the server reports everything (``total``), a
        short page (fewer items than ``page_size``) or an empty page,
        bounded by ``max_pages``. ``short_page_stops=False`` disables the
        short-page heuristic for routes that IGNORE ``pageSize`` and return
        irregularly-sized pages (verified live 2026-08-20: the EXPERIMENTAL
        ``/api/v1/chats/stats/usage`` returns 50/49/49 rows then an empty
        page while declaring ``total`` 149) — those routes end only on an
        empty page or the declared total. Returns ``(all_items, total)``
        where ``total`` is the server's declared total when known, otherwise
        the number of items fetched.
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
            if not items:
                break
            if short_page_stops and len(items) < page_size:
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
        """Summarize chat list items (ChatTitleIdResponse shape).

        List endpoints only ever carry id/title/dates, but two sources add
        more fields the model needs: search results carry a ``snippet`` (the
        matched message fragment) and stats/usage items carry ``tags``.
        Both are kept when present so callers can render them.
        """
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            chat = {k: item.get(k) for k in ("id", "title", "created_at", "updated_at")}
            tags = item.get("tags")
            if tags:
                chat["tags"] = tags
            snippet = item.get("snippet")
            if snippet:
                chat["snippet"] = snippet
            out.append(chat)
        return out

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

    async def get_profile(self, __request__: Any = None, __user__: dict = None,
                             __event_emitter__: Any = None) -> str:
        """Get the requesting user's own profile: id, name, email, role and permissions.

        Use this to learn who you are talking to and what they are allowed to do.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_profile(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading your profile…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_profile(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
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

    async def get_chats(
        self,
        scope: str = "all",
        limit: int = 10,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        tag: str = None,
        __request__: Any = None,
        __user__: dict = None,
        __event_emitter__: Any = None,
    ) -> str:
        """List the requesting user's chats (id, title, dates), by scope.

        ``scope`` selects the collection: "all" (default), "pinned",
        "shared" or "archived".

        ``scope="all"`` includes chats inside folders and pinned chats (the
        backend hides them from the default listing unless
        include_folders/include_pinned are sent — verified live 2026-08-20);
        it accepts ``tag`` to filter to chats carrying that tag (server-side,
        pure tag filter — not a text search; e.g. ``tag="tool"``). Unlike
        search_chats, this tag filter does NOT exclude archived chats. A
        ``tag`` filter with zero matches triggers the backend's orphan-tag
        cleanup (the tag's catalog entry is deleted — intended behavior;
        per-chat tags are untouched). ``tag`` is accepted ONLY with
        ``scope="all"`` — the backend has no route combining a scope with a
        tag filter.

        ``limit`` semantics differ per scope: for "all" it is the top-N after
        iterating server pages; for "pinned"/"shared" the top-N of the
        paged response; for "archived" the top-N of the whole (non-paginated)
        list.

        :param scope: "all", "pinned", "shared" or "archived" (default "all").
        :param limit: how many chats to return (default 10, max 100).
        :param sort_by: "updated_at" or "created_at" (default "updated_at").
        :param sort_order: "asc" or "desc" (default "desc").
        :param tag: filter to chats with this tag; only valid with scope="all" (default None = all chats).
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_chats(
                scope, limit, __request__, sort_by=sort_by, sort_order=sort_order,
                tag=tag, __user__=__user__, output_format=output_format,
            ),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action=self._chats_action(scope),
            verbose=self._resolve_verbose(__user__),
        )

    @staticmethod
    def _chats_action(scope: Any) -> str:
        """Progress label for a get_chats call, per scope."""
        return {
            "all": "Querying your chats…",
            "pinned": "Reading pinned chats…",
            "shared": "Reading shared chats…",
            "archived": "Reading archived chats…",
        }.get(scope, "Querying your chats…")

    async def _get_chats(self, scope: Any, limit: Any, request: Any,
                         sort_by: Any = "updated_at", sort_order: Any = "desc",
                         tag: Any = None,
                         __user__: Optional[dict] = None,
                         output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        sort_order = self._coerce_sort_order(sort_order)
        if scope not in ("all", "pinned", "shared", "archived"):
            raise ToolError(
                "Invalid scope: expected one of 'all', 'pinned', 'shared', "
                f"'archived' (got {scope!r})."
            )
        if isinstance(tag, str):
            tag = tag.strip()
            if not tag:
                tag = None
        if tag is not None and scope != "all":
            raise ToolError("The 'tag' filter only applies to scope='all'.")

        if scope == "all":
            if tag is not None:
                if len(tag) > 100:
                    raise ToolError("Invalid tag: at most 100 characters.")
                all_items = await self._fetch_chats_by_tag(token, tag)
                total = len(all_items)
            else:
                # The backend hides folder + pinned chats unless these flags
                # are sent (verified live 2026-08-20: default listing excludes
                # them, delta ~1/3 of the user's chats). The flags only change
                # which rows come back — item fields stay ChatTitleIdResponse.
                page_size = min(max(limit, 20), DEFAULT_PAGE_SIZE)
                all_items, total = await self._fetch_all_pages(
                    token, _ROUTE_CHATS, page_size=page_size,
                    params={"include_folders": "true", "include_pinned": "true"},
                )
            sorted_items = self._sorted_chats(all_items, sort_by, sort_order)
            chats = self._summarize_chats(sorted_items[:limit])
            return self._ok(
                {"count": len(chats), "total": total, "chats": chats},
                "chats", output_format=output_format,
            )

        if scope == "pinned":
            _status, _ct, body = await self._api_get_json(
                token, _ROUTE_CHATS_PINNED, {"pageSize": limit}
            )
            items, total = self._extract_items(json.loads(body))
            chats = self._summarize_chats(items)
            return self._ok(
                {"count": len(chats), "total": total, "chats": chats},
                "chats", output_format=output_format,
            )

        if scope == "shared":
            _status, _ct, body = await self._api_get_json(
                token, _ROUTE_CHATS_SHARED, {"pageSize": limit}
            )
            items, total = self._extract_items(json.loads(body))
            chats = self._summarize_chats(items)
            return self._ok(
                {"count": len(chats), "total": total, "chats": chats},
                "chats", output_format=output_format,
            )

        # scope == "archived" — plain ChatTitleIdResponse list, no server-side
        # pagination; sliced by limit (N2: "top-N of the returned list").
        _status, _ct, body = await self._api_get_json(token, _ROUTE_CHATS_ARCHIVED)
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)[:limit]
        return self._ok({
            "label": "Archived chats",
            "count": len(chats),
            "total": total,
            "chats": chats,
        }, "chats", output_format=output_format)

    async def _fetch_chats_by_tag(self, token: str, tag: str) -> list:
        """Fetch all chats carrying a tag via POST /api/v1/chats/tags.

        The backend paginates with ``skip``/``limit`` (no ``total`` field —
        bare ChatTitleIdResponse array) and the response is NOT sorted by
        the tool's sort keys, so the caller sorts client-side. Bounded by
        MAX_PAGES pages of DEFAULT_PAGE_SIZE. The POST is a pure query
        (no side effects) — see _ROUTE_CHATS_TAGS.
        """
        all_items: list = []
        for page in range(MAX_PAGES):
            skip = page * DEFAULT_PAGE_SIZE
            _s, _ct, body = await self._api_post_json(
                token, _ROUTE_CHATS_TAGS,
                {"name": tag, "skip": skip, "limit": DEFAULT_PAGE_SIZE},
            )
            items, _total = self._extract_items(json.loads(body))
            all_items.extend(items)
            if len(items) < DEFAULT_PAGE_SIZE:
                break
        return all_items

    async def get_tags(self, __request__: Any = None, __user__: dict = None,
                       __event_emitter__: Any = None) -> str:
        """List the tags the requesting user has used on chats (name and id).

        Reads ``GET /api/v1/chats/all/tags``. The per-tag user_id and meta
        bookkeeping are not exposed — only name and id, enough to answer
        "which tags do you use?" and to feed ``search_chats`` prefixes.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_tags(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading your tags…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_tags(self, request: Any, __user__: Optional[dict] = None,
                        output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_CHATS_ALL_TAGS)
        items, _total = self._extract_items(json.loads(body))
        tags = [
            {"id": t.get("id"), "name": t.get("name")}
            for t in items
            if isinstance(t, dict)
        ]
        return self._ok({"count": len(tags), "tags": tags}, "tags", output_format=output_format)

    async def get_chat_metadata(self, chat_id: str, __request__: Any = None,
                                 __user__: dict = None,
                                 __event_emitter__: Any = None) -> str:
        """Get the organization metadata of one chat (no message content).

        Returns id, title, message count, models, tags, folder, pinned/
        archived state, share id and dates. No message content in any
        output format — this is the light "chat data" query.

        :param chat_id: the chat's UUID.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_chat_metadata(chat_id, __request__, __user__=__user__,
                                    output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading chat metadata…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_chat_metadata(self, chat_id: Any, request: Any = None,
                                 __user__: Optional[dict] = None,
                                 output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        chat_id = self._require_id(chat_id, "chat_id")
        payload, _ = await self._chat_metadata_payload(token, chat_id)
        return self._ok(payload, "chat_metadata", output_format=output_format)

    async def get_chat_summary(self, chat_id: str, __request__: Any = None,
                                __user__: dict = None,
                                __event_emitter__: Any = None) -> str:
        """Get a compact summary of one chat: metadata plus first and last messages.

        Returns organization metadata (message count, models, tags, folder,
        dates) and a short markdown snippet of the main branch: the first
        and last DEFAULT_SNIPPET_HEAD/TAIL messages (fixed at 3), with an
        ellipsis line for the middle. It never dumps the full conversation.

        :param chat_id: the chat's UUID.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_chat_summary(chat_id, __request__, __user__=__user__,
                                   output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading chat…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_chat_summary(self, chat_id: Any, request: Any = None,
                                __user__: Optional[dict] = None,
                                output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        chat_id = self._require_id(chat_id, "chat_id")
        head = DEFAULT_SNIPPET_HEAD
        tail = DEFAULT_SNIPPET_TAIL
        payload, raw = await self._chat_metadata_payload(token, chat_id)
        branch = self._main_branch_messages(raw)
        with_text = [m for m in branch if self._message_text(m)]
        count = payload["message_count"]
        head_msgs = with_text[:head]
        tail_msgs = with_text[count - tail:] if count > head + tail else []
        skipped = max(0, count - head - tail)
        # The head/tail snippet is part of the summary (markdown and json),
        # never the full conversation.
        payload["head"] = [
            {"role": m.get("role"), "text": self._normalize_snippet_text(self._message_text(m))}
            for m in head_msgs
        ]
        payload["tail"] = [
            {"role": m.get("role"), "text": self._normalize_snippet_text(self._message_text(m))}
            for m in tail_msgs
        ]
        payload["skipped"] = skipped
        return self._ok(payload, "chat", output_format=output_format)

    async def _chat_metadata_payload(self, token: str, chat_id: str) -> tuple:
        """Fetch one chat and build the shared organization-metadata payload.

        Returns (payload, raw) so callers can extend it (summary adds the
        head/tail snippet). Field whitelist: the full ChatResponse carries
        bookkeeping noise (user_id, tasks, summary, last_read_at, the raw
        meta dict) that the model does not need — no raw body ever reaches
        _ok (pinned by the static tripwire).
        """
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHAT.format(chat_id=chat_id)
        )
        raw = json.loads(body)
        chat_obj = raw.get("chat") or {}
        meta = raw.get("meta") or {}
        folder_id = raw.get("folder_id")
        folder_name = await self._folder_name(token, folder_id)
        branch = self._main_branch_messages(raw)
        count = len([m for m in branch if self._message_text(m)])
        payload = {
            "id": raw.get("id"),
            "title": raw.get("title"),
            "message_count": count,
            "models": chat_obj.get("models") or [],
            "tags": meta.get("tags") or [],
            "folder_id": folder_id,
            "folder_name": folder_name,
            "pinned": raw.get("pinned"),
            "archived": raw.get("archived"),
            "share_id": raw.get("share_id"),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
        }
        return payload, raw

    async def search_chats(self, text: str, __request__: Any = None, __user__: dict = None,
                           __event_emitter__: Any = None) -> str:
        """Search the requesting user's chats for a text fragment.

        The backend supports the UI filter prefixes, all server-side:
        ``tag:name``, ``folder:name``, ``pinned:true/false``,
        ``archived:true/false``, ``shared:true/false`` and ``tag:none``
        (chats with no tags) — e.g. ``search_chats("tag:budget")``. Results
        include a per-chat ``snippet`` of the matched message when present.
        All prefixes combine with the text as AND (server-side scope
        limiters, verified live 2026-08-21) — ``"foo tag:bar"`` matches only
        chats matching ``foo`` that also carry the tag ``bar``.

        Backend notes: (1) a LONE ``tag:`` query with zero matches triggers
        the backend's orphan-tag cleanup — the tag's catalog entry is
        deleted (intended behavior; per-chat tags are untouched); (2) search
        excludes archived chats, so a tag used only on archived chats is
        cleaned here while ``get_chats(tag=...)`` still sees it.

        :param text: the search term (matched against chat titles and messages; UI filter prefixes accepted).
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

    async def get_chat_stats(self, chat_id: str, __request__: Any = None,
                             __user__: dict = None,
                             __event_emitter__: Any = None) -> str:
        """Get usage statistics for one chat: message counts, models, tags, averages.

        Reads the EXPERIMENTAL ``/api/v1/chats/stats/usage`` route, filtered
        client-side by id — it may change or disappear in a future release;
        a failure of that route produces a clean error and never affects
        other methods.

        Metric semantics (root-caused 2026-08-21, Iteration 9 task 9.3):
        ``message_count`` counts every step on the main branch (including
        textless reasoning steps); ``last_message_at`` is the last message's
        timestamp and may differ from ``updated_at`` (the chat-row timestamp,
        moved by renames/edits); ``history_*`` counts cover the whole message
        tree. The two content-length averages are RECOMPUTED from the chat's
        real message text (the EXPERIMENTAL route reports 0.0 for assistant
        messages on v0.10.2 — a backend bug); the raw route values remain
        available under ``…_backend``.

        :param chat_id: the chat's UUID.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_chat_stats(chat_id, __request__, __user__=__user__,
                                 output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading chat usage stats…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_chat_stats(self, chat_id: Any, request: Any = None,
                              __user__: Optional[dict] = None,
                              output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        chat_id = self._require_id(chat_id, "chat_id")
        # The stats route IGNORES pageSize — it always returns up to 50 rows
        # per page, in irregular sizes (verified live 2026-08-20: pages of
        # 50/49/49 then an empty page while declaring total 149), so short
        # pages must NOT stop the iteration; only an empty page or the
        # declared total ends it, bounded by MAX_PAGES.
        items, _total = await self._fetch_all_pages(
            token, _ROUTE_CHATS_STATS_USAGE, page_size=DEFAULT_PAGE_SIZE,
            short_page_stops=False,
        )
        found = next(
            (s for s in items if isinstance(s, dict) and s.get("id") == chat_id),
            None,
        )
        if found is None:
            raise ToolError(
                f"No usage statistics found for chat {chat_id} (the "
                "stats/usage route is EXPERIMENTAL and may not cover "
                "every chat)."
            )
        stats = {
            k: found.get(k)
            for k in (
                "id", "message_count", "models", "tags",
                "history_message_count", "history_user_message_count",
                "history_assistant_message_count",
                "average_response_time",
                "average_user_message_content_length",
                "average_assistant_message_content_length",
                "last_message_at", "created_at", "updated_at",
            )
            if k in found
        }
        # The EXPERIMENTAL stats/usage route computes the length averages from
        # the plain ``content`` string — empty for every v0.10.2 assistant
        # message (text lives in output[].content[].text), so the assistant
        # average is ALWAYS 0.0 regardless of real text (root-caused
        # 2026-08-21, Iteration 9 task 9.3). Recompute both averages from the
        # ChatResponse using the real message text; the raw backend values
        # stay available under ``…_backend``. A fetch failure degrades to the
        # backend values (never an error — this is enrichment, not the query).
        lengths = await self._role_content_lengths(token, chat_id)
        for role, key in (
            ("user", "average_user_message_content_length"),
            ("assistant", "average_assistant_message_content_length"),
        ):
            if key in stats:
                stats[f"{key}_backend"] = stats[key]
            if role in lengths:  # {} (fetch failed) → keep the backend values
                stats[key] = lengths[role]
        return self._ok(stats, "chat_stats", output_format=output_format)

    async def _role_content_lengths(self, token: str, chat_id: str) -> dict:
        """Average real text length per role on the main branch (best-effort).

        Returns ``{"user": float|None, "assistant": float|None}`` on success
        (None = the role has no text-bearing messages, nothing to average —
        never 0.0, which would masquerade as a measurement) or ``{}`` when
        the chat fetch failed, so the caller keeps the backend values. This
        enrichment must never break the stats call.
        """
        try:
            _status, _ct, body = await self._api_get_json(
                token, _ROUTE_CHAT.format(chat_id=chat_id)
            )
            raw = json.loads(body)
        except Exception:
            return {}
        branch = self._main_branch_messages(raw)
        by_role: dict[str, list[int]] = {}
        for m in branch:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            text = self._message_text(m)
            if role in ("user", "assistant") and text:
                by_role.setdefault(role, []).append(len(text))
        out: dict[str, Optional[float]] = {"user": None, "assistant": None}
        for role, lengths in by_role.items():
            if lengths:
                out[role] = sum(lengths) / len(lengths)
        return out

    async def get_folders(self, __request__: Any = None, __user__: dict = None,
                          __event_emitter__: Any = None) -> str:
        """List the folders the requesting user has created (name, id, parent).

        Reads ``GET /api/v1/folders/`` (trailing slash required). The route
        is gated by the folders feature: on an instance where folders are
        disabled the backend returns 403, which maps to a readable
        Forbidden error.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_folders(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Reading your folders…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_folders(self, request: Any, __user__: Optional[dict] = None,
                           output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_FOLDERS)
        items, _total = self._extract_items(json.loads(body))
        folders = [
            {k: f.get(k)
             for k in ("id", "name", "parent_id", "is_expanded",
                       "created_at", "updated_at")}
            for f in items
            if isinstance(f, dict)
        ]
        return self._ok({"count": len(folders), "folders": folders}, "folders", output_format=output_format)

    async def get_files(
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
            self._get_files(
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

    async def _get_files(self, request: Any, limit: Any = 50, sort_by: Any = "created_at",
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
        name = filename or file_id

        if ct.startswith("image/"):
            # Option B (user decision 2026-08-03): embed the image inline in
            # the message via the ``embeds`` event (FullHeightIframe srcdoc),
            # styled like a snippet — NOT markdown, NOT the files attachment
            # (which sits above the text). The note tells the model the image
            # is already visible so it must not embed/display it again as
            # markdown (mirrors generate_image's contract).
            await self._emit_embeds(
                __event_emitter__, [self._image_embed_html(file_id, name)]
            )
            # Iteration 9 task 9.2: enrich with header metadata via Pillow
            # (resolution + color depth; lazy open, never pixel data).
            # Best-effort: a bad header or missing Pillow → no extra fields.
            info = self._image_header_info(body)
            payload = {
                "file_id": file_id,
                "filename": filename,
                "content_type": ct,
                "size": size,
                "note": f"Image ({ct}, {size} bytes) is embedded in the "
                        "conversation and visible to the user. Do NOT embed or "
                        "display it again as markdown.",
            }
            if info:
                payload.update(info)
            return self._ok(payload, "file_binary", output_format=output_format)

        # Text and generic binaries: attach via the ``files`` event (UI
        # download chip).
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

    def _image_embed_html(self, file_id: str, name: str) -> str:
        """Build the HTML fragment that embeds an image inline in the message.

        Rendered by the frontend's ``embeds`` block (FullHeightIframe with
        srcdoc — see ResponseMessage.svelte / FullHeightIframe.svelte in
        v0.10.2). The srcdoc iframe inherits the parent document's base URL,
        so the relative ``/api/v1/files/{id}/content`` path resolves against
        the app origin and loads with the session cookie (no token in the
        URL). Styled as an inline preview: contained, rounded, capped height.
        """
        name_html = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<div style="max-width:100%;padding:4px;">'
            f'<img src="/api/v1/files/{file_id}/content" '
            f'alt="{name_html}" '
            f'style="max-width:100%;max-height:320px;object-fit:contain;'
            f'border-radius:8px;display:block;"/>'
            f'</div>'
        )

    async def _emit_embeds(self, emitter: Optional[Any], embeds: list) -> None:
        """Emit an ``embeds`` event rendering HTML inline in the message.

        Native Open WebUI event (verified in backend/open_webui/socket/main.py
        and Chat.svelte / ResponseMessage.svelte in v0.10.2): the backend
        persists it into the assistant message and re-broadcasts it live.
        Best-effort: a failed UI event must never break the tool call.
        """
        if emitter is None or not embeds:
            return
        try:
            await emitter({"type": "embeds", "data": {"embeds": embeds}})
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

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

    async def get_prompts(self, __request__: Any = None, __user__: dict = None,
                          __event_emitter__: Any = None) -> str:
        """List the requesting user's custom prompts (command, name, content)."""
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_prompts(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your prompts…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_prompts(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_PROMPTS)
        items, _total = self._extract_items(json.loads(body))
        prompts = [
            {k: item.get(k) for k in ("id", "command", "name", "content")}
            for item in items
            if isinstance(item, dict)
        ]
        return self._ok({"count": len(prompts), "prompts": prompts}, "prompts", output_format=output_format)

    async def get_tools(self, __request__: Any = None, __user__: dict = None,
                        __event_emitter__: Any = None) -> str:
        """List the tools available to the requesting user (id, name, description)."""
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_tools(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your tools…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_tools(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
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

    async def get_skills(self, __request__: Any = None, __user__: dict = None,
                         __event_emitter__: Any = None) -> str:
        """List the skills available to the requesting user (id, name, description, active state).

        Skills are workspace resources like tools and prompts: the list includes
        the user's own skills and skills shared with them via access grants.
        The skill's content (its instructions) is not included here to keep the
        listing light — use get_skill() for the full detail.
        """
        output_format = self._resolve_output_format(__user__)
        return await self._run(
            self._get_skills(__request__, __user__=__user__, output_format=output_format),
            output_format=output_format,
            request=__request__,
            emitter=__event_emitter__,
            action="Listing your skills…",
            verbose=self._resolve_verbose(__user__),
        )

    async def _get_skills(self, request: Any, __user__: Optional[dict] = None, output_format: Optional[str] = None) -> str:
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
