"""
title: Smart Fetch URL
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Fetches URLs with TLS fingerprinting to avoid blocks, returns clean content with metadata.
required_open_webui_version: 0.9.0
requirements: curl_cffi>=0.7.0, trafilatura, selectolax
version: 0.9.0
licence: MIT
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Browser profiles & formats
# ──────────────────────────────────────────────

VALID_FORMATS = frozenset({"skimmd", "markdown", "html", "txt", "json", "raw"})
VALID_BROWSERS = frozenset({"firefox", "chrome", "edge", "safari"})
DEFAULT_BROWSER = "firefox"
DEFAULT_MAX_CHARS = 16_384
DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_BATCH_CONCURRENCY = 8
DEFAULT_BATCH_REQUESTS_PER_SEC = 10
THREAD_POOL_WORKERS = 8
THREAD_TIMEOUT_SEC = 5
MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK = 30
GLOBAL_OPERATION_TIMEOUT_SEC = 30
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
DEFAULT_RAW_ACCEPT = "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,text/markdown;q=0.8,text/plain;q=0.8,*/*;q=0.7"
DEFAULT_JSON_ACCEPT = "application/json,text/json,application/ld+json;q=0.9,text/plain;q=0.8,*/*;q=0.7"


class _RateLimiter:
    """Sliding-window rate limiter for batch fetches."""
    def __init__(self, rate: float):
        self.rate = rate
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, (1.0 / self.rate) - (now - self._last))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class Tools:

    class Valves(BaseModel):
        """Configuration settings for this tool (admin-set, server-side)."""

        max_chars: int = Field(
            DEFAULT_MAX_CHARS,
            description="Default maximum characters to return",
        )
        timeout_ms: int = Field(
            DEFAULT_TIMEOUT_MS,
            description="Default request timeout in milliseconds",
        )
        default_browser: str = Field(
            DEFAULT_BROWSER,
            description="Browser fingerprint profile",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "firefox", "label": "Firefox"},
                        {"value": "chrome", "label": "Chrome"},
                        {"value": "edge", "label": "Edge"},
                        {"value": "safari", "label": "Safari"},
                    ],
                }
            },
        )
        batch_concurrency: int = Field(
            DEFAULT_BATCH_CONCURRENCY,
            description="Default concurrency for batch fetches",
        )
        requests_per_second: int = Field(
            DEFAULT_BATCH_REQUESTS_PER_SEC,
            description="Max requests per second in batch fetches",
        )
        verbose: bool = Field(
            False,
            description="Emit detailed status events during fetch",
        )
        proxy: Optional[str] = Field(
            None,
            description="Proxy URL for all requests (http://user:pass@host:port or socks5://host:port). Admin-only.",
        )

    class UserValves(BaseModel):
        """Per-user overrides for fetch settings. Configured from the chat session."""

        max_chars: Optional[int] = Field(
            None,
            description="Maximum characters to return (overrides admin setting)",
        )
        timeout_ms: Optional[int] = Field(
            None,
            description="Request timeout in milliseconds (overrides admin setting)",
        )
        default_browser: str = Field(
            "inherit",
            description="Browser fingerprint profile. Choose a browser or 'Inherit from admin'.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "inherit", "label": "— Inherit from admin —"},
                        {"value": "firefox", "label": "Firefox"},
                        {"value": "chrome", "label": "Chrome"},
                        {"value": "edge", "label": "Edge"},
                        {"value": "safari", "label": "Safari"},
                    ],
                }
            },
        )
        batch_concurrency: Optional[int] = Field(
            None,
            description="Concurrency for batch fetches (overrides admin setting)",
        )
        verbose: Optional[bool] = Field(
            None,
            description="Emit detailed status events during fetch (overrides admin setting)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cffi_available = None  # lazy check
        self._thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._fallback_note: Optional[str] = None

    def _close(self):
        """Shut down the thread pool explicitly.

        Call this when the Tools instance is no longer needed
        (e.g. from the harness lifecycle hooks) to ensure no
        threads outlive their owner.
        """
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
            self._thread_pool = None

    def _get_thread_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._thread_pool is None:
            self._thread_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=THREAD_POOL_WORKERS, thread_name_prefix="smart_fetch"
            )
        return self._thread_pool

    async def _run_in_thread(self, func, timeout: float = THREAD_TIMEOUT_SEC):
        loop = asyncio.get_running_loop()
        pool = self._get_thread_pool()
        fut = loop.run_in_executor(pool, func)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.CancelledError:
            logger.warning(
                "Threaded operation cancelled — thread continues in pool until "
                "current work completes (fut.cancel() cannot kill running threads)"
            )
            fut.cancel()
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Threaded operation timed out after %.1fs — thread continues in pool "
                "until current work completes",
                timeout,
            )
            fut.cancel()
            raise

    def __del__(self):
        # Best-effort cleanup — ``_close()`` is the reliable path.
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
            self._thread_pool = None

    # ──────────────────────────────────────────────
    #  Core tool method
    # ──────────────────────────────────────────────

    async def smart_fetch_url(
        self,
        urls: list[str],
        format: Literal["skimmd", "markdown", "html", "txt", "json", "raw"] = "skimmd",
        max_chars: Optional[int] = None,
        timeout_ms: Optional[int] = None,
        include_replies: bool = False,
        concurrency: Optional[int] = None,
        __event_emitter__: Optional[Any] = None,
        __user__: Optional[Any] = None,
    ) -> str:
        """
        Fetch one or more URLs with browser-grade TLS fingerprinting and clean content extraction.

        Handles single URLs and batches with the same interface — pass a list
        with one element for a single fetch, or multiple URLs for concurrent
        batch fetching.

        :param urls: URL(s) to fetch (http/https only).  A single-item list
                     runs the full single-fetch pipeline; multiple URLs are
                     fetched concurrently with bounded concurrency.
        :param format: Output format: "skimmd" (default, cleaned MD)
                       "markdown" (MD), "html" (cleaned HTML), "txt" (plain text),
                       "json" (structured), "raw" (full server response)
        :param max_chars: Max response chars
        :param timeout_ms: Timeout in ms per request
        :param include_replies: Include replies/comments from feed/forum sites
        :param concurrency: Max concurrent fetches for batch (default: 8)
        :param __event_emitter__: Internal — for UI progress updates
        :param __user__: Internal — for user-specific valve overrides
        :returns: Extracted content with metadata header (single) or
                  labeled results separated by ``---`` lines (batch)
        """

        uv = self._get_user_valves(__user__)
        max_chars = max_chars or (uv.max_chars if uv else None) or self.valves.max_chars
        timeout_ms = timeout_ms or (uv.timeout_ms if uv else None) or self.valves.timeout_ms
        uv_browser = uv.default_browser if uv else "inherit"
        browser = self.valves.default_browser if uv_browser == "inherit" else uv_browser

        # Validate browser (safety net — json_schema_extra should prevent invalid values)
        if browser not in VALID_BROWSERS:
            return f"Error: Invalid browser '{browser}'. Must be one of: {', '.join(sorted(VALID_BROWSERS))}."

        concurrency = concurrency or (uv.batch_concurrency if uv else None) or self.valves.batch_concurrency
        uv_verbose = uv.verbose if uv else None
        verbose = uv_verbose if uv_verbose is not None else self.valves.verbose

        # Validate
        if format not in VALID_FORMATS:
            return f"Error: Invalid format '{format}'. Must be one of: {', '.join(sorted(VALID_FORMATS))}."
        if not urls or not isinstance(urls, list):
            return "Error: A list of URLs is required."
        if len(urls) > 50:
            return f"Error: Maximum 50 URLs per batch, got {len(urls)}."

        # Validate and clean each URL
        cleaned: list[str] = []
        for u in urls:
            if not u or not isinstance(u, str) or not u.strip():
                return "Error: Each URL must be a non-empty string."
            u = u.strip()
            if not u.startswith(("http://", "https://")):
                return f"Error: Invalid URL protocol. Only http/https are supported: {u}"
            cleaned.append(u)
        urls = cleaned

        # ── Batch path ──────────────────────────────────────────────
        if len(urls) > 1:
            concurrency = max(1, min(concurrency, 50))
            requests_per_second = max(1, self.valves.requests_per_second)

            semaphore = asyncio.Semaphore(concurrency)
            rate_limiter = _RateLimiter(requests_per_second)

            async def fetch_one(index: int, single_url: str) -> str:
                async with semaphore:
                    await rate_limiter.acquire()
                    try:
                        _start = time.monotonic()
                        await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] 🌐 {single_url}", done=False)
                        result = await asyncio.wait_for(
                            self._execute_fetch(
                                url=single_url,
                                browser=browser,
                                timeout_ms=timeout_ms,
                                format=format,
                                max_chars=max_chars,
                                include_replies=include_replies,
                                verbose=verbose,
                                __event_emitter__=None,  # suppress per-item events
                                _start_time=_start,
                            ),
                            timeout=GLOBAL_OPERATION_TIMEOUT_SEC,
                        )
                        await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] ✅ {single_url}", done=False)
                        return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{result}\n\n---\n"
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] ❌ {single_url}", done=False)
                        err_result = self._format_output(
                            url=single_url, final_url=single_url, title="", author="",
                            site="", language="", published="", content="",
                            format=format, word_count=0, browser=browser, status_code=0,
                            error={"error_type": "timeout", "message": "The operation timed out"},
                        )
                        return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{err_result}\n\n---\n"
                    except Exception as e:
                        await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] ❌ {single_url}", done=False)
                        error_data = self._format_error(e, single_url)
                        err_result = self._format_output(
                            url=single_url, final_url=single_url, title="", author="",
                            site="", language="", published="", content="",
                            format=format, word_count=0, browser=browser, status_code=0,
                            error=error_data,
                        )
                        return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{err_result}\n\n---\n"

            await self._emit_status(__event_emitter__, f"[0/{len(urls)}] Fetching {len(urls)} URLs…", done=False)

            tasks = [fetch_one(i, u) for i, u in enumerate(urls)]
            try:
                results = await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                logger.warning("Batch fetch cancelled by user (Stop button)")
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await self._emit_status(__event_emitter__, f"❌ Batch cancelled", done=True)
                raise

            await self._emit_sources(__event_emitter__, urls)
            await self._emit_status(__event_emitter__, f"✅ Fetched {len(urls)} URLs", done=True)
            return "".join(results)

        # ── Single URL path ─────────────────────────────────────────
        url = urls[0]

        try:
            _start_time = time.monotonic()
            await self._emit_status(__event_emitter__, f"🌐 {url}", done=False)

            result = await asyncio.wait_for(
                self._execute_fetch(
                    url=url,
                    browser=browser,
                    timeout_ms=timeout_ms,
                    format=format,
                    max_chars=max_chars,
                    include_replies=include_replies,
                    verbose=verbose,
                    __event_emitter__=__event_emitter__,
                    _start_time=_start_time,
                ),
                timeout=GLOBAL_OPERATION_TIMEOUT_SEC,
            )
            return result

        except asyncio.TimeoutError:
            await self._emit_status(__event_emitter__, f"❌ {url}", done=True)
            return self._format_output(
                url=url, final_url=url, title="", author="", site="",
                language="", published="", content="", format=format,
                word_count=0, browser=browser, status_code=0,
                error={"error_type": "timeout", "message": "The operation timed out"},
            )

        except asyncio.CancelledError:
            await self._emit_status(__event_emitter__, f"❌ {url}", done=True)
            raise

        except Exception as e:
            error_data = self._format_error(e, url)
            await self._emit_status(__event_emitter__, f"❌ {url}", done=True)
            logger.exception(f"smart_fetch_url failed for {url}")
            return self._format_output(
                url=url, final_url=url, title="", author="", site="",
                language="", published="", content="", format=format,
                word_count=0, browser=browser, status_code=0,
                error=error_data,
            )

    # ──────────────────────────────────────────────
    #  Internal: full fetch+extract pipeline (runs under global timeout)
    # ──────────────────────────────────────────────

    async def _execute_fetch(
        self,
        url: str,
        browser: str,
        timeout_ms: int,
        format: str,
        max_chars: int,
        include_replies: bool,
        verbose: bool,
        __event_emitter__: Optional[Any],
        _start_time: float,
    ) -> str:
        """Execute the full fetch, extract, and format pipeline.

        Called from :meth:`smart_fetch_url` wrapped in
        ``asyncio.wait_for(timeout=GLOBAL_OPERATION_TIMEOUT_SEC)``.
        """
        _defense_timeout = timeout_ms / 1000
        (raw_html, final_url, status_code, content_type, resp_headers, raw_bytes) = (
            await asyncio.wait_for(
                self._fetch_with_fingerprint(
                url=url,
                browser=browser,
                timeout_ms=timeout_ms,
                proxy=self.valves.proxy,
                format=format,
            ),
                timeout=_defense_timeout,
            )
        )
        fallback_note = self._fallback_note
        self._fallback_note = None

        # Step 2: Route by Content-Type
        #
        # 2a — extractable documents (PDF, DOCX, …).  Download bytes
        #      and extract text with a format-specific parser.
        # 2b — true binary (images, video, fonts, …).  Show metadata
        #      only — binary data in the LLM context is harmful.
        # 2c — text / HTML / JSON / unknown.  Continue to the
        #      normal trafilatura / raw pipeline below.

        if self._is_extractable_document(content_type):
            extracted = await self._extract_document_content(
                url=final_url,
                content_type=content_type,
                raw_bytes=raw_bytes,
            )
            del raw_bytes  # free immediately — large docs (50MB+ PDF) must
            # not persist through formatting and event emission.

            content = extracted.get("content", "")
            if max_chars and len(content) > max_chars:
                content = content[:max_chars]
            word_count = extracted.get("word_count", 0)

            result = self._format_output(
                url=url,
                final_url=final_url,
                title=extracted.get("title", ""),
                author=extracted.get("author", ""),
                site=urlparse(final_url).hostname or "",
                language=extracted.get("language", ""),
                published=extracted.get("published", ""),
                content=content,
                format=format,
                word_count=word_count,
                browser=browser,
                status_code=status_code,
                note=fallback_note,
            )
            _elapsed = time.monotonic() - _start_time
            _desc = f"✅ {url}" if not verbose else f"✅ {url} ({word_count}w, {_elapsed:.1f}s)"
            await self._emit_sources(__event_emitter__, [final_url])
            await self._emit_status(__event_emitter__, _desc, done=True)
            return result

        if not self._is_text_content(content_type):
            result = self._format_output(
                url=url,
                final_url=final_url,
                title="",
                author="",
                site=urlparse(final_url).hostname or "",
                language="",
                published="",
                content=f"[Non-text content ({content_type}). Content not displayed to avoid context pollution.]",
                format=format,
                word_count=0,
                browser=browser,
                status_code=status_code,
                note=fallback_note,
            )
            _elapsed = time.monotonic() - _start_time
            await self._emit_sources(__event_emitter__, [final_url])
            await self._emit_status(__event_emitter__, f"✅ {url}", done=True)
            return result

        # Text path: raw bytes no longer needed — drop the binding
        # so the GC can reclaim the body before extraction runs.
        del raw_bytes

        # Step 3: Handle raw format early — return the full server response
        if format == "raw":
            result = self._build_raw_response(
                url=url,
                final_url=final_url,
                raw_html=raw_html,
                status_code=status_code,
                content_type=content_type,
                browser=browser,
            )
            _elapsed = time.monotonic() - _start_time
            await self._emit_sources(__event_emitter__, [final_url])
            await self._emit_status(__event_emitter__, f"✅ {url}", done=True)
            return result

        # Step 3b: skimmd format — skimmed Markdown preserving links and media
        if format == "skimmd":
            content = _skimmd_parse(
                raw_html,
                base_url=final_url,
                strip_external=True,
            )
            word_count = len(content.split()) if content else 0
            if max_chars and len(content) > max_chars:
                content = content[:max_chars]

            result = self._format_output(
                url=url,
                final_url=final_url,
                title=self._extract_title(raw_html),
                author=self._extract_meta(raw_html, "author"),
                site=urlparse(final_url).hostname or "",
                language=self._extract_language(raw_html),
                published=(
                    self._extract_meta(raw_html, "article:published_time")
                    or self._extract_meta(raw_html, "date")
                    or ""
                ),
                content=content,
                format=format,
                word_count=word_count,
                browser=browser,
                status_code=status_code,
                note=fallback_note,
            )
            _elapsed = time.monotonic() - _start_time
            _desc = f"✅ {url}" if not verbose else f"✅ {url} ({word_count}w, {_elapsed:.1f}s)"
            await self._emit_sources(__event_emitter__, [final_url])
            await self._emit_status(__event_emitter__, _desc, done=True)
            return result

        # Step 4: Extract content
        extracted = await self._extract_content(
            raw_html=raw_html,
            url=final_url,
            format=format,
            include_replies=include_replies,
        )

        # Step 5: Alternate content fallback for thin/no content
        alternate_urls = []
        if (
            format != "json"
            and extracted.get("word_count", 0)
            < MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK
        ):
            extracted, alternates_used = await self._try_alternate_fallback(
                raw_html=raw_html,
                url=final_url,
                browser=browser,
                timeout_ms=timeout_ms,
                proxy=self.valves.proxy,
                format=format,
            )
            alternate_urls = alternates_used or []

        # Step 6: Truncate
        content = extracted.get("content", "")
        if max_chars and len(content) > max_chars:
            content = content[:max_chars]

        # Step 7: Build result
        result = self._format_output(
            url=url,
            final_url=final_url,
            title=extracted.get("title", ""),
            author=extracted.get("author", ""),
            site=extracted.get("site", ""),
            language=extracted.get("language", ""),
            published=extracted.get("published", ""),
            content=content,
            format=format,
            word_count=extracted.get("word_count", 0),
            browser=browser,
            status_code=status_code,
            note=fallback_note,
        )

        # Step 8: Emit source events for Open WebUI's Citations component (bottom of message)
        visited_urls = self._collect_visited_urls(url, final_url, alternate_urls)
        word_count = extracted.get("word_count", 0)
        _elapsed = time.monotonic() - _start_time
        _desc = f"✅ {url}" if not verbose else f"✅ {url} ({word_count}w, {_elapsed:.1f}s)"
        await self._emit_sources(__event_emitter__, visited_urls)
        await self._emit_status(__event_emitter__, _desc, done=True)

        return result

    # ──────────────────────────────────────────────
    #  Internal: TLS-fingerprinted fetch
    # ──────────────────────────────────────────────

    async def _fetch_with_fingerprint(
        self,
        url: str,
        browser: str,
        timeout_ms: int,
        proxy: Optional[str] = None,
        format: str = "markdown",
    ) -> tuple[str, str, int, str, dict, Optional[bytes]]:
        """
        Perform the actual HTTP request with TLS fingerprinting.

        Returns: (raw_html, final_url, status_code, content_type,
                  response_headers, raw_bytes)

        raw_bytes is the undecoded response body — needed for binary
        document extraction (PDF, DOCX, etc.).
        """
        resolved_browser = browser

        # Build headers
        request_headers = {}

        # Set default Accept based on format
        accept_key = "Accept"
        if accept_key not in request_headers:
            if format == "json":
                request_headers[accept_key] = DEFAULT_JSON_ACCEPT
            elif format == "raw":
                request_headers[accept_key] = DEFAULT_RAW_ACCEPT
            else:
                request_headers[accept_key] = DEFAULT_ACCEPT

        if "Accept-Language" not in request_headers:
            request_headers["Accept-Language"] = DEFAULT_ACCEPT_LANGUAGE

        # curl_cffi sets its own User-Agent matching the impersonate profile.
        # Only override if the caller explicitly provided one.

        # Try curl_cffi first (async), fall back to httpx
        try:
            return await self._fetch_with_curl_cffi(
                url=url,
                browser=resolved_browser,
                headers=request_headers,
                timeout_ms=timeout_ms,
                proxy=proxy,
            )
        except ImportError:
            msg = "curl_cffi not installed — falling back to httpx (no TLS fingerprinting)"
            logger.warning(msg)
            print(msg, file=__import__("sys").stderr)
            self._fallback_note = "⚠️ Fetched via httpx fallback (curl_cffi not installed, no TLS fingerprinting)"
            return await self._fetch_with_httpx(
                url=url,
                headers=request_headers,
                timeout_ms=timeout_ms,
                proxy=proxy,
            )

    async def _fetch_with_curl_cffi(
        self,
        url: str,
        browser: str,
        headers: dict,
        timeout_ms: int,
        proxy: Optional[str] = None,
    ) -> tuple[str, str, int, str, dict]:
        """Fetch using curl_cffi's async API with TLS fingerprinting."""
        from curl_cffi.requests import AsyncSession

        timeout_sec = timeout_ms / 1000

        proxies_dict = {"http": proxy, "https": proxy} if proxy else None
        async with AsyncSession(
            impersonate=browser,
            proxies=proxies_dict,
        ) as session:
            try:
                resp = await session.get(
                    url,
                    headers=headers,
                    timeout=timeout_sec,
                    allow_redirects=True,
                )
            except asyncio.CancelledError:
                logger.warning("Request cancelled: %s", url)
                raise

            content_type = resp.headers.get("content-type", "") or ""
            resp_headers = dict(resp.headers)
            final_url = str(resp.url)
            status_code = resp.status_code

            # Always grab raw bytes (needed for document extraction).
            raw_bytes: Optional[bytes] = resp.content

            # Only decode to text when the Content-Type warrants it.
            # For PDFs / images / other binary, ``resp.text`` produces
            # garbage that doubles memory for zero value.
            ct_mime = content_type.split(";", 1)[0].strip().lower()
            if ct_mime.startswith("text/") or ct_mime in Tools._TEXT_LIKE_APPLICATION_TYPES:
                raw_html = resp.text
            elif ct_mime in Tools._EXTRACTABLE_DOCUMENT_TYPES:
                raw_html = ""
            else:
                raw_html = ""  # true binary (image, video, …)

            return raw_html, final_url, status_code, content_type, resp_headers, raw_bytes

    async def _fetch_with_httpx(
        self,
        url: str,
        headers: dict,
        timeout_ms: int,
        proxy: Optional[str] = None,
    ) -> tuple[str, str, int, str, dict]:
        """Fallback fetch using httpx (no TLS fingerprinting)."""

        import httpx

        request_kwargs = {
            "headers": headers,
            "follow_redirects": True,
            "timeout": timeout_ms / 1000,
        }

        async with httpx.AsyncClient(proxy=proxy) as client:
            try:
                resp = await client.get(url, **request_kwargs)
            except asyncio.CancelledError:
                logger.warning("Request cancelled: %s", url)
                raise
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "") or ""
            resp_headers = dict(resp.headers)
            final_url = str(resp.url)
            status_code = resp.status_code

            # Always grab raw bytes (needed for document extraction).
            raw_bytes: Optional[bytes] = resp.content

            # Only decode to text when the Content-Type warrants it.
            ct_mime = content_type.split(";", 1)[0].strip().lower()
            if ct_mime.startswith("text/") or ct_mime in Tools._TEXT_LIKE_APPLICATION_TYPES:
                raw_html = resp.text
            elif ct_mime in Tools._EXTRACTABLE_DOCUMENT_TYPES:
                raw_html = ""
            else:
                raw_html = ""  # true binary (image, video, …)

            return raw_html, final_url, status_code, content_type, resp_headers, raw_bytes

    # ──────────────────────────────────────────────
    #  Internal: Content extraction
    # ──────────────────────────────────────────────

    async def _extract_content(
        self,
        raw_html: str,
        url: str,
        format: str,
        include_replies: bool = True,
    ) -> dict:
        """
        Extract clean content from raw HTML.

        Uses trafilatura for primary extraction (similar to Defuddle/Readability).
        Falls back to basic extraction if trafilatura is not available or fails.

        Heavy CPU-bound work (trafilatura, selectolax) is run in a thread
        so the event loop stays responsive and CancelledError can be delivered.
        """
        content = None
        doc = None

        # Guard: trafilatura expects str or bytes — normalise anything else
        if not isinstance(raw_html, (str, bytes)):
            logger.warning(
                "unexpected raw_html type=%s repr=%.200s, coercing to empty string",
                type(raw_html).__name__, repr(raw_html),
            )
            raw_html = ""  # don't str() a list — produces garbage

        # ── Detect content type for routing ─────────────────
        content_category, content_tree = await self._detect_content_type(raw_html)
        logger.info("Content type detected: %s for %s", content_category, url)

        if content_category == "feed":
            # Feed/forum/listing: use selectolax for full content.
            # Trafilatura would only extract the first post.
            content = await self._basic_extract(raw_html, format, tree=content_tree)
            word_count = len(content.split()) if content else 0
            return {
                "content": content or "",
                "title": self._extract_title(raw_html),
                "author": self._extract_meta(raw_html, "author"),
                "site": urlparse(url).hostname or "",
                "language": self._extract_language(raw_html),
                "published": (
                    self._extract_meta(raw_html, "article:published_time")
                    or self._extract_meta(raw_html, "date")
                    or ""
                ),
                "word_count": word_count,
            }

        # ── Article / Unknown path: existing trafilatura logic ───
        # Belt-and-suspenders guard: ensure raw_html is str/bytes before
        # passing to trafilatura. The top-of-method guard should have caught
        # this already, but certain code paths (e.g. alternate fallback
        # recursion) could land here with a non-string value.
        if not isinstance(raw_html, (str, bytes)):
            logger.warning(
                "raw_html is %s (repr=%.200s) before trafilatura — coercing to empty string",
                type(raw_html).__name__, repr(raw_html),
            )
            raw_html = ""

        # Try trafilatura first (best extraction quality)
        try:
            import trafilatura
            from trafilatura.core import extract_with_metadata

            # Extract with metadata in one pass — run in thread to avoid blocking
            def _do_extract():
                return extract_with_metadata(
                    raw_html,
                    url=url,
                    output_format=format if format in ("markdown", "html", "txt") else "markdown",
                    include_links=True,
                    include_tables=True,
                    include_comments=include_replies,
                )

            doc = await self._run_in_thread(_do_extract)

            if doc is not None and doc.text:
                content = doc.text

        except ImportError:
            logger.warning("trafilatura not available, using basic extraction")
        except Exception as e:
            logger.warning("trafilatura extraction failed: %s — using fallback", e)

        # Fallback: basic extraction
        if not content and format != "json":
            if not isinstance(raw_html, (str, bytes)):
                logger.warning(
                    "raw_html is %s before _basic_extract — coercing to empty string",
                    type(raw_html).__name__,
                )
                raw_html = ""
            content = await self._basic_extract(raw_html, format)

        # Fallback: just return raw text
        if not content:
            if not isinstance(raw_html, (str, bytes)):
                logger.warning(
                    "raw_html is %s before _strip_html — coercing to empty string",
                    type(raw_html).__name__,
                )
                raw_html = ""
            content = await self._run_in_thread(lambda: self._strip_html(raw_html))

        # Build metadata dict from Document if available
        meta = {}
        if doc is not None:
            meta["title"] = doc.title or ""
            meta["author"] = doc.author or ""
            meta["site"] = doc.sitename or ""
            meta["language"] = doc.language or ""
            meta["published"] = doc.date or ""
        else:
            # Try to extract from HTML directly
            meta["title"] = self._extract_title(raw_html)
            meta["author"] = self._extract_meta(raw_html, "author")
            meta["site"] = urlparse(url).hostname or ""
            meta["language"] = self._extract_language(raw_html)
            meta["published"] = (
                self._extract_meta(raw_html, "article:published_time")
                or self._extract_meta(raw_html, "date")
                or ""
            )

        word_count = len(content.split()) if content else 0

        return {
            "content": content or "",
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "site": meta.get("site", ""),
            "language": meta.get("language", ""),
            "published": meta.get("published", ""),
            "word_count": word_count,
        }

    # ── Content-type detection constants ────────────────────────
    # Weights: points contributed when a signal is triggered
    _DT_WEIGHT_FEED_HIGH = 2
    _DT_WEIGHT_FEED_LOW = 1
    _DT_WEIGHT_ARTICLE_OG_TYPE = 3
    _DT_WEIGHT_ARTICLE_META = 1
    _DT_WEIGHT_ARTICLE_SCHEMA = 2
    _DT_WEIGHT_ARTICLE_SINGLE = 2

    # Thresholds: minimum counts to activate each signal
    _DT_S1_HIGH_THRESHOLD = 5  # post-like elements for +2
    _DT_S1_LOW_THRESHOLD = 3   # post-like elements for +1
    _DT_S2_THRESHOLD = 3       # <article> tags
    _DT_S5_THRESHOLD = 2       # pagination links
    _DT_S6_REPEAT_COUNT = 3    # occurrences of same ID base to consider "repeated"
    _DT_S6_HIGH_THRESHOLD = 2  # repeated bases for +2
    _DT_S6_LOW_THRESHOLD = 1   # repeated bases for +1
    _DT_S10_ARTICLE_EQ = 1     # exactly N <article> tags
    _DT_S10_POST_LE = 2        # at most N post-like elements

    # Decision thresholds
    _DT_FEED_MIN_SCORE = 4
    _DT_ARTICLE_MIN_SCORE = 3

    # Blacklist: ID bases that are common single-use containers, not feed items
    _DT_ID_BASE_BLACKLIST: frozenset = frozenset({
        "header", "footer", "nav", "sidebar", "main", "content",
        "wrapper", "container", "section", "page", "article",
        "body", "root", "app", "site", "menu", "modal",
    })

    @staticmethod
    def _detect_content_type_sync(raw_html: str) -> tuple[str, Any]:
        """Synchronous detection logic (runs in a thread).

        Returns:
            (category, tree) where tree is the parsed HTMLParser or None.
        """
        from selectolax.parser import HTMLParser

        tree = HTMLParser(raw_html)

        feed_score = 0
        article_score = 0

        # ── Feed signals ──────────────────────────────────────────────────────────

        # S1: Multiple post-like elements (.post, .entry, .item, .thread, .topic)
        post_elements = len(tree.css(
            ".post, .entry, .item, .thread, .topic"
        ))
        if post_elements >= Tools._DT_S1_HIGH_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_HIGH
        elif post_elements >= Tools._DT_S1_LOW_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_LOW

        # S2: Multiple <article> tags
        article_tags = len(tree.css("article"))
        if article_tags >= Tools._DT_S2_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_LOW

        # S3: Explicit feed container (#posts, #feed, .feed, #threads, etc.)
        feed_containers = len(tree.css(
            "#posts, #feed, .feed, #threads, "
            ".listing, .thread-list, .post-list"
        ))
        if feed_containers >= 1:
            feed_score += Tools._DT_WEIGHT_FEED_HIGH

        # S4: Dual pagination (rel="next" AND rel="prev")
        has_next = bool(tree.css('link[rel="next"], a[rel="next"]'))
        has_prev = bool(tree.css('link[rel="prev"], a[rel="prev"]'))
        if has_next and has_prev:
            feed_score += Tools._DT_WEIGHT_FEED_HIGH

        # S5: Pagination URL params in <a href> (?page=, ?after=, ?offset=)
        page_links = len(tree.css(
            'a[href*="?page="], a[href*="&page="], '
            'a[href*="?after="], a[href*="&after="], '
            'a[href*="?offset="], a[href*="&offset="]'
        ))
        if page_links >= Tools._DT_S5_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_LOW

        # S6: Repeated ID base patterns (e.g. comment-1, comment-2, comment-3)
        id_counts: dict[str, int] = {}
        for el in tree.css("[id]"):
            raw_id = el.attributes.get("id", "")
            if not raw_id:
                continue
            base = re.sub(r"\d+$", "", raw_id).rstrip("- _")
            if base and base.lower() not in Tools._DT_ID_BASE_BLACKLIST:
                id_counts[base] = id_counts.get(base, 0) + 1
        repeated = sum(1 for c in id_counts.values() if c >= Tools._DT_S6_REPEAT_COUNT)
        if repeated >= Tools._DT_S6_HIGH_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_HIGH
        elif repeated >= Tools._DT_S6_LOW_THRESHOLD:
            feed_score += Tools._DT_WEIGHT_FEED_LOW

        # ── Article signals ───────────────────────────────────────────────────────

        # S7: Open Graph og:type="article"
        for meta in tree.css('meta[property="og:type"]'):
            if (meta.attributes.get("content") or "").lower() == "article":
                article_score += Tools._DT_WEIGHT_ARTICLE_OG_TYPE
                break

        # S8: Open Graph article:* meta tags (article:published_time, etc.)
        article_meta_count = len(tree.css('meta[property^="article:"]'))
        if article_meta_count >= 1:
            article_score += Tools._DT_WEIGHT_ARTICLE_META

        # S9: Schema.org Article type
        for el in tree.css("[itemtype]"):
            it = (el.attributes.get("itemtype") or "").lower()
            if "schema.org/article" in it:
                article_score += Tools._DT_WEIGHT_ARTICLE_SCHEMA
                break

        # S10: Exactly one <article> AND few post-like elements
        if (
            article_tags == Tools._DT_S10_ARTICLE_EQ
            and post_elements <= Tools._DT_S10_POST_LE
        ):
            article_score += Tools._DT_WEIGHT_ARTICLE_SINGLE

        # ── Decision ──────────────────────────────────────────────────────────────
        if feed_score >= Tools._DT_FEED_MIN_SCORE and feed_score >= article_score:
            return "feed", tree
        if article_score >= Tools._DT_ARTICLE_MIN_SCORE and article_score >= feed_score:
            return "article", tree
        return "unknown", tree

    async def _detect_content_type(self, raw_html: str) -> tuple[str, Any]:
        """
        Classify a page as 'feed', 'article', or 'unknown'.

        Uses selectolax to parse and a heuristic scoring system.
        Runs in a thread to avoid blocking the event loop.

        Returns:
            (category, tree) where tree is the parsed HTMLParser (for feed reuse)
            or None if detection failed.

            category is:
                "feed"    -> page is a feed/forum/listing - use selectolax full extraction
                "article" -> page is a single article - use trafilatura
                "unknown" -> no clear signals - defer to default trafilatura behavior
        """
        try:
            return await self._run_in_thread(lambda: Tools._detect_content_type_sync(raw_html))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Content type detection failed, falling back to 'unknown'"
            )
            return "unknown", None

    async def _basic_extract(self, html: str, format: str, tree=None) -> str:
        """Basic HTML extraction fallback using selectolax or regex.

        If *tree* is provided (a pre-parsed selectolax HTMLParser), it is used
        directly instead of re-parsing the HTML — avoids duplicate work when
        content-type detection already parsed the page.

        Runs in a thread to avoid blocking the event loop.
        """
        try:
            from selectolax.parser import HTMLParser

            def _do_extract():
                nonlocal tree
                if tree is None:
                    tree = HTMLParser(html)

                # Remove unwanted elements
                for tag in ("script", "style", "nav", "header", "footer", "aside"):
                    for node in tree.css(tag):
                        node.decompose()

                if format == "html":
                    return tree.body.html or tree.html or ""

                text = tree.body.text(separator="\n") if tree.body else tree.text()

                if format == "markdown":
                    # Simple conversion: wrap paragraphs
                    lines = []
                    for line in text.split("\n"):
                        stripped = line.strip()
                        if stripped:
                            lines.append(stripped)
                        elif lines and lines[-1]:
                            lines.append("")
                    text = "\n".join(lines)

                return text.strip()

            return await self._run_in_thread(_do_extract)

        except ImportError:
            pass

        # Ultimate fallback: regex strip
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ──────────────────────────────────────────────
    #  Internal: Alternate content fallback
    # ──────────────────────────────────────────────

    async def _try_alternate_fallback(
        self,
        raw_html: str,
        url: str,
        browser: str,
        timeout_ms: int,
        proxy: Optional[str],
        format: str,
    ) -> tuple[dict, list[str]]:
        """
        When the extracted content is too thin, look for <link rel="alternate">
        entries in the HTML <head> and try fetching them.

        Returns: (extracted_dict, list_of_alternate_urls_used)
        """
        alternates_used = []
        try:
            from selectolax.parser import HTMLParser

            def _find_alternates():
                tree = HTMLParser(raw_html)
                return tree.css('link[rel="alternate"]')

            alternates = await self._run_in_thread(_find_alternates)

            candidates = []
            for link in alternates:
                alt_type = link.attributes.get("type", "")
                alt_href = link.attributes.get("href", "")

                if not alt_href or alt_href.startswith("#"):
                    continue

                # Match type to requested format
                if format == "json" and "json" in alt_type.lower():
                    candidates.append(alt_href)
                elif format == "raw" or format == "html":
                    if alt_type in ("text/html", "application/xhtml+xml"):
                        candidates.append(alt_href)
                elif format in ("markdown", "txt"):
                    if alt_type in ("text/markdown", "text/plain"):
                        candidates.append(alt_href)

            if not candidates:
                return {"content": "", "word_count": 0}, alternates_used

            # Resolve relative URLs
            from urllib.parse import urljoin

            # Only try the best candidate — trying 3 alternates per URL
            # in a batch of 50 would mean up to 200 extra fetches, pressuring
            # connection pools and memory. One fallback covers the common case.
            for alt_url in candidates[:1]:
                resolved = urljoin(url, alt_url)
                try:
                    alt_raw, alt_final, _, _, _, _ = await self._fetch_with_fingerprint(
                        url=resolved,
                        browser=browser,
                        timeout_ms=timeout_ms,
                        proxy=proxy,
                        format=format,
                    )
                    alt_extracted = await self._extract_content(
                        raw_html=alt_raw,
                        url=alt_final,
                        format=format,
                    )
                    if alt_extracted.get("word_count", 0) > MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK:
                        alternates_used.append(alt_final)
                        return alt_extracted, alternates_used
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue

        except ImportError:
            pass

        return {"content": "", "word_count": 0}, alternates_used

    # ──────────────────────────────────────────────
    #  Internal: Content-type guard
    # ──────────────────────────────────────────────

    # Application types that carry human-readable text.
    _TEXT_LIKE_APPLICATION_TYPES: frozenset = frozenset({
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/javascript",
        "application/ecmascript",
    })

    @staticmethod
    def _is_text_content(content_type: str) -> bool:
        """Return True when *content_type* indicates human-readable text.

        Empty/missing content-type is treated as text (optimistic fallback).
        """
        ct = (content_type or "").strip()
        if not ct:
            return True

        # Strip parameters (e.g. ``text/html; charset=utf-8``)
        mime = ct.split(";", 1)[0].strip().lower()

        if mime.startswith("text/"):
            return True

        return mime in Tools._TEXT_LIKE_APPLICATION_TYPES

    # ── document extraction ──────────────────────

    # Content-types whose body can be parsed into text.
    _EXTRACTABLE_DOCUMENT_TYPES: frozenset = frozenset({
        # PDF
        "application/pdf",
        # Microsoft Office (modern)
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        # Microsoft Office (legacy)
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        # OpenDocument
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        # Other text-carrying formats
        "application/epub+zip",
        "application/rtf",
        "text/rtf",
    })

    @staticmethod
    def _is_extractable_document(content_type: str) -> bool:
        """Return True when *content_type* is a document format whose text
        we know how to extract (PDF, DOCX, etc.)."""
        ct = (content_type or "").strip()
        if not ct:
            return False
        mime = ct.split(";", 1)[0].strip().lower()
        return mime in Tools._EXTRACTABLE_DOCUMENT_TYPES

    async def _extract_document_content(
        self,
        url: str,
        content_type: str,
        raw_bytes: Optional[bytes],
    ) -> dict:
        """Extract text from a binary document body (PDF, DOCX, …).

        Returns a dict compatible with ``_extract_content``:
        ``{content, title, author, language, published, word_count}``.
        """
        empty = {
            "content": "",
            "title": "",
            "author": "",
            "language": "",
            "published": "",
            "word_count": 0,
        }

        if raw_bytes is None:
            empty["content"] = (
                f"[Document ({content_type}) could not be extracted: "
                "no response body available. Try fetching again.]"
            )
            return empty

        mime = content_type.split(";", 1)[0].strip().lower()

        try:
            if mime == "application/pdf":
                return await self._extract_pdf(url, raw_bytes)

            if mime in (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ):
                return await self._extract_docx(url, raw_bytes)

            # Formats we recognise but don't have a dedicated parser for yet.
            # Return a helpful message rather than binary garbage.
            fmt_name = {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel (.xlsx)",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint (.pptx)",
                "application/msword": "Word (.doc)",
                "application/vnd.ms-excel": "Excel (.xls)",
                "application/vnd.ms-powerpoint": "PowerPoint (.ppt)",
                "application/vnd.oasis.opendocument.text": "OpenDocument Text (.odt)",
                "application/vnd.oasis.opendocument.spreadsheet": "OpenDocument Spreadsheet (.ods)",
                "application/vnd.oasis.opendocument.presentation": "OpenDocument Presentation (.odp)",
                "application/epub+zip": "EPUB",
                "application/rtf": "Rich Text Format",
                "text/rtf": "Rich Text Format",
            }.get(mime, mime)

            empty["content"] = (
                f"[Document format detected: {fmt_name}. "
                f"Text extraction for this format is not yet supported by smart_fetch_url. "
                f"Consider uploading the file directly to Open WebUI's knowledge base for full extraction.]"
            )
            return empty

        except Exception as e:
            logger.warning(f"Document extraction failed for {url} ({content_type}): {e}")
            empty["content"] = (
                f"[Document ({content_type}) text extraction failed: {e}]"
            )
            return empty

    async def _extract_pdf(self, url: str, raw_bytes: bytes) -> dict:
        """Extract text and metadata from a PDF using pypdf."""
        import io

        from pypdf import PdfReader

        def _do_extract():
            reader = PdfReader(io.BytesIO(raw_bytes))

            # Metadata
            meta = reader.metadata or {}
            title = str(meta.get("/Title", "") or "").strip()
            author = str(meta.get("/Author", "") or "").strip()

            # Content
            parts: list[str] = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    parts.append(text.strip())

            content = "\n\n".join(parts)
            return {
                "content": content,
                "title": title,
                "author": author,
                "language": "",
                "published": "",
                "word_count": len(content.split()) if content else 0,
            }

        return await self._run_in_thread(_do_extract)

    async def _extract_docx(self, url: str, raw_bytes: bytes) -> dict:
        """Extract text from a modern Word document (.docx).

        A .docx file is a ZIP archive of XML files.  We extract the
        document body from ``word/document.xml`` without pulling in
        ``python-docx`` as an extra dependency.
        """
        import io
        import xml.etree.ElementTree as ET
        import zipfile

        def _do_extract():
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = set(zf.namelist())

                if "word/document.xml" not in names:
                    return {
                        "content": "",
                        "title": "",
                        "author": "",
                        "language": "",
                        "published": "",
                        "word_count": 0,
                    }

                doc_xml = zf.read("word/document.xml")

                # Metadata (best-effort, same zip session — no double-open)
                title = ""
                author = ""
                if "docProps/core.xml" in names:
                    try:
                        core_xml = zf.read("docProps/core.xml")
                        core_ns = {
                            "dc": "http://purl.org/dc/elements/1.1/",
                            "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                            "dcterms": "http://purl.org/dc/terms/",
                        }
                        core_root = ET.fromstring(core_xml)
                        title_el = core_root.find("dc:title", core_ns) or core_root.find("dcterms:title", core_ns)
                        if title_el is not None and title_el.text:
                            title = title_el.text.strip()
                        author_el = core_root.find("dc:creator", core_ns)
                        if author_el is not None and author_el.text:
                            author = author_el.text.strip()
                    except Exception:
                        pass

            # Namespace map for OOXML (outside the with-block — xml is already in memory)
            ns = {
                "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            }

            root = ET.fromstring(doc_xml)

            # Extract all paragraph texts
            paragraphs: list[str] = []
            for p in root.iter("{%s}p" % ns["w"]):
                texts: list[str] = []
                for t in p.iter("{%s}t" % ns["w"]):
                    if t.text:
                        texts.append(t.text)
                if texts:
                    paragraphs.append("".join(texts))

            content = "\n\n".join(paragraphs)

            return {
                "content": content,
                "title": title,
                "author": author,
                "language": "",
                "published": "",
                "word_count": len(content.split()) if content else 0,
            }

        return await self._run_in_thread(_do_extract)

    # ──────────────────────────────────────────────
    #  Internal: Format helpers
    # ──────────────────────────────────────────────

    def _format_output(
        self,
        url: str,
        final_url: str,
        title: str,
        author: str,
        site: str,
        language: str,
        published: str,
        content: str,
        format: str,
        word_count: int,
        browser: str,
        status_code: int,
        note: Optional[str] = None,
        error: Optional[dict[str, str]] = None,
    ) -> str:
        """Build the final output string from a single metadata dict.

        All text-based formats share the same dict→lines loop.
        On error, ``error`` is a dict with ``"error_type"`` and ``"message"``
        keys (as returned by :meth:`_format_error`); ``content`` and
        ``word_count`` are ignored.
        """
        # ── Build metadata dict (ordered, for consistent text output) ──
        fields: dict[str, str] = {}

        if error:
            fields["Status"] = "error"
            fields["Error"] = error["message"]
        else:
            fields["Status"] = "ok"

        resolved_url = final_url or url
        fields["URL"] = resolved_url
        if title:
            fields["Title"] = title
        if author:
            fields["Author"] = author
        if site and site != urlparse(resolved_url).hostname:
            fields["Site"] = site
        if language:
            fields["Language"] = language
        if published:
            fields["Published"] = published
        fields["Words"] = str(word_count)
        fields["Browser"] = browser
        if note:
            fields["Note"] = note

        # ── Strip base64 from content (always) ─────────────────────────
        # Base64-encoded images/videos are pure token waste — the LLM
        # cannot render them. Replace with empty-image Markdown.
        if content and not error:
            content = _strip_base64(content)

        # ── JSON ───────────────────────────────────────────────────────
        if format == "json":
            result = dict(fields)
            result["statusCode"] = status_code
            if error:
                result["errorType"] = error.get("error_type", "")
            else:
                result["content"] = content
            result["url"] = url
            result["finalUrl"] = resolved_url
            return json.dumps(result, indent=2, ensure_ascii=False)

        # ── Text-based formats ─────────────────────────────────────────
        parts: list[str] = [f"> {k}: {v}" for k, v in fields.items()]
        parts.append("")
        if content and not error:
            parts.append(content)
        return "\n".join(parts)

    def _build_raw_response(
        self,
        url: str,
        final_url: str,
        raw_html: str,
        status_code: int,
        content_type: str,
        browser: str,
    ) -> str:
        """Build raw format response with metadata prefix."""
        raw_html = _strip_base64_raw(raw_html)

        lines = [
            f"> Status: ok",
            f"> URL: {final_url}",
            f"> HTTP Status: {status_code}",
            f"> Content-Type: {content_type}",
            f"> Browser: {browser}",
            f"> Size: {len(raw_html)} bytes",
            "",
            raw_html,
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────
    #  Internal: HTML metadata helpers
    # ──────────────────────────────────────────────

    def _extract_title(self, html: str) -> str:
        match = re.search(
            r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE
        )
        return match.group(1).strip() if match else ""

    def _extract_meta(self, html: str, name: str) -> str:
        # property variant (og:, article:)
        patterns = [
            rf'<meta\s+(?:property|name)=["\']{name}["\']\s+content=["\'](.*?)["\']',
            rf'<meta\s+content=["\'](.*?)["\']\s+(?:property|name)=["\']{name}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _extract_language(self, html: str) -> str:
        match = re.search(
            r'<html[^>]*\slang=["\']([a-zA-Z-]+)["\']', html, re.IGNORECASE
        )
        return match.group(1).strip() if match else ""

    def _strip_html(self, html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    # ──────────────────────────────────────────────
    #  Internal: UserValves helper
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    #  Internal: Sources / visited URLs helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _collect_visited_urls(
        original_url: str, final_url: str, alternate_urls: list[str]
    ) -> list[str]:
        """Build a de-duplicated list of all URLs visited during the fetch."""
        seen = set()
        result = []
        for u in (original_url, final_url, *alternate_urls):
            if u and u not in seen:
                seen.add(u)
                result.append(u)
        return result

    async def _emit_sources(
        self,
        emitter: Optional[Any],
        urls: list[str],
    ):
        """
        Emit source events that Open WebUI appends to message.sources.

        The frontend Citations.svelte renders these at the bottom of the message
        with favicons (fetched from google's favicon service).

        Emits all URLs concurrently via ``asyncio.gather`` instead of
        sequentially, so N URLs cost one ``await`` cycle (the slowest)
        instead of N sequential ``await emitter()`` calls that could
        delay LLM streaming.
        """
        if emitter is None or not urls:
            return

        async def _emit_one(u: str):
            await emitter(
                {
                    "type": "source",
                    "data": {
                        "source": {"name": u, "id": u},
                        "document": [""],
                        "metadata": [{"source": u, "name": u, "url": u}],
                    },
                }
            )

        try:
            await asyncio.gather(*(_emit_one(u) for u in urls))
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

    async def _emit_status(
        self,
        emitter: Optional[Any],
        description: str,
        done: bool = False,
    ):
        """
        Emit a real-time status event for the Open WebUI progress indicator.

        Uses ``"type": "status"`` which is the only real-time feedback type
        that works in Native Mode.  Always emit a final ``done=True`` to
        stop the shimmer animation.

        Payload format (works identically in Default and Native modes):

        .. code-block:: python

            {
                "type": "status",
                "data": {
                    "description": "Human-readable text",
                    "done": False,      # False = shimmer animation active
                    "hidden": False,     # True = saved to history, not shown
                },
            }
        """
        if emitter is None:
            return
        try:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                    },
                }
            )
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception:
            pass  # Event emission is best-effort

    # ──────────────────────────────────────────────
    #  Internal: Error formatting
    # ──────────────────────────────────────────────

    def _format_error(self, error: Exception, _url: str = "") -> dict[str, str]:
        """Classify an exception into a structured error dict.

        Returns ``{"error_type": "...", "message": "..."}`` so callers
        can use it for both JSON output and text-based metadata without
        duplicating logic.
        """
        msg = str(error)
        if "Name or service not known" in msg or "nodename nor servname" in msg:
            return {"error_type": "dns", "message": "DNS resolution failed"}
        if "Connection refused" in msg or "connect" in msg.lower():
            return {"error_type": "connection_refused", "message": "Connection refused"}
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return {"error_type": "timeout", "message": "Connection timed out"}
        if "403" in msg:
            return {"error_type": "http_403", "message": "403 Forbidden"}
        if "404" in msg:
            return {"error_type": "http_404", "message": "404 Not Found"}
        if "SSL" in msg or "certificate" in msg.lower():
            return {"error_type": "tls", "message": "SSL connection error"}
        return {"error_type": "internal", "message": "Internal error"}


