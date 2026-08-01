"""
title: Open WebUI Meta-Tool
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Queries Open WebUI's own internal API to answer questions about the requesting user's data (chats, files, prompts, tools, models, knowledge). Authenticates automatically with the requesting user's token — no credentials to configure. Read-only, allowlisted endpoints only.
required_open_webui_version: 0.9.0
requirements: httpx
version: 0.2.0
licence: MIT
"""

import json
import logging
import os
import re
from typing import Any, Optional

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

# ── Canonical route paths (allowlist) ─────────────────────────────────
# Trailing slashes are SIGNIFICANT in this deployment (v0.10.2):
#   - Listings (auths, chats, files, prompts, tools, knowledge, users)
#     are registered with a trailing slash:  GET /api/v1/<resource>/
#     -> WITHOUT the slash they fall into the SPA HTML catch-all (HTTP 200).
#   - Sub-resources (search, pinned, shared, {id}, …) and /api/models are
#     registered WITHOUT a trailing slash.
# Verified live against the instance (2026-08-01): see PLAN.md §iteration-1.
_ROUTE_PROFILE = "/api/v1/auths/"
_ROUTE_MODELS = "/api/models"
_ROUTE_CHATS = "/api/v1/chats/"
_ROUTE_CHAT = "/api/v1/chats/{chat_id}"
_ROUTE_CHATS_SEARCH = "/api/v1/chats/search"
_ROUTE_CHATS_SHARED = "/api/v1/chats/shared"
_ROUTE_CHATS_PINNED = "/api/v1/chats/pinned"
_ROUTE_FILES = "/api/v1/files/"
_ROUTE_FILE_CONTENT = "/api/v1/files/{file_id}/content"
_ROUTE_PROMPTS = "/api/v1/prompts/"
_ROUTE_TOOLS = "/api/v1/tools/"
_ROUTE_KNOWLEDGE = "/api/v1/knowledge/"

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

    def __init__(self):
        self.valves = self.Valves()
        # Test seams — never set in production.
        self._transport: Optional[httpx.AsyncBaseTransport] = None
        self._base_url_override: Optional[str] = None

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
        state = getattr(request, "state", None) if request is not None else None
        token = getattr(state, "token", None) if state is not None else None
        if token is None:
            raise ToolError(
                "No authentication token available. This tool must run inside an "
                "authenticated Open WebUI session or API request (it reads "
                "__request__.state.token)."
            )
        # v0.10.2: HTTPAuthorizationCredentials object with .credentials;
        # a plain string is accepted for robustness across versions/tests.
        if isinstance(token, str):
            credentials = token
        else:
            credentials = getattr(token, "credentials", None)
        if not isinstance(credentials, str) or not credentials.strip():
            raise ToolError(
                "No authentication token available. This tool must run inside an "
                "authenticated Open WebUI session or API request (it reads "
                "__request__.state.token)."
            )
        return credentials.strip()

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
                     accept: str = "application/json") -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "owui_meta/0.1.0 (Open WebUI internal tool)",
        }
        async with self._client() as client:
            return await client.get(url, headers=headers, params=params)

    async def _fetch_with_retry(self, token: str, path: str,
                                params: Optional[dict] = None,
                                accept: str = "application/json") -> httpx.Response:
        """GET an allowlisted route, retrying once against the fallback URL.

        Retry only on transport errors (DNS/connection/timeout — DESIGN §4.3).
        Never retries on API 4xx/5xx responses.
        """
        base = await self._resolve_base_url()
        primary_url = base + path
        try:
            resp = await self._fetch(primary_url, token, params, accept)
        except httpx.RequestError as exc:
            fallback = self.valves.fallback_base_url.rstrip("/")
            if fallback and fallback != base:
                try:
                    resp = await self._fetch(fallback + path, token, params, accept)
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

    # ──────────────────────────────────────────────
    #  Output formatting (DESIGN §8.6.5, §7.2)
    # ──────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        max_chars = self.valves.max_response_chars
        if len(text) <= max_chars:
            return text
        note = f"\n… [truncated from {len(text)} to {max_chars} characters]"
        return text[: max(0, max_chars - len(note))] + note

    def _ok(self, payload: Any) -> str:
        return self._truncate(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def _error(self, message: str) -> str:
        return json.dumps({"error": message}, ensure_ascii=False, indent=2)

    async def _run(self, coro: Any) -> str:
        """Execute a private implementation, converting failures to safe JSON."""
        try:
            return await coro
        except ToolError as exc:
            return self._error(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            # The token is never part of an exception message (it travels in a
            # header only), so logging the traceback cannot leak it.
            logger.exception("owui_meta: unexpected error")
            return self._error(
                f"Unexpected internal error ({type(exc).__name__}); see server logs."
            )

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

    async def get_my_profile(self, __request__: Any = None) -> str:
        """Get the requesting user's own profile: id, name, email, role and permissions.

        Use this to learn who you are talking to and what they are allowed to do.
        """
        return await self._run(self._get_my_profile(__request__))

    async def _get_my_profile(self, request: Any) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_PROFILE)
        return self._ok(json.loads(body))

    async def get_models(self, __request__: Any = None) -> str:
        """List the models available to the requesting user (id, name, owner).

        Only lightweight metadata is returned, not the full model definitions.
        """
        return await self._run(self._get_models(__request__))

    async def _get_models(self, request: Any) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_MODELS)
        payload = json.loads(body)
        items = payload.get("data") if isinstance(payload, dict) else payload
        models = self._summarize_models(items if isinstance(items, list) else [])
        return self._ok({"count": len(models), "models": models})

    async def get_my_chats(self, limit: int = 10, __request__: Any = None) -> str:
        """List the requesting user's recent chats (id, title, dates).

        :param limit: how many chats to return (default 10, max 100).
        """
        return await self._run(self._get_my_chats(limit, __request__))

    async def _get_my_chats(self, limit: Any, request: Any) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS, {"pageSize": limit}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"count": len(chats), "total": total, "chats": chats})

    async def get_chat(self, chat_id: str, __request__: Any = None) -> str:
        """Get the full content of one chat (all its messages) by id.

        :param chat_id: the chat's UUID.
        """
        return await self._run(self._get_chat(chat_id, __request__))

    async def _get_chat(self, chat_id: Any, request: Any) -> str:
        token = self._require_token(request)
        chat_id = self._require_id(chat_id, "chat_id")
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHAT.format(chat_id=chat_id)
        )
        return self._ok(json.loads(body))

    async def search_chats(self, text: str, __request__: Any = None) -> str:
        """Search the requesting user's chats for a text fragment.

        :param text: the search term (matched against chat titles and messages).
        """
        return await self._run(self._search_chats(text, __request__))

    async def _search_chats(self, text: Any, request: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ToolError("search_chats requires a non-empty 'text' parameter.")
        text = text.strip()[:200]
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_SEARCH, {"text": text}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"query": text, "count": len(chats), "total": total, "chats": chats})

    async def get_shared_chats(self, limit: int = 10, __request__: Any = None) -> str:
        """List chats the requesting user has shared with others.

        :param limit: how many chats to return (default 10, max 100).
        """
        return await self._run(self._get_shared_chats(limit, __request__))

    async def _get_shared_chats(self, limit: Any, request: Any) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_SHARED, {"pageSize": limit}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"count": len(chats), "total": total, "chats": chats})

    async def get_pinned_chats(self, limit: int = 10, __request__: Any = None) -> str:
        """List chats the requesting user has pinned.

        :param limit: how many chats to return (default 10, max 100).
        """
        return await self._run(self._get_pinned_chats(limit, __request__))

    async def _get_pinned_chats(self, limit: Any, request: Any) -> str:
        token = self._require_token(request)
        limit = self._coerce_limit(limit)
        _status, _ct, body = await self._api_get_json(
            token, _ROUTE_CHATS_PINNED, {"pageSize": limit}
        )
        items, total = self._extract_items(json.loads(body))
        chats = self._summarize_chats(items)
        return self._ok({"count": len(chats), "total": total, "chats": chats})

    async def get_my_files(self, __request__: Any = None) -> str:
        """List the requesting user's files with metadata.

        Returns filename, content type, size (bytes), dates and the origin
        chat/message when the file was generated by a chat. Binary content is
        not fetched — use get_file_content() to read a specific file.
        """
        return await self._run(self._get_my_files(__request__))

    async def _get_my_files(self, request: Any) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_FILES)
        items, total = self._extract_items(json.loads(body))
        files = self._summarize_files(items)
        return self._ok({"count": len(files), "total": total, "files": files})

    async def get_file_content(self, file_id: str, __request__: Any = None) -> str:
        """Read a file's content by id.

        Text files return their content (truncated to max_response_chars).
        Binary files (images, PDFs, ...) return metadata only, with a note.

        :param file_id: the file's UUID.
        """
        return await self._run(self._get_file_content(file_id, __request__))

    async def _get_file_content(self, file_id: Any, request: Any) -> str:
        token = self._require_token(request)
        file_id = self._require_id(file_id, "file_id")
        path = _ROUTE_FILE_CONTENT.format(file_id=file_id)
        _status, content_type, body = await self._api_get_raw(token, path)
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("text/") or ct in _TEXT_CONTENT_TYPES or ct.endswith(("+json", "+xml")):
            text = body.decode("utf-8", errors="replace")
            return self._ok({
                "file_id": file_id,
                "content_type": ct,
                "size": len(body),
                "content": text,
            })
        return self._ok({
            "file_id": file_id,
            "content_type": ct,
            "size": len(body),
            "note": f"Binary content ({ct}) is not returned inline. Use get_my_files() "
                    "for metadata (size, dates, origin).",
        })

    async def get_my_prompts(self, __request__: Any = None) -> str:
        """List the requesting user's custom prompts (command, name, content)."""
        return await self._run(self._get_my_prompts(__request__))

    async def _get_my_prompts(self, request: Any) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_PROMPTS)
        items, _total = self._extract_items(json.loads(body))
        prompts = [
            {k: item.get(k) for k in ("id", "command", "name", "content")}
            for item in items
            if isinstance(item, dict)
        ]
        return self._ok({"count": len(prompts), "prompts": prompts})

    async def get_my_tools(self, __request__: Any = None) -> str:
        """List the tools available to the requesting user (id, name, description)."""
        return await self._run(self._get_my_tools(__request__))

    async def _get_my_tools(self, request: Any) -> str:
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
        return self._ok({"count": len(tools), "tools": tools})

    async def get_knowledge_bases(self, __request__: Any = None) -> str:
        """List the knowledge bases available to the requesting user (id, name, description)."""
        return await self._run(self._get_knowledge_bases(__request__))

    async def _get_knowledge_bases(self, request: Any) -> str:
        token = self._require_token(request)
        _status, _ct, body = await self._api_get_json(token, _ROUTE_KNOWLEDGE)
        items, total = self._extract_items(json.loads(body))
        knowledge = [
            {k: item.get(k) for k in ("id", "name", "description", "created_at")}
            for item in items
            if isinstance(item, dict)
        ]
        return self._ok({"count": len(knowledge), "total": total, "knowledge": knowledge})
