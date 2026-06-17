"""
title: Smart Fetch URL
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions
description: Always preferred over 'fetch_url'. Fetches URLs with TLS fingerprinting to avoid blocks, returns clean content with metadata. Use by default.
required_open_webui_version: 0.4.0
requirements: curl_cffi, trafilatura, selectolax
version: 0.4.1
licence: MIT

Based on pi-smart-fetch (MIT) by Thinkscape
Copyright (c) 2026 Thinkscape
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
                       "text" (plain text), "json" (structured), or "raw" (full server response)
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

        # Validate
        if not url or not url.strip():
            return "Error: URL is required."

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return f"Error: Invalid URL protocol. Only http/https are supported: {url}"

        try:
            raw_html, final_url, status_code, content_type, resp_headers = (
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

            # Step 2: Handle raw format early — return the full server response
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
                if show_favicons:
                    await self._emit_sources(__event_emitter__, [final_url])
                return result

            # Step 3: Extract content
            extracted = self._extract_content(
                raw_html=raw_html,
                url=final_url,
                format=format,
                remove_images=remove_images,
                include_replies=include_replies,
            )

            # Step 4: Alternate content fallback for thin/no content
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

            # Step 5: Truncate
            content = extracted.get("content", "")
            if max_chars and len(content) > max_chars:
                content = content[:max_chars]

            # Step 6: Build result
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

            # Step 7: Emit source events for Open WebUI's Citations component (bottom of message)
            visited_urls = self._collect_visited_urls(url, final_url, alternate_urls)
            if show_favicons:
                await self._emit_sources(__event_emitter__, visited_urls)

            return result

        except Exception as e:
            error_msg = self._format_error(e, url)
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
        :param format: Output format: "markdown", "html", "text", "json", or "raw"
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
                    return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{result}\n\n---\n"
                except Exception as e:
                    return f"## [{index + 1}/{len(urls)}] {single_url}\n\nError: {self._format_error(e, single_url)}\n\n---\n"

        tasks = [fetch_one(i, u) for i, u in enumerate(urls)]
        results = await asyncio.gather(*tasks)

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
    ) -> tuple[str, str, int, str, dict]:
        """
        Perform the actual HTTP request with TLS fingerprinting.

        Returns: (raw_html, final_url, status_code, content_type, response_headers)
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
            headers=headers,
            timeout=timeout_sec,
            proxies=proxy,
        ) as session:
            resp = await session.get(url, allow_redirects=True)

            raw_html = resp.text
            final_url = str(resp.url)
            status_code = resp.status_code
            content_type = resp.headers.get("content-type", "") or ""
            resp_headers = dict(resp.headers)

            return raw_html, final_url, status_code, content_type, resp_headers

    async def _fetch_with_httpx(
        self,
        url: str,
        headers: dict,
        timeout_ms: int,
        proxy: Optional[str] = None,
    ) -> tuple[str, str, int, str, dict]:
        """Fallback fetch using httpx (no TLS fingerprinting)."""
        import httpx

        client_kwargs = {
            "headers": headers,
            "follow_redirects": True,
            "timeout": timeout_ms / 1000,
        }
        if proxy:
            client_kwargs["proxies"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            raw_html = resp.text
            final_url = str(resp.url)
            status_code = resp.status_code
            content_type = resp.headers.get("content-type", "") or ""
            resp_headers = dict(resp.headers)

            return raw_html, final_url, status_code, content_type, resp_headers

    # ──────────────────────────────────────────────
    #  Internal: Content extraction
    # ──────────────────────────────────────────────

    def _extract_content(
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
        """
        content = None
        metadata = {}

        # Try trafilatura first (best extraction quality)
        try:
            import trafilatura
            from trafilatura.settings import use_config

            config = use_config()
            config.set("DEFAULT", "EXTRACTION_CLEANWISE", "on")

            # Extract with metadata
            # Map user-facing format names to trafilatura's expected values
            trafilatura_output_format = "txt" if format == "text" else format
            if trafilatura_output_format not in ("markdown", "html", "txt"):
                trafilatura_output_format = "markdown"

            extracted = trafilatura.extract(
                raw_html,
                url=url,
                output_format=trafilatura_output_format,
                include_links=True,
                include_images=not remove_images,
                include_tables=True,
                include_comments=include_replies,
                with_metadata=True,
                config=config,
            )

            if extracted:
                content = extracted

            # Get metadata separately
            metadata = trafilatura.extract_metadata(raw_html, default_url=url)
        except ImportError:
            logger.warning("trafilatura not available, using basic extraction")
        except Exception as e:
            logger.warning(f"trafilatura extraction failed: {e}")

        # Fallback: basic extraction
        if not content and format != "json":
            content = self._basic_extract(raw_html, format, remove_images)

        # Fallback: just return raw text
        if not content:
            content = self._strip_html(raw_html)

        # Build metadata dict
        meta = {}
        if metadata:
            meta["title"] = getattr(metadata, "title", None) or ""
            meta["author"] = getattr(metadata, "author", None) or ""
            meta["site"] = getattr(metadata, "sitename", None) or ""
            meta["language"] = getattr(metadata, "language", None) or ""
            meta["published"] = getattr(metadata, "date", None) or ""
        else:
            # Try to extract from HTML
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

    def _basic_extract(self, html: str, format: str, remove_images: bool) -> str:
        """Basic HTML extraction fallback using selectolax or regex."""
        try:
            from selectolax.parser import HTMLParser

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

            tree = HTMLParser(raw_html)
            alternates = tree.css('link[rel="alternate"]')

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
                elif format in ("markdown", "text"):
                    if alt_type in ("text/markdown", "text/plain"):
                        candidates.append(alt_href)

            if not candidates:
                return {"content": "", "word_count": 0}, alternates_used

            # Resolve relative URLs
            from urllib.parse import urljoin

            for alt_url in candidates[:3]:
                resolved = urljoin(url, alt_url)
                try:
                    alt_raw, alt_final, _, _, _ = await self._fetch_with_fingerprint(
                        url=resolved,
                        browser=browser,
                        os=os,
                        timeout_ms=timeout_ms,
                        proxy=proxy,
                        headers=headers,
                        format=format,
                    )
                    alt_extracted = self._extract_content(
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