# ═════════════════════════════════════════════════════════════════════════════
#  skimmd — Skimmed Markdown (inline, zero external dependencies)
# ═════════════════════════════════════════════════════════════════════════════
#
# Whitelist-based HTML-to-Markdown converter used by the ``"skimmd"`` output
# format.  Canonical source (with CLI) lives at ``test/skimmd.py``.

from html.parser import HTMLParser

_SKIMMD_WHITELIST = frozenset({
    "a", "img", "video", "source", "picture",
    "p", "br", "hr", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "td", "th", "thead", "tbody", "caption",
    "strong", "em", "b", "i", "u", "span", "mark", "small",
    "div", "section", "article", "figure", "figcaption",
    "blockquote", "cite", "q",
})

_SKIMMD_NOISY = frozenset({
    "script", "style", "nav", "footer", "header",
    "noscript", "iframe", "meta", "link", "svg", "form",
})

_SKIMMD_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

_SKIMMD_BLOCK = frozenset({
    "p", "div", "section", "article", "figure", "figcaption",
    "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "td", "th",
})

_SKIMMD_LIST = frozenset({"ul", "ol"})

_SKIMMD_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
    "nbsp": "\u00a0", "bull": "\u2022", "hellip": "\u2026",
    "mdash": "\u2014", "ndash": "\u2013",
    "laquo": "\u00ab", "raquo": "\u00bb",
    "lsquo": "\u2018", "rsquo": "\u2019",
    "ldquo": "\u201c", "rdquo": "\u201d",
}


