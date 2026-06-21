# Content-Type Detection Heuristic for `smart_fetch_url`

Design, implementation plan, and verification for routing feed/forum/listing pages through selectolax instead of trafilatura.

---

## 1. Problem

`smart_fetch_url` currently uses **Trafilatura** as its primary HTML extractor. Trafilatura is designed to extract the **main content of single articles**, not feeds, forums, listings, or multi-entry front pages.

**Concrete example:** Fetching a feed-style page (e.g., a Reddit frontend, a news aggregator, or a forum thread listing):
- **Trafilatura** returns only the first post/entry (~200-300 words), ignoring the other ~20+ entries.
- **Selectolax** (the basic fallback) would return text from **all entries**, but it never activates because Trafilatura does return content (word_count >= 30).
- **`format="raw"`** returns the full HTML with all entries — no extraction at all.

Trafilatura's own docs acknowledge this: *"geared towards article pages, blog posts, main text parts. Results vary wildly on link lists, galleries or catalogs."*

---

## 2. Current vs Proposed Flow

### Current flow

```
Fetch → raw_html
  │
  ├─ format == "raw"  →  Return raw HTML (no extraction)
  │
  └─ format != "raw"  →  _extract_content()
                            │
                            └─ Trafilatura extraction
                                 │
                                 ├─ word_count < 30  →  _try_alternate_fallback()
                                 │
                                 └─ word_count >= 30  →  Use result (1 post only)
```

### Proposed flow

```
Fetch → raw_html
  │
  ├─ format == "raw"  →  Return raw HTML
  │
  └─ format != "raw"  →  _extract_content()
       │  (after type normalisation guard)
       │
       ├─ _detect_content_type(raw_html)
       │    ├─ "feed"    → _basic_extract() + regex metadata → return dict
       │    └─ "article" ─┐
       │    └─ "unknown" ─┤
       │                  ▼
       │         Trafilatura extraction (unchanged)
       │              │
       │              ├─ word_count < 30  →  _try_alternate_fallback()
       │              └─ word_count >= 30  →  Result
       │
       └─ (fallthrough to existing code)
```

---

## 3. Method Specification: `_detect_content_type()`

### 3.1 Signature and position

Add this as an instance method on `class Tools`, placed between `_extract_content` and `_basic_extract` (no callers until Phase 2).

```python
async def _detect_content_type(self, raw_html: str) -> str:
    """
    Classify a page as 'feed', 'article', or 'unknown'.

    Uses selectolax to parse and a heuristic scoring system.
    Runs in a thread to avoid blocking the event loop.

    Returns:
        "feed"    → page is a feed/forum/listing — use selectolax full extraction
        "article" → page is a single article — use trafilatura
        "unknown" → no clear signals — defer to default trafilatura behavior
    """
```

### 3.2 Scoring signals

**Feed signals (S1–S6):**

| # | Signal | Weight |
|---|--------|--------|
| S1 | `.post/.entry/.item/.thread/.topic` elements ≥5 / ≥3 | +2 / +1 |
| S2 | `<article>` tags ≥3 | +1 |
| S3 | Feed container (`#posts`, `.feed`, `#threads`, `.listing`, etc.) | +2 |
| S4 | Dual pagination (`rel="next"` + `rel="prev"`) | +2 |
| S5 | `?page=`/`?after=`/`?offset=` in ≥2 `<a href>` | +1 |
| S6 | ≥2 repeated ID bases / ≥1 repeated base (filtered through a blacklist) | +2 / +1 |

**Article signals (S7–S10):**

| # | Signal | Weight |
|---|--------|--------|
| S7 | `og:type="article"` | +3 |
| S8 | Any `<meta property="article:*">` | +1 |
| S9 | Schema.org `itemtype="...Article"` | +2 |
| S10 | Exactly 1 `<article>` + ≤2 post-like elements | +2 |

### 3.3 Decision logic

```python
if feed_score >= 4 and feed_score >= article_score:
    return "feed"
if article_score >= 3 and article_score >= feed_score:
    return "article"
return "unknown"
```

### 3.4 Full method code

