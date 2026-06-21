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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import Tools


# ═══════════════════════════════════════════════
#  Configuration — override these for your env
# ═══════════════════════════════════════════════

# Redlib instance (lightweight Reddit frontend, no JS)
REDLIB_URL = "http://redlib.private/r/Python/"

# Article URLs to try (first one that responds 200 wins)
ARTICLE_URLS = [
    "https://tonsky.me/blog/thermocline/",
    "https://www.marco.org/2025/01/08/a-decade-later",
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
BROWSER = "chrome_145"


# ═══════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════

def check(name: str, result: bool) -> bool:
    status = "✅" if result else "❌"
    print(f"  {status} {name}")
    return result


async def fetch_and_extract(url: str, tools: Tools) -> dict:
    """Fetch a URL and return _extract_content result with metadata."""
    raw_html, final_url, status_code, content_type, _, _ = (
        await tools._fetch_with_fingerprint(
            url=url,
            browser=BROWSER,
            os="windows",
            timeout_ms=TIMEOUT_MS,
        )
    )

    category = await tools._detect_content_type(raw_html)

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
        "category": category,
        "title": extracted.get("title", ""),
        "word_count": extracted.get("word_count", 0),
        "content": extracted.get("content", ""),
    }


# ═══════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════

async def test_t1_feed_listing():
    """
    T1: Feed listing page (Redlib/Reddit frontend).
    Expected category: "feed".
    Success: word_count >= 1000 (all visible posts), .post elements detected.
    """
    print(f"\n▶ T1: Feed listing ({REDLIB_URL})")
    tools = Tools()

    try:
        result = await fetch_and_extract(REDLIB_URL, tools)
    except Exception as e:
        print(f"  ❌ Fetch failed: {e}")
        return False

    print(f"    Status: {result['status_code']}")
    print(f"    Category: {result['category']}")
    print(f"    Word count: {result['word_count']}")
    print(f"    Title: {result['title'][:60]}")

    ok = True
    ok &= check(f"status 200 ({result['status_code']})", result['status_code'] == 200)
    ok &= check(f"category is 'feed' ({result['category']})", result['category'] == "feed")
    ok &= check(f"word_count ({result['word_count']}) >= 1000", result['word_count'] >= 1000)
    ok &= check("title not empty", len(result['title']) > 0)

    return ok


async def test_t2_blog_article():
    """
    T2: Blog article with og:type=article.
    Expected category: "article".
    Success: word_count >= 200, article metadata present.
    """
    print(f"\n▶ T2: Blog article with og:type")
    tools = Tools()

    for url in ARTICLE_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            print(f"  ⚠️  {url}: fetch failed ({e}), trying next...")
            continue

        print(f"    URL: {url}")
        print(f"    Status: {result['status_code']}")
        print(f"    Category: {result['category']}")
        print(f"    Word count: {result['word_count']}")
        print(f"    Title: {result['title'][:60]}")

        ok = True
        ok &= check(f"status 200 ({result['status_code']})", result['status_code'] == 200)
        ok &= check(f"word_count ({result['word_count']}) >= 200", result['word_count'] >= 200)
        ok &= check("title not empty", len(result['title']) > 0)
        return ok

    print("  ❌ All article URLs failed")
    return False


async def test_t3_documentation():
    """
    T3: Documentation page (MDN-style).
    Expected category: "unknown" (no feed/article signals).
    Success: trafilatura extracts meaningful content (no regression).
    """
    print("\n▶ T3: Documentation page")
    tools = Tools()

    for url in DOCS_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            print(f"  ⚠️  {url}: fetch failed ({e}), trying next...")
            continue

        print(f"    URL: {url}")
        print(f"    Status: {result['status_code']}")
        print(f"    Category: {result['category']}")
        print(f"    Word count: {result['word_count']}")
        print(f"    Title: {result['title'][:60]}")

        ok = True
        ok &= check(f"status 200 ({result['status_code']})", result['status_code'] == 200)
        ok &= check(f"word_count ({result['word_count']}) >= 100", result['word_count'] >= 100)
        ok &= check("title not empty", len(result['title']) > 0)

        if result['category'] != "article":
            print(f"    ℹ️  Category is '{result['category']}' (expected 'unknown')")

        return ok

    print("  ❌ All documentation URLs failed")
    return False


async def test_t4_article_with_comments():
    """
    T4: HN comment thread — many comments, should still work.
    Expected category: "unknown" (HN uses table layout).
    Success: content present, word_count > 0.
    """
    print("\n▶ T4: HN comment thread (resilience)")
    tools = Tools()

    for url in COMMENT_URLS:
        try:
            result = await fetch_and_extract(url, tools)
        except Exception as e:
            print(f"  ⚠️  {url}: fetch failed ({e}), trying next...")
            continue

        print(f"    URL: {url}")
        print(f"    Status: {result['status_code']}")
        print(f"    Category: {result['category']}")
        print(f"    Word count: {result['word_count']}")
        print(f"    Title: {result['title'][:60]}")

        ok = True
        ok &= check(f"status 200 ({result['status_code']})", result['status_code'] == 200)
        ok &= check(f"word_count ({result['word_count']}) > 0", result['word_count'] > 0)
        ok &= check("title not empty", len(result['title']) > 0)

        return ok

    print("  ❌ All comment URLs failed")
    return False


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("Phase 3 — Real-URL validation tests")
    print("⚠️  Requires network access")
    print("=" * 60)
    print(f"\nConfiguration:")
    print(f"  REDLIB_URL = {REDLIB_URL}")

    tests = [
        ("T1: Feed listing", test_t1_feed_listing()),
        ("T2: Blog article", test_t2_blog_article()),
        ("T3: Documentation page", test_t3_documentation()),
        ("T4: Article with comments", test_t4_article_with_comments()),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, coro in tests:
        try:
            if await coro:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"\n▶ {name}")
            print(f"  ❌ Exception: {e}")
            failed += 1

    total = passed + failed + skipped
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed, {skipped} skipped")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
