# Issues — `smart_fetch_url`

Known bugs and open problems, tracked per component (monorepo convention).

| # | Component | Severity | Status | Opened |
|---|-----------|----------|--------|--------|
| 1 | `tools/smart_fetch_url` | Medium | Closed (v0.11.1) | 2026-09-02 |
| 2 | `tools/smart_fetch_url` | Medium | Closed (v0.11.2) | 2026-09-03 |

---

## Issue 1 — Short pages come back empty in `markdown` / `html` / `txt` formats

**Component:** `tools/smart_fetch_url` (master `v0.10.0`; also present in
`v0.11.0` on `feat/smart_fetch_url_cache`)
**Severity:** Medium — silent content loss for short pages
**Status:** Closed — fixed in v0.11.1

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

### Resolution (v0.11.1)

`_execute_fetch` now only replaces `extracted` when the fallback actually
improved on it (`word_count` of the alternate result greater than the
original's). The fallback keeps returning an empty dict for "nothing
better found"; the caller no longer lets that empty dict discard the good
extraction (content and metadata). Behaviour for pages above the threshold,
for `skimmd`/`raw`/documents, and for successful alternates is unchanged.
Regression test: `test/test_cache.py::
test_regression_short_page_markdown_keeps_content`.

### Suggested fix (historical)

Only take the fallback result when it actually improved on the original
extraction — e.g. when the returned `word_count` is greater than the
original (or greater than the threshold). Otherwise keep `extracted`. The
fallback should signal "no better content found" instead of returning an
empty dict that overwrites the original.

---

## Issue 2 — Citation sources carry ids but no content (Open WebUI)

**Component:** `tools/smart_fetch_url` (all versions up to and including
v0.11.1)
**Severity:** Medium — citations rendered as chips/markers with no readable
text; models following the default RAG template ("only cite when the
`<source>` tag includes an id attribute **and readable content**") refuse
to cite or report the context as empty
**Status:** Closed — fixed in v0.11.2

### Symptom

After a fetch, Open WebUI shows citation sources (chips) with an id but no
content. With a model that only cites sources carrying readable content,
the agent reports something like: *"the context contains `<source>` tags
with id, but no readable content inside (they are empty)"* and stops
citing the fetched pages.

### Root cause

`_emit_sources` (`smart_fetch_url.py`) emitted every source event with an
empty document:

```python
"document": [""],
```

while Open WebUI core's native `fetch_url` tool puts the fetched content in
`document` — truncated to 500 characters — via
`get_citation_source_from_tool_result` (`backend/open_webui/utils/
middleware.py`). Sources with empty documents are also emitted for URLs
whose fetch produced nothing (errors, dropped batch results), multiplying
empty chips.

Verified against open-webui `main` (v0.11.3) and the v0.11.1 tag: identical
allow-list handling of tool results and `document: [content[:500] + '...']`
for native `fetch_url`. The pipe in this monorepo (`agent_loop_guard`) does
not generate, strip, or alter citation sources — it forwards tool messages
verbatim — so it was ruled out as a contributor.

### Resolution (v0.11.2)

- `_emit_sources` now receives the fetched content per URL and puts a real
  snippet in `document` (`_source_document`, 500-char truncation mirroring
the core's native `fetch_url`).
- URLs whose content is empty or whitespace (errors, empty extractions,
  batch results dropped by truncation) emit **no** source event — a chip
  without content is the failure mode above.
- Batch fetches emit one citation per *kept* result, carrying that result's
  own content (`_batch_result_content` strips the `> key: value` metadata
  block; JSON results are kept whole).
- Regression tests: `test/test_sources.py`.

### Suggested fix (historical)

Fill `document` with the fetched content (first ~500 chars, `...` suffix)
and skip emitting sources when there is nothing readable to cite.
