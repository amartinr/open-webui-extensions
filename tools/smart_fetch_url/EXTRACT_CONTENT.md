# Proposal: Content-Type Detection Heuristic for `smart_fetch_url`

## 1. Problem

`smart_fetch_url` currently uses **Trafilatura** as its primary HTML extractor. Trafilatura is designed to extract the **main content of single articles**, not feeds, forums, listings, or multi-entry front pages.

**Concrete example:** Fetching `http://redlib:8080/` (Reddit frontend via Redlib):
- **Trafilatura** returns only the first post (~259 words), ignoring the other ~20+ posts.
- **Selectolax** (the basic fallback) would return text from **all posts**, but it never activates because Trafilatura does return content (word_count >= 30).
- **`format="raw"`** returns the full HTML with all posts (50,774 bytes) — no extraction at all.

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

## 3. Proposed Flow

```
Fetch → raw_html
  │
  ├─ format == "raw"  →  Return raw HTML
  │
  └─ format != "raw"  →  _detect_content_type(raw_html)
                            │
                            ├─ Is feed/forum/listing?  →  Use Selectolax (full body extraction)
                            │
                            └─ Is article/single page?  →  Use Trafilatura (precise extraction)
                                                             │
                                                             ├─ word_count < 30  →  _try_alternate_fallback()
                                                             │
                                                             └─ word_count >= 30  →  Result
```

## 4. Detection Heuristic (`_detect_content_type`)

A new method that quickly scans the HTML with **Selectolax** (already a dependency) to classify the page before choosing the extractor.

### 4.1 Feed/Forum/Listing Signals

Design notes for the heuristic function (returning `"article"`, `"feed"`, or `"unknown"`):

**Feed signals (positive indicators for feed/forum/listing):**

1. **Multiple post-like elements** — Count elements matching CSS selectors like `.post`, `article`, `.entry`, `.item`, `.thread`, `.topic`. If ≥3, add weight.
2. **Explicit feed container** — Presence of IDs/classes like `#posts`, `#feed`, `.feed`, `.listing`, `#threads`.
3. **Post separators** — Multiple `<hr>` elements with classes like `.sep`, `.divider`, or `.separator`.
4. **Pagination links** — Presence of `rel="next"`, `rel="prev"`, `.pagination`, `.next`, or `a[accesskey='N']`.
5. **Pagination URL parameters** — Occurrences of `?after=`, `?page=`, `?offset=` in the raw HTML text.
6. **Repeated ID patterns** — Extract all `id="..."` values, strip digits, and check if the same base pattern appears ≥3 times.

**Article signals (positive indicators for single articles):**

7. **Open Graph `og:type="article"`** — Check for `property="og:type" content="article"` or similar patterns, plus any `property="article:"` meta tags.
8. **Schema.org `Article`** — Presence of `itemtype="http://schema.org/Article"` or `https://schema.org/Article"`.
9. **Single `<article>` tag** — Exactly one `<article>` element and ≤1 post-like element.
10. **Estimated reading time** — Meta tags like `twitter:data1` or `article:read_time`.

**Decision logic:**
- If `feed_score >= 4` and exceeds `article_score` → `"feed"`
- If `article_score >= 3` and ≥ `feed_score` → `"article"`
- Otherwise → `"unknown"` (defer to default Trafilatura behavior)

### 4.2 Integration into `_extract_content`

Modify the existing `_extract_content` method as follows:

- **New step at the top:** Call `self._detect_content_type(raw_html)` to classify the page.
- **If `"feed"`:** Log the detection, then call `self._basic_extract()` (which uses Selectolax) directly. Extract basic metadata (title via regex, author via regex, site via `urlparse`, language via regex). Skip Trafilatura entirely for this path.
- **If `"article"` or `"unknown"`:** Fall through to the existing Trafilatura extraction logic unchanged. The existing Trafilatura → fallback → strip chain is preserved.
- The alternate fallback (`_try_alternate_fallback`) remains gated by `word_count < 30` and only applies to the article/unknown path.

## 5. Advantages

| Aspect | Improvement |
|--------|-------------|
| **Accuracy** | Feeds get full content; articles keep clean Trafilatura extraction |
| **Speed** | Selectolax is faster than Trafilatura for large feeds |
| **No regressions** | Articles still use Trafilatura — same quality |
| **No new dependencies** | Selectolax already listed in requirements |
| **Efficient** | Detection only parses, doesn't extract full text |

## 6. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **False positives**: Article with many comments detected as feed | Comments are typically inside nested containers (`.comments`), not as top-level `.post` elements. Tune weights if needed. |
| **False negatives**: Feed without standard CSS classes detected as article | The `"unknown"` case delegates to Trafilatura — same behavior as today. No regression. |
| **Double parse** (Selectolax for detection + Trafilatura/Selectolax for extraction) | Detection is lightweight (~5ms) vs Trafilatura (~200-500ms). Minimal impact. |
| **Heuristic maintenance** | Logic isolated in `_detect_content_type()` for easy future tuning. |

## 7. Suggested Test Cases

| URL | Expected Type | Key Signals |
|-----|---------------|-------------|
| `http://redlib:8080/` | `feed` | Multiple `.post`, `#posts`, `after=` pagination |
| `https://old.reddit.com/r/all` | `feed` | Multiple `.entry`, `.thing`, pagination |
| `https://news.ycombinator.com/` | `feed` | Multiple `.athing` / `tr.athing` |
| Blog article with Open Graph | `article` | `og:type="article"`, single `<article>` |
| Documentation page | `unknown` → Trafilatura | No clear feed or article signals |

## 8. Implementation Order

1. Create `_detect_content_type()` as a standalone method on the `Tools` class.
2. Modify `_extract_content()` to call detection before extraction.
3. On `"feed"`, route directly to `_basic_extract()`.
4. Add debug logging: `logger.info(f"Content type detected: {type} for {url}")`.
5. Test with the suggested test cases.
6. Tune heuristic weights based on results.

## 9. References

- [2] `smart_fetch_url.py` — current extraction logic (Trafilatura primary, Selectolax/regex fallback)
- [5] Trafilatura docs: *"geared towards article pages, blog posts, main text parts. Results vary wildly on link lists, galleries or catalogs."*