class _SkimmdParser(HTMLParser):
    """Whitelist-based HTML-to-Markdown converter (inline for Open WebUI)."""

    def __init__(self, base_url: str | None = None, *, strip_external: bool = True):
        super().__init__(convert_charrefs=False)
        self._output: list[str] = []
        self._buf: list[str] = []
        self._buf_has_internal = False
        self._buf_has_external = False
        self._skip_depth = 0
        self._in_pre = False
        self._list_stack: list[str] = []
        self._list_counters: list[int] = []
        self._in_a = False
        self._a_href = ""
        self._a_is_external = False
        self._base_url = base_url.rstrip("/") if base_url else None
        self._strip_external = strip_external
        self._origin: str | None = None
        if strip_external and base_url:
            self._origin = urlparse(base_url).hostname

    def _is_external(self, href: str) -> bool:
        if not href or not self._origin:
            return False
        parsed = urlparse(href)
        if not parsed.hostname:
            return False
        return parsed.hostname != self._origin

    def _resolve(self, url: str) -> str:
        if self._base_url and url and url.startswith("/"):
            return self._base_url + url
        return url

    def _emit(self, text: str) -> None:
        if text:
            self._buf.append(text)

    def _flush_buf(self) -> None:
        if not self._buf:
            return
        if self._strip_external and self._buf_has_external and not self._buf_has_internal:
            self._reset_buf()
            return
        text = "".join(self._buf)
        self._output.append(text)
        self._reset_buf()

    def _reset_buf(self) -> None:
        self._buf = []
        self._buf_has_internal = False
        self._buf_has_external = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIMMD_NOISY:
            if tag not in _SKIMMD_VOID:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr_dict = {k: v for k, v in attrs if v is not None}
        for key in ("href", "src", "poster"):
            if key in attr_dict:
                attr_dict[key] = self._resolve(attr_dict[key])
        if tag in _SKIMMD_BLOCK:
            self._flush_buf()
        self._handle_prefix(tag, attr_dict)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIMMD_NOISY:
            if tag not in _SKIMMD_VOID:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        self._handle_suffix(tag)
        if tag in _SKIMMD_BLOCK:
            self._flush_buf()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self._emit(text)
            if not self._a_is_external:
                self._buf_has_internal = True

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            return
        self._emit(_SKIMMD_ENTITIES.get(name, f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            return
        try:
            self._emit(chr(int(name)))
        except (ValueError, OverflowError):
            self._emit(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        pass

    def _handle_prefix(self, tag: str, attrs: dict[str, str]) -> None:
        if tag not in _SKIMMD_WHITELIST:
            return
        # a — links
        if tag == "a":
            href = attrs.get("href", "")
            is_ext = self._is_external(href)
            self._a_href = href
            self._a_is_external = is_ext
            if self._strip_external and is_ext:
                self._buf_has_external = True
                return
            self._emit("[")
            if href:
                self._buf_has_internal = True
            return
        # img
        if tag == "img":
            src = attrs.get("src", "")
            alt = attrs.get("alt", "")
            if src.startswith("data:"):
                self._emit(f"![{alt}]()")
            else:
                self._emit(f"![{alt}]({src})")
            self._buf_has_internal = True
            return
        # video
        if tag == "video":
            src = attrs.get("src", "")
            poster = attrs.get("poster", "")
            parts: list[str] = []
            if poster:
                poster_md = f"![Poster]()" if poster.startswith("data:") else f"![Poster]({poster})"
                parts.append(poster_md)
            if src:
                if src.startswith("data:"):
                    parts.append("[Video]")
                else:
                    parts.append(f"[Video]({src})")
            if parts:
                self._emit(" ".join(parts))
                self._buf_has_internal = True
            return
        # source
        if tag == "source":
            src = attrs.get("src", "")
            if src:
                self._emit(f" [Stream]({src})")
                self._buf_has_internal = True
            return
        if tag == "picture":
            return
        # headings
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit(f"\n{'#' * int(tag[1])} ")
            return
        # lists
        if tag in ("ul", "ol"):
            if not self._list_stack:
                self._emit("\n")
            self._list_stack.append(tag)
            self._list_counters.append(0)
            return
        if tag == "li":
            indent = "  " * len(self._list_stack)
            prefix = "-"
            if self._list_stack and self._list_stack[-1] == "ol":
                self._list_counters[-1] += 1
                prefix = f"{self._list_counters[-1]}."
            self._emit(f"\n{indent}{prefix} ")
            return
        # blockquote
        if tag == "blockquote":
            self._emit("\n> ")
            return
        # code
        if tag == "pre":
            self._in_pre = True
            self._emit("\n```\n")
            return
        if tag == "code" and not self._in_pre:
            self._emit("`")
            return
        # inline formatting
        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return
        # hr / br
        if tag == "hr":
            self._emit("\n---\n")
            return
        if tag == "br":
            self._emit("\n")
            return

    def _handle_suffix(self, tag: str) -> None:
        if tag not in _SKIMMD_WHITELIST:
            return
        if tag == "a":
            if self._strip_external and self._a_is_external:
                self._a_href = ""
                self._a_is_external = False
                return
            if self._a_href:
                self._emit(f"]({self._a_href})")
            self._a_href = ""
            self._a_is_external = False
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n")
            return
        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
                self._list_counters.pop()
            self._emit("\n")
            return
        if tag == "blockquote":
            self._emit("\n")
            return
        if tag == "pre":
            self._in_pre = False
            self._emit("\n```\n")
            return
        if tag == "code" and not self._in_pre:
            self._emit("`")
            return
        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return

    def get_result(self) -> str:
        self._flush_buf()
        text = "".join(self._output)
        text = re.sub(r" +\n", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n", text)
        text = re.sub(r"  +", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return text.strip()


# 1×1 white transparent GIF in base64 — used as a tiny placeholder for
# stripped base64 images in ``raw`` format output.
_BASE64_PLACEHOLDER_GIF = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=="
)


def _strip_base64(text: str) -> str:
    """Strip base64-encoded image/video URLs from Markdown content.

    Base64 blobs are pure token waste — the LLM cannot render them.
    This replaces ``![alt](data:...)`` with ``![alt]()`` in all text-based
    output formats, regardless of any ``remove_images``-style flag.
    """
    # Markdown image: ![alt](data:image/...base64,...)  →  ![alt]()
    text = re.sub(
        r'!\[([^\]]*)\]\(data:(?:image|video)[^\)]+\)',
        r'![\1]()',
        text,
    )
    # Markdown video: [Video](data:...)  →  [Video]
    text = re.sub(
        r'\[Video\]\(data:[^\)]+\)',
        '[Video]',
        text,
    )
    return text


def _strip_base64_raw(html: str) -> str:
    """Replace base64 data URIs in raw HTML with a tiny 1×1 white pixel.

    Used in ``raw`` format output — preserves HTML structure (valid image
    references) while eliminating multi-kilobyte base64 blobs.
    """
    # <img src="data:image/...base64,..."  →  <img src="pixel.gif"
    html = re.sub(
        r'src=[\'"]data:(?:image|video)[^\'"]+[\'"]',
        f'src="{_BASE64_PLACEHOLDER_GIF}"',
        html,
    )
    # <source src="data:..."  →  <source src="pixel.gif"
    html = re.sub(
        r'src=[\'"]data:[^\'"]+[\'"]',
        f'src="{_BASE64_PLACEHOLDER_GIF}"',
        html,
    )
    # poster="data:..."  →  poster="pixel.gif"
    html = re.sub(
        r'poster=[\'"]data:[^\'"]+[\'"]',
        f'poster="{_BASE64_PLACEHOLDER_GIF}"',
        html,
    )
    return html


def _skimmd_parse(html: str, base_url: str | None = None, *, strip_external: bool = True) -> str:
    """Convert HTML to Skimmed Markdown (inline for Open WebUI)."""
    parser = _SkimmdParser(base_url=base_url, strip_external=strip_external)
    parser.feed(html)
    return parser.get_result()