```python
async def _detect_content_type(self, raw_html: str) -> str:
    """
    Classify a page as 'feed', 'article', or 'unknown'.

    Uses selectolax to parse and a heuristic scoring system.
    Runs in a thread to avoid blocking the event loop.

    Returns:
        "feed"    → page is a feed/forum/listing — use selectolax full extraction
        "article" → page is a single article — use trafilatura
        "unknown" → no clear signals — defer to default trafilatura behavior
    """
    def _detect():
        from selectolax.parser import HTMLParser
        tree = HTMLParser(raw_html)

        feed_score = 0
        article_score = 0

        # ── Feed signals ───────────────────────────────────────

        # S1: Multiple post-like elements (.post, .entry, .item, .thread, .topic)
        post_elements = len(tree.css(
            ".post, .entry, .item, .thread, .topic"
        ))
        if post_elements >= 5:
            feed_score += 2
        elif post_elements >= 3:
            feed_score += 1

        # S2: Multiple <article> tags (>= 3)
        article_tags = len(tree.css("article"))
        if article_tags >= 3:
            feed_score += 1

        # S3: Explicit feed container (#posts, #feed, .feed, #threads, etc.)
        feed_containers = len(tree.css(
            "#posts, #feed, .feed, #threads, "
            ".listing, .thread-list, .post-list"
        ))
        if feed_containers >= 1:
            feed_score += 2

        # S4: Dual pagination (rel="next" AND rel="prev")
        has_next = bool(tree.css('link[rel="next"], a[rel="next"]'))
        has_prev = bool(tree.css('link[rel="prev"], a[rel="prev"]'))
        if has_next and has_prev:
            feed_score += 2

        # S5: Pagination URL params in <a href> (?page=, ?after=, ?offset=)
        page_links = len(tree.css(
            'a[href*="?page="], a[href*="&page="], '
            'a[href*="?after="], a[href*="&after="], '
            'a[href*="?offset="], a[href*="&offset="]'
        ))
        if page_links >= 2:
            feed_score += 1

        # S6: Repeated ID base patterns (e.g. comment-1, comment-2, comment-3)
        #     with a blacklist of common single-use IDs.
        id_counts = {}
        ID_BASE_BLACKLIST = frozenset({
            "header", "footer", "nav", "sidebar", "main", "content",
            "wrapper", "container", "section", "page", "article",
            "body", "root", "app", "site", "menu", "modal",
        })
        for el in tree.css("[id]"):
            raw_id = el.attributes.get("id", "")
            if not raw_id:
                continue
            base = re.sub(r"\d+$", "", raw_id).rstrip("- _")
            if base and base.lower() not in ID_BASE_BLACKLIST:
                id_counts[base] = id_counts.get(base, 0) + 1
        repeated = sum(1 for c in id_counts.values() if c >= 3)
        if repeated >= 2:
            feed_score += 2
        elif repeated >= 1:
            feed_score += 1

        # ── Article signals ────────────────────────────────────

        # S7: Open Graph og:type="article"
        for meta in tree.css('meta[property="og:type"]'):
            if (meta.attributes.get("content") or "").lower() == "article":
                article_score += 3
                break

        # S8: Open Graph article:* meta tags (article:published_time, etc.)
        article_meta_count = len(tree.css('meta[property^="article:"]'))
        if article_meta_count >= 1:
            article_score += 1

        # S9: Schema.org Article type
        for el in tree.css("[itemtype]"):
            it = (el.attributes.get("itemtype") or "").lower()
            if "schema.org/article" in it:
                article_score += 2
                break

        # S10: Exactly one <article> AND <= 2 post-like elements
        if article_tags == 1 and post_elements <= 2:
            article_score += 2

        # ── Decision ───────────────────────────────────────────
        if feed_score >= 4 and feed_score >= article_score:
            return "feed"
        if article_score >= 3 and article_score >= feed_score:
            return "article"
        return "unknown"

    try:
        return await asyncio.to_thread(_detect)
    except Exception:
        logger.warning(
            "Content type detection failed, falling back to 'unknown'"
        )
        return "unknown"
```

### 3.5 Interaction with `_try_alternate_fallback()`

The alternate fallback is gated by `word_count < 30` in `smart_fetch_url()`. For the feed path, `_basic_extract()` returns hundreds or thousands of words, so `word_count >= 30` by default and the fallback never triggers. No changes needed to `_try_alternate_fallback()`.

---

## 4. Modification: `_extract_content()`

### 4.1 Hook location

After the `raw_html` type normalisation guard, before the `try: import trafilatura` block.

### 4.2 Code to add

```python
        # ── NEW: Detect content type for routing ─────────────────
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
```

### 4.3 What remains untouched

