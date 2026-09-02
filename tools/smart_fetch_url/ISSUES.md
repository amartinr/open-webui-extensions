# Issues — `smart_fetch_url`

Known bugs and open problems, tracked per component (monorepo convention).

| # | Component | Severity | Status | Opened |
|---|-----------|----------|--------|--------|
| 1 | `tools/smart_fetch_url` | Medium | Open | 2026-09-02 |

---

## Issue 1 — Short pages come back empty in `markdown` / `html` / `txt` formats

**Component:** `tools/smart_fetch_url` (master `v0.10.0`; also present in
`v0.11.0` on `feat/smart_fetch_url_cache`)
**Severity:** Medium — silent content loss for short pages
**Status:** Open

### Symptom

Fetching a page whose extracted content is short (fewer than
`MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK = 30` words,
`smart_fetch_url.py:65`) with `format="markdown"` (or `html` / `txt`)
returns **only the metadata header, with no content at all**.

`skimmd`, `raw` and `json` are unaffected (different code paths).

### Root cause

`_execute_fetch` (`smart_fetch_url.py:792`) unconditionally **replaces** the
good extraction with the alternate-fallback result whenever the word count
is below the threshold:

```python
if (
    format != "json"
    and extracted.get("word_count", 0)
    < MIN_EXTRACTED_WORDS_BEFORE_ALTERNATE_FALLBACK
):
    extracted, alternates_used = await self._try_alternate_fallback(...)
```

But `_try_alternate_fallback` (`smart_fetch_url.py:1390`) returns an
**empty** result `{"content": "", "word_count": 0}` when the page has no
`<link rel="alternate">` candidates (or none of them succeeds). The good
extracted content is discarded and nothing replaces it.

### Reproduction

1. Fetch any page that extracts to < 30 words with `format="markdown"`.
2. Output contains the `> Status / > URL` header but no body.

Discovered while testing the on-disk fetch cache
(`feat/smart_fetch_url_cache`) with a synthetic short page — the behaviour
is pre-existing on `master` and unrelated to the cache.

### Suggested fix

Only take the fallback result when it actually improved on the original
extraction — e.g. when the returned `word_count` is greater than the
original (or greater than the threshold). Otherwise keep `extracted`. The
fallback should signal "no better content found" instead of returning an
empty dict that overwrites the original.
