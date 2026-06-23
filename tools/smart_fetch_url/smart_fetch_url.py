"""
title: Smart Fetch URL
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Always preferred over 'fetch_url'. Fetches URLs with TLS fingerprinting to avoid blocks, returns clean content with metadata. Use by default.
required_open_webui_version: 0.9.0
requirements: curl_cffi>=0.7.0, trafilatura, selectolax
version: 0.5.0
licence: MIT
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Browser TLS profiles (fingerprints)
# ──────────────────────────────────────────────

# These map to curl_cffi's impersonate strings.
# curl_cffi natively supports: chrome99, chrome100, chrome101, chrome104,
# chrome107, chrome110, chrome116, chrome119, chrome120, chrome123,
# chrome124, chrome131, chrome133, chrome134, chrome135, chrome145,
# safari15_3, safari15_5, safari17_0, safari17_2_1, safari18_0,
# safari17_5, safari18_2, firefox110, firefox117, firefox147,
# firefox133, firefox135, firefox137, edge99, edge101, edge127,
# edge133, edge135, opera92, opera95, opera105, opera114
#
# We expose them with human-readable names and map to curl_cffi aliases.

BROWSER_PROFILES = {
    # Chrome
    "chrome_99": "chrome99",
    "chrome_100": "chrome100",
    "chrome_101": "chrome101",
    "chrome_104": "chrome104",
    "chrome_107": "chrome107",
    "chrome_110": "chrome110",
    "chrome_116": "chrome116",
    "chrome_119": "chrome119",
    "chrome_120": "chrome120",
    "chrome_123": "chrome123",
    "chrome_124": "chrome124",
    "chrome_131": "chrome131",
    "chrome_133": "chrome133",
    "chrome_134": "chrome134",
    "chrome_135": "chrome135",
    "chrome_145": "chrome145",
    # Firefox
    "firefox_110": "firefox110",
    "firefox_117": "firefox117",
    "firefox_133": "firefox133",
    "firefox_135": "firefox135",
    "firefox_137": "firefox137",
    "firefox_147": "firefox147",
    # Safari
    "safari_15_3": "safari15_3",
    "safari_15_5": "safari15_5",
    "safari_17_0": "safari17_0",
    "safari_17_2_1": "safari17_2_1",
    "safari_17_5": "safari17_5",
    "safari_18_0": "safari18_0",
    "safari_18_2": "safari18_2",
    # Edge
    "edge_99": "edge99",
    "edge_101": "edge101",
    "edge_127": "edge127",
    "edge_133": "edge133",
    "edge_135": "edge135",
    # Opera
    "opera_92": "opera92",
    "opera_95": "opera95",
    "opera_105": "opera105",
    "opera_114": "opera114",
}

DEFAULT_BROWSER = "chrome_145"
DEFAULT_MAX_CHARS = 50_000
DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_BATCH_CONCURRENCY = 8
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
DEFAULT_RAW_ACCEPT = "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,text/markdown;q=0.8,text/plain;q=0.8,*/*;q=0.7"
DEFAULT_JSON_ACCEPT = "application/json,text/json,application/ld+json;q=0.9,text/plain;q=0.8,*/*;q=0.7"
DEFAULT_USER_AGENTS = {
    "chrome_145": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "firefox_147": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "safari_18_0": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "edge_135": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
}


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
            description="Default browser fingerprint profile",
        )
        batch_concurrency: int = Field(
            DEFAULT_BATCH_CONCURRENCY,
            description="Default concurrency for batch_fetch_urls",
        )
        verbose: bool = Field(
            False,
            description="Emit detailed status events during fetch",
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
        default_browser: Optional[str] = Field(
            None,
            description="Browser fingerprint profile (overrides admin setting)",
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

    # ──────────────────────────────────────────────
    #  Core tool method
    # ──────────────────────────────────────────────

    async def smart_fetch_url(
        self,
        url: str,
        format: str = "markdown",
        max_chars: Optional[int] = None,
        browser: Optional[str] = None,
        os: str = "windows",
        timeout_ms: Optional[int] = None,
        remove_images: bool = False,
        include_replies: bool = True,
        proxy: Optional[str] = None,
        headers: Optional[dict] = None,
        show_favicons: bool = True,
        __event_emitter__: Optional[Any] = None,
        __user__: Optional[Any] = None,
    ) -> str:
        """
        Always preferred over 'fetch_url'. Fetches URLs with TLS fingerprinting to avoid blocks, returns clean content with metadata. Use by default.

        :param url: URL to fetch (http/https only)
        :param format: Output format: "markdown" (default, readable), "html" (cleaned HTML),
                       "txt" (plain text), "json" (structured), or "raw" (full server response)
        :param max_chars: Maximum characters to return (default: 50000)
        :param browser: Browser profile for TLS fingerprinting.
                        Examples: chrome_145, firefox_147, safari_18_0, edge_135
        :param os: OS profile hint. Options: windows, macos, linux, android, ios
        :param timeout_ms: Request timeout in milliseconds
        :param remove_images: Strip image references from output
        :param include_replies: Include reply/comment threads when site supports them
        :param proxy: Proxy URL (http://user:pass@host:port or socks5://host:port)
        :param headers: Custom HTTP headers to send
        :param show_favicons: Emit source events so Open Web UI displays
                              favicons and a clickable URL list below the response
                              (default: True)
        :param __event_emitter__: Internal — for UI progress updates
        :param __user__: Internal — for user-specific valve overrides
        :returns: Extracted content string with metadata header
        """

        uv = self._get_user_valves(__user__)
        max_chars = max_chars or (uv.max_chars if uv else None) or self.valves.max_chars
        timeout_ms = timeout_ms or (uv.timeout_ms if uv else None) or self.valves.timeout_ms
        browser = browser or (uv.default_browser if uv else None) or self.valves.default_browser
        uv_verbose = uv.verbose if uv else None
        verbose = uv_verbose if uv_verbose is not None else self.valves.verbose

        # Validate
        if not url or not url.strip():
            return "Error: URL is required."

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return f"Error: Invalid URL protocol. Only http/https are supported: {url}"

        try:
            _start_time = time.monotonic()
            await self._emit_status(__event_emitter__, f"🌐 {url}", done=False)

            (raw_html, final_url, status_code, content_type, resp_headers, raw_bytes) = (
                await self._fetch_with_fingerprint(
                    url=url,
                    browser=browser,
                    os=os,
                    timeout_ms=timeout_ms,
                    proxy=proxy,
                    headers=headers,
                    format=format,
                )
            )

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
                raw_bytes = None  # release early — GC hint for large docs

                content = extracted.get("content", "")
                if max_chars and len(content) > max_chars:
                    content = content[:max_chars]

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
                    word_count=extracted.get("word_count", 0),
                    browser=browser,
                    os=os,
                    status_code=status_code,
                )
                word_count = extracted.get("word_count", 0)
                _elapsed = time.monotonic() - _start_time
                _desc = f"✅ {url}" if not verbose else f"✅ {url} ({word_count}w, {_elapsed:.1f}s)"
                await self._emit_status(__event_emitter__, _desc, done=True)
                if show_favicons:
                    await self._emit_sources(__event_emitter__, [final_url])
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
                    os=os,
                    status_code=status_code,
                )
                _elapsed = time.monotonic() - _start_time
                await self._emit_status(__event_emitter__, f"✅ {url}", done=True)
                if show_favicons:
                    await self._emit_sources(__event_emitter__, [final_url])
                return result

            # Text path: raw bytes no longer needed — drop the reference
            # so the GC can reclaim the body before extraction runs.
            raw_bytes = None

            # Step 3: Handle raw format early — return the full server response
            if format == "raw":
                result = self._build_raw_response(
                    url=url,
                    final_url=final_url,
                    raw_html=raw_html,
                    status_code=status_code,
                    content_type=content_type,
                    browser=browser,
                    os=os,
                )
                _elapsed = time.monotonic() - _start_time
                await self._emit_status(__event_emitter__, f"✅ {url}", done=True)
                if show_favicons:
                    await self._emit_sources(__event_emitter__, [final_url])
                return result

            # Step 4: Extract content
            extracted = await self._extract_content(
                raw_html=raw_html,
                url=final_url,
                format=format,
                remove_images=remove_images,
                include_replies=include_replies,
            )

            # Step 5: Alternate content fallback for thin/no content
            alternate_urls = []
            if (
                format != "json"
                and extracted.get("word_count", 0)
                < 30  # MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK
            ):
                extracted, alternates_used = await self._try_alternate_fallback(
                    raw_html=raw_html,
                    url=final_url,
                    browser=browser,
                    os=os,
                    timeout_ms=timeout_ms,
                    proxy=proxy,
                    headers=headers,
                    format=format,
                    remove_images=remove_images,
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
                os=os,
                status_code=status_code,
            )

            # Step 8: Emit source events for Open WebUI's Citations component (bottom of message)
            visited_urls = self._collect_visited_urls(url, final_url, alternate_urls)
            word_count = extracted.get("word_count", 0)
            _elapsed = time.monotonic() - _start_time
            _desc = f"✅ {url}" if not verbose else f"✅ {url} ({word_count}w, {_elapsed:.1f}s)"
            await self._emit_status(__event_emitter__, _desc, done=True)
            if show_favicons:
                await self._emit_sources(__event_emitter__, visited_urls)

            return result

        except Exception as e:
            error_msg = self._format_error(e, url)
            await self._emit_status(__event_emitter__, f"❌ {url}", done=True)
            logger.exception(f"smart_fetch_url failed for {url}")
            return error_msg

    # ──────────────────────────────────────────────
    #  Batch variant
    # ──────────────────────────────────────────────

    async def batch_fetch_urls(
        self,
        urls: list[str],
        format: str = "markdown",
        max_chars: Optional[int] = None,
        browser: Optional[str] = None,
        os: str = "windows",
        timeout_ms: Optional[int] = None,
        concurrency: Optional[int] = None,
        __event_emitter__: Optional[Any] = None,
        __user__: Optional[Any] = None,
    ) -> str:
        """
        Fetch multiple URLs concurrently with browser-grade TLS fingerprinting.

        Each URL is fetched with the same parameters. Results are clearly labeled
        per URL with success/failure indicators.

        :param urls: Array of URLs to fetch (http/https only, 1-50 items)
        :param format: Output format: "markdown", "html", "txt", "json", or "raw"
        :param max_chars: Maximum characters per URL
        :param browser: Browser profile for TLS fingerprinting
        :param os: OS profile hint
        :param timeout_ms: Request timeout per URL in milliseconds
        :param concurrency: Max concurrent fetches (default: 8)
        :param __event_emitter__: Internal — for UI progress updates
        :param __user__: Internal — for user-specific valve overrides
        :returns: Labeled results for all URLs
        """

        uv = self._get_user_valves(__user__)
        max_chars = max_chars or (uv.max_chars if uv else None) or self.valves.max_chars
        timeout_ms = timeout_ms or (uv.timeout_ms if uv else None) or self.valves.timeout_ms
        browser = browser or (uv.default_browser if uv else None) or self.valves.default_browser
        concurrency = concurrency or (uv.batch_concurrency if uv else None) or self.valves.batch_concurrency

        if not urls:
            return "Error: No URLs provided."

        concurrency = max(1, min(concurrency, 50))

        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(index: int, single_url: str) -> str:
            async with semaphore:
                try:
                    result = await self.smart_fetch_url(
                        url=single_url,
                        format=format,
                        max_chars=max_chars,
                        browser=browser,
                        os=os,
                        timeout_ms=timeout_ms,
                        show_favicons=False,  # batch handles its own source list
                        __event_emitter__=None,  # suppress per-item events
                    )
                    await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] ✅ {single_url}", done=False)
                    return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{result}\n\n---\n"
                except Exception as e:
                    await self._emit_status(__event_emitter__, f"[{index + 1}/{len(urls)}] ❌ {single_url}", done=False)
                    return f"## [{index + 1}/{len(urls)}] {single_url}\n\nError: {self._format_error(e, single_url)}\n\n---\n"

        await self._emit_status(__event_emitter__, f"[0/{len(urls)}] Fetching {len(urls)} URLs…", done=False)

        tasks = [fetch_one(i, u) for i, u in enumerate(urls)]
        results = await asyncio.gather(*tasks)

        await self._emit_status(__event_emitter__, f"✅ Fetched {len(urls)} URLs", done=True)

        # Emit a single combined source list for all batch URLs
        await self._emit_sources(__event_emitter__, urls)

        return "".join(results)

    # ──────────────────────────────────────────────
    #  Internal: TLS-fingerprinted fetch
    # ──────────────────────────────────────────────

    async def _fetch_with_fingerprint(
        self,
        url: str,
        browser: str,
        os: str,
        timeout_ms: int,
        proxy: Optional[str] = None,
        headers: Optional[dict] = None,
        format: str = "markdown",
    ) -> tuple[str, str, int, str, dict, Optional[bytes]]:
        """
        Perform the actual HTTP request with TLS fingerprinting.

        Returns: (raw_html, final_url, status_code, content_type,
                  response_headers, raw_bytes)

        raw_bytes is the undecoded response body — needed for binary
        document extraction (PDF, DOCX, etc.).
        """
        resolved_browser = BROWSER_PROFILES.get(browser, browser)

        # Build headers
        request_headers = dict(headers or {})

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

        # Set User-Agent from profile if not explicitly provided
        if "User-Agent" not in request_headers:
            ua = DEFAULT_USER_AGENTS.get(browser)
            if ua:
                request_headers["User-Agent"] = ua

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
            logger.warning("curl_cffi not available, falling back to httpx")
            return await self._fetch_with_httpx(
                url=url,
                headers=request_headers,
                timeout_ms=timeout_ms,
                proxy=proxy,
            )
        except Exception as e:
            logger.warning(f"curl_cffi failed ({e}), falling back to httpx")
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

        async with AsyncSession(
            impersonate=browser,
            proxies=proxy,
        ) as session:
            resp = await session.get(
                url,
                headers=headers,
                timeout=timeout_sec,
                allow_redirects=True,
            )

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
        if proxy:
            request_kwargs["proxies"] = proxy

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, **request_kwargs)
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
        remove_images: bool = False,
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
                "unexpected raw_html type %s, coercing to empty string", type(raw_html).__name__
            )
            raw_html = str(raw_html) if raw_html is not None else ""

        # ── Detect content type for routing ─────────────────
        content_category = await self._detect_content_type(raw_html)
        logger.info("Content type detected: %s for %s", content_category, url)

        if content_category == "feed":
            # Feed/forum/listing: use selectolax for full content.
            # Trafilatura would only extract the first post.
            content = await self._basic_extract(raw_html, format, remove_images)
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
                    include_images=not remove_images,
                    include_tables=True,
                    include_comments=include_replies,
                )

            doc = await asyncio.to_thread(_do_extract)

            if doc is not None and doc.text:
                content = doc.text

        except ImportError:
            logger.warning("trafilatura not available, using basic extraction")
        except Exception as e:
            logger.warning("trafilatura extraction failed: %s — using fallback", e)

        # Fallback: basic extraction
        if not content and format != "json":
            content = await self._basic_extract(raw_html, format, remove_images)

        # Fallback: just return raw text
        if not content:
            content = await asyncio.to_thread(self._strip_html, raw_html)

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
    def _detect_content_type_sync(raw_html: str) -> str:
        """Synchronous detection logic (runs in a thread)."""
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
            return "feed"
        if article_score >= Tools._DT_ARTICLE_MIN_SCORE and article_score >= feed_score:
            return "article"
        return "unknown"

    async def _detect_content_type(self, raw_html: str) -> str:
        """
        Classify a page as 'feed', 'article', or 'unknown'.

        Uses selectolax to parse and a heuristic scoring system.
        Runs in a thread to avoid blocking the event loop.

        Returns:
            "feed"    -> page is a feed/forum/listing - use selectolax full extraction
            "article" -> page is a single article - use trafilatura
            "unknown" -> no clear signals - defer to default trafilatura behavior
        """
        try:
            return await asyncio.to_thread(Tools._detect_content_type_sync, raw_html)
        except Exception:
            logger.warning(
                "Content type detection failed, falling back to 'unknown'"
            )
            return "unknown"

    async def _basic_extract(self, html: str, format: str, remove_images: bool) -> str:
        """Basic HTML extraction fallback using selectolax or regex.

        Runs in a thread to avoid blocking the event loop.
        """
        try:
            from selectolax.parser import HTMLParser

            def _do_extract():
                tree = HTMLParser(html)

                # Remove unwanted elements
                for tag in ("script", "style", "nav", "header", "footer", "aside"):
                    for node in tree.css(tag):
                        node.decompose()

                if remove_images:
                    for node in tree.css("img"):
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

            return await asyncio.to_thread(_do_extract)

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
        os: str,
        timeout_ms: int,
        proxy: Optional[str],
        headers: Optional[dict],
        format: str,
        remove_images: bool,
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

            alternates = await asyncio.to_thread(_find_alternates)

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
                        os=os,
                        timeout_ms=timeout_ms,
                        proxy=proxy,
                        headers=headers,
                        format=format,
                    )
                    alt_extracted = await self._extract_content(
                        raw_html=alt_raw,
                        url=alt_final,
                        format=format,
                        remove_images=remove_images,
                    )
                    if alt_extracted.get("word_count", 0) > 30:
                        alternates_used.append(alt_final)
                        return alt_extracted, alternates_used
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

        return await asyncio.to_thread(_do_extract)

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

        return await asyncio.to_thread(_do_extract)

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
        os: str,
        status_code: int,
    ) -> str:
        """Build the final output string depending on format."""

        if format == "json":
            result = {
                "url": url,
                "finalUrl": final_url,
                "title": title,
                "author": author,
                "site": site,
                "language": language,
                "publishedDate": published,
                "wordCount": word_count,
                "content": content,
                "browser": browser,
                "os": os,
            }
            return json.dumps(result, indent=2, ensure_ascii=False)

        # Build a metadata header for text-based formats
        parts = []
        parts.append(f"> URL: {final_url}")
        if title:
            parts.append(f"> Title: {title}")
        if author:
            parts.append(f"> Author: {author}")
        if site and site != urlparse(final_url).hostname:
            parts.append(f"> Site: {site}")
        if language:
            parts.append(f"> Language: {language}")
        if published:
            parts.append(f"> Published: {published}")
        parts.append(f"> Words: {word_count}")
        parts.append(f"> Browser: {browser}/{os}")
        parts.append("")

        if content:
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
        os: str,
    ) -> str:
        """Build raw format response with metadata prefix."""
        lines = [
            f"> URL: {final_url}",
            f"> Status: {status_code}",
            f"> Content-Type: {content_type}",
            f"> Browser: {browser}/{os}",
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
        """
        if emitter is None or not urls:
            return
        try:
            for url in urls:
                await emitter(
                    {
                        "type": "source",
                        "data": {
                            "source": {"name": url, "id": url},
                            "document": [""],
                            "metadata": [{"source": url, "name": url, "url": url}],
                        },
                    }
                )
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
        except Exception:
            pass  # Event emission is best-effort

    # ──────────────────────────────────────────────
    #  Internal: Error formatting
    # ──────────────────────────────────────────────

    def _format_error(self, error: Exception, url: str) -> str:
        msg = str(error)
        if "Name or service not known" in msg or "nodename nor servname" in msg:
            return f"DNS error: Could not resolve hostname for {url}"
        if "Connection refused" in msg or "connect" in msg.lower():
            return f"Connection refused: Could not connect to {url}"
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return f"Timeout: Request to {url} timed out"
        if "403" in msg:
            return f"Access denied (403): {url} is blocking the request"
        if "404" in msg:
            return f"Not found (404): {url} does not exist"
        if "SSL" in msg or "certificate" in msg.lower():
            return f"TLS/SSL error: Could not establish secure connection to {url}"
        return f"Request failed: {msg[:200]}"


