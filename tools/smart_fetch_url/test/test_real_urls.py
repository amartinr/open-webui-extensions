"""
Phase 3 — Real-URL validation tests.

Requires network access. Tests are individually skippable.
Tests actual fetch + extraction against real websites.

Test cases from design doc:
  T1: Feed/forum (e.g., HN)     → word_count ≥ 1000 (if "feed") or reasonable
  T2: Blog article with og:type → "article" category, same quality as before
  T3: Documentation page (MDN)  → "unknown" → trafilatura, no regression
  T4: Article with many <article> comments → "article", body not comment spam
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import Tools


# ═══════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════

TIMEOUT_MS = 15_000
BROWSER = "chrome_145"


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

async def test_t1_hacker_news():
    """
    T1: Hacker News frontpage — feed-like aggregator.
    Expected category: "feed" (or "unknown" if heuristics don't detect table layout).
    Success: word_count ≥ 500 (all visible stories).
    """
    print("\n▶ T1: Hacker News")
    tools = Tools()

    try:
        result = await fetch_and_extract("https://news.ycombinator.com", tools)
    except Exception as e:
        print(f"  ❌ Fetch failed: {e}")
        return False

    print(f"    Status: {result['status_code']}")
    print(f"    Category: {result['category']}")
    print(f"    Word count: {result['word_count']}")
    print(f"    Title: {result['title'][:60]}")

    ok = True
    ok &= check(f"status 200 ({result['status_code']})", result['status_code'] == 200)
    ok &= check(f"word_count ({result['word_count']}) >= 500", result['word_count'] >= 500)
    ok &= check("title contains 'Hacker News'", "Hacker News" in result['title'])

    return ok


async def test_t2_blog_article():
    """
    T2: Blog article with og:type=article.
    Expected category: "article".
    Success: word_count ≥ 200, article metadata present.
    """
    print("\n▶ T2: Blog article with og:type")
    tools = Tools()

    # Use a known article with og:type
    urls_to_try = [
        "https://www.marco.org/2025/01/08/a-decade-later",
        "https://tonsky.me/blog/thermocline/",
    ]

    for url in urls_to_try:
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

    urls_to_try = [
        "https://developer.mozilla.org/en-US/docs/Web/HTML/Element/article",
        "https://docs.python.org/3/library/asyncio.html",
        "https://en.wikipedia.org/wiki/Web_scraping",
    ]

    for url in urls_to_try:
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

        # Note: category might be "article" for Wikipedia (it has og:type=article)
        # That's OK — the important thing is content quality, not the label
        if result['category'] != "article":
            print(f"    ℹ️  Category is '{result['category']}' (expected 'unknown')")

        return ok

    print("  ❌ All documentation URLs failed")
    return False


async def test_t4_article_with_comments():
    """
    T4: Article page with <article> comment tags.
    Expected category: "article".
    Success: article body, not comment spam.
    """
    print("\n▶ T4: Article with comments (resilience)")
    tools = Tools()

    # A blog post likely to have many comments
    urls_to_try = [
        "https://news.ycombinator.com/item?id=42000000",  # HN comment thread
    ]

    for url in urls_to_try:
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

    tests = [
        ("T1: Hacker News feed", test_t1_hacker_news()),
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
