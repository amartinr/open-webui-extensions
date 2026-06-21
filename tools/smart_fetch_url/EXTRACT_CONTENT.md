# Revised Proposal: Content-Type Detection Heuristic for `smart_fetch_url`

Blueprint-ready implementation plan — v2.0

---

## 1. Problem

`smart_fetch_url` currently uses **Trafilatura** as its primary HTML extractor. Trafilatura is designed to extract the **main content of single articles**, not feeds, forums, listings, or multi-entry front pages.

**Concrete example:** Fetching a feed-style page (e.g., a Reddit frontend, a news aggregator, or a forum thread listing):
- **Trafilatura** returns only the first post/entry (~200-300 words), ignoring the other ~20+ entries.
- **Selectolax** (the basic fallback) would return text from **all entries**, but it never activates because Trafilatura does return content (word_count >= 30).
- **`format="raw"`** returns the full HTML with all entries — no extraction at all.

Trafilatura's own docs acknowledge this: *"geared towards article pages, blog posts, main text parts. Results vary wildly on link lists, galleries or catalogs."*

---

## 2. Current Flow

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

---

## 3. Proposed Flow

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

## 4. New Method: `_detect_content_type()`

### 4.1 Position in the class

Add this as an instance method on `class Tools`, placed between `_extract_content` and `_basic_extract` (or anywhere logically grouped with the other extraction helpers).

### 4.2 Signature and structure

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

### 4.3 Scoring table summary

| # | Signal | Weight |
|---|---|---|
| S1 | `.post/.entry/.item/.thread/.topic` elements >= 5 / >= 3 | +2 / +1 |
| S2 | `<article>` tags >= 3 | +1 |
| S3 | Feed container (`#posts`, `.feed`, `#threads`, etc.) | +2 |
| S4 | Dual pagination (`rel="next"` + `rel="prev"`) | +2 |
| S5 | `?page=`/`?after=`/`?offset=` in >= 2 `<a href>` | +1 |
| S6 | >= 2 repeated ID bases / >= 1 repeated base | +2 / +1 |
| S7 | `og:type="article"` | +3 |
| S8 | Any `article:*` meta tag | +1 |
| S9 | Schema.org `itemtype="...Article"` | +2 |
| S10 | 1 `<article>` + <= 2 post-like elements | +2 |

**Decision logic:**
- If `feed_score >= 4` AND `feed_score >= article_score` → `"feed"`
- If `article_score >= 3` AND `article_score >= feed_score` → `"article"`
- Otherwise → `"unknown"`

---

## 5. Modification of `_extract_content()`

### 5.1 Exact hook location

The hook goes **after** the `raw_html` type normalisation guard and **before** the `try: import trafilatura` block. Current structure of `_extract_content()` (lines 643-718 in `smart_fetch_url.py` v0.4.6):

```
L643:   content = None
L644:   doc = None
L646:   # Guard: normalise raw_html to str/bytes
L652:   # Try trafilatura first
L653:   try:
L660:       def _do_extract(): ...
L665:       doc = await asyncio.to_thread(_do_extract)
L672:   except ImportError: ...
L674:   except Exception: ...
L677:   # Fallback: basic extraction
L680:   # Fallback: strip_html
L683:   # Build metadata dict
L718:   # Return dict
```

The content-type detection goes between **L651** (end of guard) and **L652** (start of trafilatura try block).

### 5.2 Modified method

Replace the body of `_extract_content()` (lines 643-718) with:

```python
        content = None
        doc = None

        # Guard: trafilatura expects str or bytes — normalise anything else
        if not isinstance(raw_html, (str, bytes)):
            logger.warning(
                "unexpected raw_html type %s, coercing to empty string",
                type(raw_html).__name__,
            )
            raw_html = str(raw_html) if raw_html is not None else ""

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
        try:
            import trafilatura
            from trafilatura.core import extract_with_metadata

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
```

### 5.3 Interaction with `_try_alternate_fallback()`

No changes needed. The alternate fallback is gated by `word_count < 30` in `smart_fetch_url()` (line ~333). For the feed path, `_basic_extract()` returns hundreds or thousands of words, so `word_count >= 30` by default and the fallback never triggers. This is implicit and correct.

---

## 6. No changes outside these two methods

