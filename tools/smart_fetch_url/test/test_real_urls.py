"""
Phase 3 — Real-URL validation tests.

Requires network access. Tests are individually skippable.
Tests actual fetch + extraction against real websites.

Test cases:
  T1: Feed listing (Redlib/Reddit frontend) → "feed", word_count ≥ 1000
  T2: Blog article with og:type             → "article", word_count ≥ 200
  T3: Documentation page (MDN)              → "unknown" → trafilatura, no regression
  T4: HN comment thread                     → content present, not just first comment
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import Tools


# ═══════════════════════════════════════════════
#  Configuration — override these for your env
# ═══════════════════════════════════════════════

# Redlib instance (lightweight Reddit frontend, no JS)
REDLIB_URL = "http://redlib.private/r/LocalLLaMa/"

# Article URLs to try (first one that responds 200 wins)
ARTICLE_URLS = [
    "https://martinfowler.com/bliki/Serverless.html",
    "https://www.joelonsoftware.com/2000/08/09/the-joel-test-12-steps-to-better-code/",
]

# Documentation URLs
DOCS_URLS = [
    "https://developer.mozilla.org/en-US/docs/Web/HTML/Element/article",
    "https://docs.python.org/3/library/asyncio.html",
    "https://en.wikipedia.org/wiki/Web_scraping",
]

# Comment thread URLs
COMMENT_URLS = [
    "https://news.ycombinator.com/item?id=42000000",
]

TIMEOUT_MS = 15_000
BROWSER = "chrome"


# ═══════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════

async def fetch_and_extract(url: str, tools: Tools) -> dict:
    """Fetch a URL and return _extract_content result with metadata."""
    result = await tools._fetch_with_fingerprint(
        url=url,
        browser=BROWSER,
        timeout_ms=TIMEOUT_MS,
    )
    raw_html = result.raw_html
    final_url = result.final_url
    status_code = result.status_code
    content_type = result.content_type

    category_name, _ = await tools._detect_content_type(raw_html)

    extracted = await tools._extract_content(
        raw_html=raw_html,
        url=final_url,
        format="markdown",
    )

    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "category": category_name,
        "title": extracted.get("title", ""),
        "word_count": extracted.get("word_count", 0),
        "content": extracted.get("content", ""),
    }


# ═══════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════

@pytest.mark.skipif(not REDLIB_URL, reason="REDLIB_URL not configured")
async def test_t1_feed_listing():
    """
    T1: Feed listing page (Redlib/Reddit frontend).
    Expected category: "feed".
    Success: word_count >= 1000 (all visible posts), .post elements detected.
    """
    tools = Tools()
    result = await fetch_and_extract(REDLIB_URL, tools)

    assert result["status_code"] == 200, f"Expected 200, got {result['status_code']}"
    assert result["category"] == "feed", f"Expected 'feed', got '{result['category']}'"
    assert result["word_count"] >= 1000, f"Expected >= 1000 words, got {result['word_count']}"
    assert result["title"], "Title should not be empty"


async def test_t2_blog_article():
    """
    T2: Blog article with og:type=article.
    Expected category: "article".
    Success: word_count >= 200, article metadata present.
    """
    tools = Tools()
    last_error = None

    for url in ARTICLE_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            last_error = e
            continue

        assert result["status_code"] == 200, f"Expected 200, got {result['status_code']}"
        assert result["word_count"] >= 200, f"Expected >= 200 words, got {result['word_count']}"
        assert result["title"], "Title should not be empty"
        return

    pytest.fail(f"All article URLs failed. Last error: {last_error}")


async def test_t3_documentation():
    """
    T3: Documentation page (MDN-style).
    Expected category: "unknown" (no feed/article signals).
    Success: trafilatura extracts meaningful content (no regression).
    """
    tools = Tools()
    last_error = None

    for url in DOCS_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            last_error = e
            continue

        assert result["status_code"] == 200, f"Expected 200, got {result['status_code']}"
        assert result["word_count"] >= 100, f"Expected >= 100 words, got {result['word_count']}"
        assert result["title"], "Title should not be empty"
        return

    pytest.fail(f"All documentation URLs failed. Last error: {last_error}")


async def test_t4_article_with_comments():
    """
    T4: HN comment thread — many comments, should still work.
    Expected category: "unknown" (HN uses table layout).
    Success: content present, word_count > 0.
    """
    tools = Tools()
    last_error = None

    for url in COMMENT_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            last_error = e
            continue

        assert result["status_code"] == 200, f"Expected 200, got {result['status_code']}"
        assert result["word_count"] > 0, f"Expected > 0 words, got {result['word_count']}"
        assert result["title"], "Title should not be empty"
        return

    pytest.fail(f"All comment URLs failed. Last error: {last_error}")