- `smart_fetch_url()` — unchanged. Still calls `_extract_content()` as before.
- `_basic_extract()` — unchanged. Still returns `str`.
- `_try_alternate_fallback()` — unchanged. Still gated by `word_count < 30`.
- Metadata helpers (`_extract_title`, `_extract_meta`, `_extract_language`) — unchanged.
- Valves, UserValves — unchanged.
- Output formats — unchanged. `_format_output()` works the same regardless of extractor.
- Dependencies — no new ones. `selectolax` is already listed in `requirements`.

---

## 5. Implementation Phases

### Phase 1 — Scaffolding: Add `_detect_content_type()`

**Task**: Insert the new method as dead code (no caller yet) between `_extract_content` and `_basic_extract`.

**Verification**:
- Module imports without errors
- `Tools()._detect_content_type("<html>...</html>")` returns `"unknown"` for trivial HTML
- `Tools()._detect_content_type("<html>...")` returns `"feed"` for HTML with ≥5 `.post` elements
- `Tools()._detect_content_type(...)` returns `"article"` for HTML with `og:type="article"` and 1 `<article>` tag

**Risk**: None — no caller, no behavioral change.

### Phase 2 — Integration: Hook into `_extract_content()`

**Task**: Add the content-type detection call and early return for `"feed"` after the `raw_html` type guard.

**Verification**:
- **Article** with `og:type="article"` → still extracted by trafilatura → same output as before
- **Feed** (e.g., Reddit, HN) → all visible content returned, not just the first post
- **Unknown** (e.g., MDN docs) → trafilatura, same as before
- **JSON format** feed → still works (early return returns dict, `_format_output` handles JSON)
- `format == "raw"` is unaffected (never reaches `_extract_content`)

**Risk**: Low. The early return is isolated; `"unknown"` and `"article"` fall through to identical existing logic.

### Phase 3 — Validation: Test with real URLs

**Test cases**:

| URL type | Expected category | Success criterion |
|----------|-------------------|-------------------|
| Feed/forum (e.g., old Reddit, HN) | `"feed"` | word_count ≥ 1000 (all visible entries) |
| Blog article with `og:type="article"` | `"article"` | Same extraction quality as before |
| Documentation page (MDN, ReadTheDocs) | `"unknown"` → trafilatura | No regression |
| Article with many `<article>` comments | `"article"` | Article body, not comment spam |

**Tuning**: If false positives/negatives appear, only adjust weights inside `_detect_content_type()`. Never touch `_extract_content()` or other methods.

---

## 6. What NOT to do

- ❌ Do **not** add new dependencies — `selectolax` is already available.
- ❌ Do **not** change `_basic_extract()` — it remains a `str`-returning helper.
- ❌ Do **not** change `_try_alternate_fallback()` — the word_count gate is sufficient.
- ❌ Do **not** pass `include_replies` to the feed path — in feeds, all content IS the content.
- ❌ Do **not** place the hook before the `raw_html` type normalisation guard.
- ❌ Do **not** use `re` on the main thread for detection — wrap in `asyncio.to_thread()`.
- ❌ Do **not** call `_basic_extract()` twice — detection uses its own lightweight parse.

---

## 7. Rollback Plan

Each phase is a single atomic edit:

| Phase | Rollback action |
|-------|-----------------|
| 1 | Remove `_detect_content_type()` method |
| 2 | Remove the hook + early return from `_extract_content()` |
| 3 | Revert weight changes in `_detect_content_type()` |

No cascading dependencies — Phase 2 does not depend on Phase 1 beyond the method existing (rollback of Phase 1 while keeping Phase 2 would cause a runtime `AttributeError`, easily caught).

---

## 8. Advantages

| Aspect | Improvement |
|--------|-------------|
| **Accuracy** | Feeds get full content; articles keep clean Trafilatura extraction |
| **Speed** | Selectolax is faster than Trafilatura for large feeds |
| **No regressions** | Articles still use Trafilatura — same quality |
| **No new dependencies** | Selectolax already listed in requirements |
| **Lightweight detection** | ~5ms parse vs ~200-500ms Trafilatura extraction |

## 9. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **False positive**: Article with many comments classified as feed | S7/S9/S10 (`og:type`, Schema, single `<article>`) override feed signals. `"unknown"` catch-all prevents regression. |
| **False negative**: Feed without standard CSS classes | Falls to `"unknown"` → same Trafilatura behavior as today. No regression. |
| **Double parse** (detection + extraction) | Detection is ~5ms vs Trafilatura ~200-500ms. Acceptable overhead. |
| **Heuristic maintenance** | Logic isolated in `_detect_content_type()` for easy future tuning. |