- **`smart_fetch_url()`** (main entry point, line ~200): unchanged. Still calls `_extract_content()` as before.
- **`_basic_extract()`** (line ~720): unchanged. Still returns `str`.
- **`_try_alternate_fallback()`** (line ~814): unchanged. Still gated by `word_count < 30`.
- **Metadata helpers** (`_extract_title`, `_extract_meta`, `_extract_language`, lines ~1185-1212): unchanged — used by the feed path.
- **Valves, UserValves**: unchanged.
- **Output formats**: unchanged — `_format_output()` works the same regardless of which extractor was used.
- **Dependencies**: no new ones — `selectolax` is already listed in `requirements`.

---

## 7. Test cases with quantitative criteria

| URL | Expected | Key signals | Success criteria |
|-----|----------|-------------|------------------|
| `https://old.reddit.com/r/all` | `"feed"` | Multiple `.entry`, `.thing`, dual pagination | word_count >= 1000 (all visible entries) |
| `https://news.ycombinator.com/` | `"feed"` | Multiple `tr.athing`, repeated numeric IDs | word_count >= 2000 (all stories on front page) |
| Blog article with `og:type="article"` | `"article"` | `og:type="article"`, 1 `<article>` tag | Same extraction quality as current trafilatura |
| Documentation page (e.g., MDN, ReadTheDocs) | `"unknown"` → trafilatura | No clear feed or article signals | No regression vs. current behavior |
| Blog article with many `<article>` comments | `"article"` | 1 `<article>` + `og:type` overrides comment count | Article body extracted, not comment spam |

---

## 8. Implementation order

1. **Add `_detect_content_type()`** as a new instance method on `class Tools`, between `_extract_content` and `_basic_extract`.
2. **Modify `_extract_content()`** inserting the content-type detection hook and early return for `"feed"`.
3. **Test** each case from section 7 manually.
4. **Tune weights** if false positives/negatives appear in real-world usage.
5. **Consider removing or lowering S1** (`.post` class) if WordPress blogs with comment `.post` elements cause too many false positives.

---

## 9. What NOT to do

- ❌ Do **not** add new dependencies — `selectolax` is already available.
- ❌ Do **not** change `_basic_extract()` — it remains a `str`-returning helper.
- ❌ Do **not** change `_try_alternate_fallback()` — the word_count gate is sufficient.
- ❌ Do **not** pass `include_replies` to the feed path — in feeds, all content IS the content.
- ❌ Do **not** place the hook before the `raw_html` type normalisation guard.
- ❌ Do **not** use `re` on the main thread for detection — wrap in `asyncio.to_thread()`.
- ❌ Do **not** call `_basic_extract()` twice (once for detection, once for extraction) on the feed path — detection uses its own lightweight parse.

---

## 10. Reference lines in current codebase (`smart_fetch_url.py` v0.4.6)

| Component | Lines |
|---|---|
| `_extract_content()` body (to replace) | 643-718 |
| `_basic_extract()` (unchanged) | 720-810 |
| `_try_alternate_fallback()` (unchanged) | 814-885 |
| `_extract_title()` | 1185-1189 |
| `_extract_meta()` | 1191-1201 |
| `_extract_language()` | 1203-1207 |
| `_strip_html()` | 1209-1212 |
| Word count gate in `smart_fetch_url()` | ~333 |

---

## 11. Advantages

| Aspect | Improvement |
|--------|-------------|
| **Accuracy** | Feeds get full content; articles keep clean Trafilatura extraction |
| **Speed** | Selectolax is faster than Trafilatura for large feeds |
| **No regressions** | Articles still use Trafilatura — same quality |
| **No new dependencies** | Selectolax already listed in requirements |
| **Lightweight detection** | ~5ms parse vs ~200-500ms Trafilatura extraction |

## 12. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **False positive**: Article with many comments classified as feed | S7/S9/S10 (`og:type`, Schema, single `<article>`) override feed signals. `"unknown"` catch-all prevents regression. |
| **False negative**: Feed without standard CSS classes | Falls to `"unknown"` → same Trafilatura behavior as today. No regression. |
| **Double parse** (detection + extraction) | Detection is ~5ms vs Trafilatura ~200-500ms. Acceptable overhead. |
| **Heuristic maintenance** | Logic isolated in `_detect_content_type()` for easy future tuning. |
