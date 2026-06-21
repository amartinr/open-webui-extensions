"""
Phase 1 — Unit tests for _detect_content_type().

Covers all 10 signals (S1–S10), the decision logic, and edge cases.
No network calls — pure HTML parsing via selectolax.
"""

import asyncio
import sys
from pathlib import Path

# Allow importing smart_fetch_url from parent directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import Tools


# ═══════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════

def make_html(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def repeat(tag: str, n: int, attrs: str = "", text: str = "x") -> str:
    """Generate n repeated HTML elements."""
    if text:
        if attrs:
            return "".join(f"<{tag} {attrs}>{text}</{tag}>" for _ in range(n))
        else:
            return "".join(f"<{tag}>{text}</{tag}>" for _ in range(n))
    else:
        return "".join(f"<{tag} {attrs} />" if attrs else f"<{tag} />" for _ in range(n))


async def detect(html: str) -> str:
    tools = Tools()
    return await tools._detect_content_type(html)


def check(name: str, result: str, expected: str) -> bool:
    ok = result == expected
    status = "✅" if ok else "❌"
    print(f"  {status} {name}: got '{result}', expected '{expected}'")
    return ok


# ═══════════════════════════════════════════════
#  Test cases
# ═══════════════════════════════════════════════

async def test_trivial():
    print("\n--- Trivial / empty ---")
    ok = True
    ok &= check("empty body", await detect("<html></html>"), "unknown")
    ok &= check("no signals", await detect(make_html("<p>Hello</p>")), "unknown")
    return ok


async def test_s1_post_elements():
    """
    S1: .post, .entry, .item, .thread, .topic

    ≥5 → +2,  ≥3 → +1
    """
    print("\n--- S1: Post-like elements ---")
    ok = True

    # 2 posts → no signal
    ok &= check("2 .post",
        await detect(make_html(repeat("div", 2, 'class="post"', "x"))), "unknown")

    # 3 posts → +1 (not enough alone)
    ok &= check("3 .post (low threshold, need more signals)",
        await detect(make_html(repeat("div", 3, 'class="post"', "x"))), "unknown")

    # 5 posts → +2 (not enough alone)
    ok &= check("5 .post (high threshold, need more signals)",
        await detect(make_html(repeat("div", 5, 'class="post"', "x"))), "unknown")

    # 5 posts + dual pagination = 2 + 2 = 4 → feed
    html = make_html(
        repeat("div", 5, 'class="post"', "x")
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("5 .post + dual pagination → feed", await detect(html), "feed")

    # 5 .item instead of .post
    html = make_html(
        repeat("div", 5, 'class="item"', "x")
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("5 .item + pagination → feed", await detect(html), "feed")

    # 5 .thread
    html = make_html(
        repeat("div", 5, 'class="thread"', "x")
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("5 .thread + pagination → feed", await detect(html), "feed")

    # 5 .topic
    html = make_html(
        repeat("div", 5, 'class="topic"', "x")
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("5 .topic + pagination → feed", await detect(html), "feed")

    return ok


async def test_s2_article_tags():
    """
    S2: ≥3 <article> tags → +1
    """
    print("\n--- S2: Multiple <article> tags ---")
    ok = True

    ok &= check("2 <article> (no signal)",
        await detect(make_html(repeat("article", 2, text="x"))), "unknown")

    ok &= check("3 <article> (+1, need more signals)",
        await detect(make_html(repeat("article", 3, text="x"))), "unknown")

    # 3 articles + container = 1 + 2 = 3 → still not enough
    html = make_html(repeat("article", 3, text="x") + '<div id="posts"></div>')
    ok &= check("3 <article> + #posts container (need more)",
        await detect(html), "unknown")

    # 5 articles + container + pagination = 1 + 2 + 2 = 5 → feed
    html = make_html(
        repeat("article", 5, text="x")
        + '<div id="posts"></div>'
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("5 <article> + #posts + pagination → feed",
        await detect(html), "feed")

    return ok


async def test_s3_feed_container():
    """
    S3: #posts, #feed, .feed, #threads, .listing, .thread-list, .post-list → +2
    """
    print("\n--- S3: Feed containers ---")
    ok = True

    for container_id in ("#posts", "#feed", "#threads", ".listing",
                         ".thread-list", ".post-list"):
        tag = "div"
        sel = container_id
        if container_id.startswith("."):
            attr = f'class="{container_id[1:]}"'
        else:
            attr = f'id="{container_id[1:]}"'

        # Container alone = +2, need 2 more points
        html = make_html(
            f'<{tag} {attr}></{tag}>'
            + repeat("div", 5, 'class="post"', "x")
        )
        ok &= check(f"5 .post + {sel} = 2+2=4 → feed",
            await detect(html), "feed")

    return ok


async def test_s4_dual_pagination():
    """
    S4: rel="next" AND rel="prev" → +2
    """
    print("\n--- S4: Dual pagination ---")
    ok = True

    pagination = '<link rel="next" href="/2"><link rel="prev" href="/1">'

    # Pagination alone = +2, need 2 more
    ok &= check("pagination + 0 posts (not enough)",
        await detect(make_html(pagination)), "unknown")

    # Pagination + 5 posts = 2 + 2 = 4 → feed
    ok &= check("pagination + 5 .post → feed",
        await detect(make_html(pagination + repeat("div", 5, 'class="post"', "x"))), "feed")

    return ok


async def test_s5_page_params():
    """
    S5: ≥2 <a href> with ?page=, &page=, ?after=, etc. → +1
    """
    print("\n--- S5: Page URL params ---")
    ok = True

    links = '<a href="?page=1">1</a><a href="?page=2">2</a>'
    html = make_html(links + repeat("div", 5, 'class="post"', "x"))
    # S5 +1, S1 +2 = 3, not enough
    ok &= check("5 .post + 2 page links (need 1 more point)",
        await detect(html), "unknown")

    # Add pagination too: S5 +1, S1 +2, S4 +2 = 5 → feed
    html = make_html(
        links
        + repeat("div", 5, 'class="post"', "x")
        + '<link rel="next" href="/3"><link rel="prev" href="/1">'
    )
    ok &= check("5 .post + page links + pagination → feed",
        await detect(html), "feed")

    return ok


async def test_s6_repeated_ids():
    """
    S6: Repeated ID bases (≥3 occurrences each).
    ≥2 bases → +2,  ≥1 base → +1
    """
    print("\n--- S6: Repeated ID bases ---")
    ok = True

    # One base repeated 3 times → +1
    html = make_html("".join(f'<div id="comment-{i}">c</div>' for i in range(1, 4)))
    ok &= check("3x comment-N (+1, not enough alone)",
        await detect(html), "unknown")

    # Two different bases each repeated 3 times → +2
    html = make_html(
        "".join(f'<div id="comment-{i}">c</div>' for i in range(1, 4))
        + "".join(f'<div id="post-{i}">p</div>' for i in range(1, 4))
    )
    ok &= check("3x comment-N + 3x post-N (+2, not enough alone)",
        await detect(html), "unknown")

    # Two bases + pagination = 2 + 2 = 4 → feed
    html = make_html(
        "".join(f'<div id="comment-{i}">c</div>' for i in range(1, 4))
        + "".join(f'<div id="post-{i}">p</div>' for i in range(1, 4))
        + '<link rel="next" href="/2"><link rel="prev" href="/1">'
    )
    ok &= check("repeated IDs + pagination → feed",
        await detect(html), "feed")

    # Blacklisted IDs should NOT count
    html = make_html(
        "".join(f'<div id="header-{i}">h</div>' for i in range(1, 6))
    )
    # "header" is blacklisted → no signal
    ok &= check("5x header-N (blacklisted → no signal)",
        await detect(html), "unknown")

    return ok


async def test_s7_og_type_article():
    """
    S7: og:type="article" → +3
    """
    print("\n--- S7: og:type=article ---")
    ok = True

    html = '<html><head><meta property="og:type" content="article"></head><body></body></html>'
    # +3, but needs ≥3, so if article_score >= 3 AND >= feed_score → "article"
    ok &= check("og:type=article alone (+3 ≥ 3, feed 0) → article",
        await detect(html), "article")

    return ok


async def test_s8_article_meta():
    """
    S8: article:* meta tags → +1
    """
    print("\n--- S8: article:* meta ---")
    ok = True

    html = '<html><head><meta property="article:published_time" content="2026-01-01"></head><body></body></html>'
    # +1, not enough alone
    ok &= check("article:published_time alone (+1, needs ≥3)",
        await detect(html), "unknown")

    # article meta + og:type = 1 + 3 = 4 → article
    html = '<html><head><meta property="og:type" content="article"><meta property="article:published_time" content="2026-01-01"></head><body></body></html>'
    ok &= check("og:type + article:meta → article",
        await detect(html), "article")

    return ok


async def test_s9_schema_article():
    """
    S9: Schema.org Article itemtype → +2
    """
    print("\n--- S9: Schema.org Article ---")
    ok = True

    html = make_html('<div itemtype="https://schema.org/Article">Content</div>')
    # +2, needs ≥3 or tiebreak
    ok &= check("schema.org Article alone (+2 < 3)",
        await detect(html), "unknown")

    # schema + article meta = 2 + 1 = 3 → article
    html = make_html(
        '<div itemtype="https://schema.org/Article">Content</div>'
        + '<meta property="article:published_time" content="2026-01-01">'
    )
    # But wait - <meta> in body is unusual. Let's put it in head properly
    html2 = '<html><head><meta property="article:published_time" content="2026-01-01"></head><body><div itemtype="https://schema.org/Article">Content</div></body></html>'
    ok &= check("schema.org + article:meta = 3 → article",
        await detect(html2), "article")

    return ok


async def test_s10_single_article():
    """
    S10: 1 <article> + ≤2 post-like elements → +2
    """
    print("\n--- S10: Single article + few posts ---")
    ok = True

    # 1 <article>, 0 post-like → +2 (but needs ≥3)
    html = make_html('<article>Content</article>')
    ok &= check("1 <article> alone (+2 < 3)",
        await detect(html), "unknown")

    # 1 <article> + 0 post-like + og:type = 2 + 3 = 5 → article
    html = '<html><head><meta property="og:type" content="article"></head><body><article>Content</article></body></html>'
    ok &= check("1 <article> + og:type → article",
        await detect(html), "article")

    # 1 <article> + 1 post-like (≤2) → S10 still triggers
    html = '<html><head><meta property="og:type" content="article"></head><body><article>Content</article><div class="post">x</div></body></html>'
    ok &= check("1 <article> + 1 .post + og:type → article",
        await detect(html), "article")

    # 1 <article> + 3 post-like → S10 does NOT trigger (≤2 violated)
    html = '<html><head><meta property="og:type" content="article"></head><body><article>Content</article><div class="post">x</div><div class="post">y</div><div class="post">z</div></body></html>'
    # S7 +3, S10 doesn't trigger (3 posts > 2), total article = 3 → still article by S7 alone
    ok &= check("1 <article> + 3 .post + og:type (S7=3, S10 no) → article",
        await detect(html), "article")

    return ok


async def test_feed_vs_article_tiebreak():
    """
    When both feed and article scores are equal, the higher absolute score wins.
    """
    print("\n--- Feed vs Article tiebreaking ---")
    ok = True

    # 5 .post + og:type = feed 2, article 3 → article wins
    html = '<html><head><meta property="og:type" content="article"></head><body>' + repeat("div", 5, 'class="post"', "x") + '</body></html>'
    ok &= check("5 .post (f=2) + og:type (a=3) → article wins",
        await detect(html), "article")

    # 5 .post + pagination + og:type = feed 4, article 3 → feed wins (4 >= 3 AND 4 >= 4)
    html = '<html><head><meta property="og:type" content="article"></head><body>' + repeat("div", 5, 'class="post"', "x") + '<link rel="next" href="/2"><link rel="prev" href="/1"></body></html>'
    ok &= check("5 .post + pagination (f=4) + og:type (a=3) → feed wins",
        await detect(html), "feed")

    return ok


async def test_article_with_comments():
    """
    Article with many <article> comment tags should stay 'article', not flip to 'feed'.
    S10 guards against this: 1 <article> + ≤2 post-like → +2 for article.
    S2 (≥3 <article>) would push toward feed, but S7/S9/S10 override.
    """
    print("\n--- Article with comments (resilience) ---")
    ok = True

    # An article with og:type and many comment <article> tags
    # 5 <article> tags → S2 +1 for feed
    # og:type → S7 +3 for article
    # Equal? No, article 3 > feed 1 → article wins
    html = '<html><head><meta property="og:type" content="article"></head><body><article>Main article</article><article>Comment 1</article><article>Comment 2</article><article>Comment 3</article><article>Comment 4</article></body></html>'
    ok &= check("og:type + 5 <article> (comments) → article (3 > 1)",
        await detect(html), "article")

    return ok


async def test_raw_format_bypass():
    """
    format='raw' never reaches _extract_content, so the feed hook is irrelevant.
    This is verified implicitly by smart_fetch_url flow.
    """
    print("\n--- format='raw' bypass (smoke check) ---")
    print("  (no-op: verified by smart_fetch_url flow, not _detect_content_type)")
    return True


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("Phase 1 — _detect_content_type() unit tests")
    print("=" * 60)

    tests = [
        ("trivial", test_trivial()),
        ("S1 post elements", test_s1_post_elements()),
        ("S2 article tags", test_s2_article_tags()),
        ("S3 feed container", test_s3_feed_container()),
        ("S4 dual pagination", test_s4_dual_pagination()),
        ("S5 page params", test_s5_page_params()),
        ("S6 repeated IDs", test_s6_repeated_ids()),
        ("S7 og:type=article", test_s7_og_type_article()),
        ("S8 article meta", test_s8_article_meta()),
        ("S9 schema article", test_s9_schema_article()),
        ("S10 single article", test_s10_single_article()),
        ("tiebreaking", test_feed_vs_article_tiebreak()),
        ("article with comments resilience", test_article_with_comments()),
        ("raw format bypass", test_raw_format_bypass()),
    ]

    passed = 0
    failed = 0
    for name, coro in tests:
        if await coro:
            passed += 1
        else:
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
