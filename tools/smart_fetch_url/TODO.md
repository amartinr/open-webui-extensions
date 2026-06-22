# TODO — smart_fetch_url refactoring

Branch: `refactor/smart-fetch`
Base: review feedback on fitness for the Open WebUI harness.

---

## P0 — Bugs / resource leaks

- [x] **Close `_httpx_client` properly**
  Removed the shared cached client (`_get_httpx_client()`) and switched
  `_fetch_with_httpx()` to use `async with httpx.AsyncClient()` per
  request.  Since httpx is only a fallback path, the overhead of creating
  a client per call is negligible.

- [x] **`pypdf` not needed in requirements** — it is already a transitive
  dependency of Open WebUI itself (used for document processing/RAG),
  so it is always available at runtime.

---

## P1 — Code clarity / maintainability

- [ ] **Rename parameter `os` to `os_profile`**
  `os` shadows the built-in module (`import os` is used at the top of
  the file).  Python allows it, but it's confusing for readers and
  breaks IDE refactoring.  The public method signature changes, but
  callers typically pass it as a keyword argument, so this is a
  backward-compatible change in practice.

- [ ] **Deduplicate selectolax parse in feed path**
  When a page is classified as `"feed"`:
  1. `_detect_content_type()` parses HTML with selectolax
  2. `_basic_extract()` parses the same HTML again with selectolax
  In large forums this doubles parse time (~5ms → ~10ms).  Options:
  - Thread a pre-parsed `HTMLParser` tree through the pipeline
  - Cache the tree on `self` (careful with re-entrancy)
  - Accept the overhead (it's small, but inelegant)

- [ ] **Logging visibility in Open WebUI**
  `logger.info/warning` calls are invisible to the end user because
  Open WebUI doesn't surface tool logs.  Consider:
  - Emitting `__event_emitter__({"type": "status", …})` for milestones
    (fetching, parsing, extracting, done)
  - Or removing noisy internal logs that nobody sees

---

## P2 — Production hardening

- [ ] **Rate limiting for batch fetches**
  `batch_fetch_urls()` respects concurrency via `asyncio.Semaphore`,
  but there is no global rate limiter.  50 concurrent requests from
  one chat session can trigger rate-limiting or abuse detection on
  the target servers.  Options:
  - Add a `requests_per_second` valve (default: ~10/s)
  - Use `asyncio.Semaphore` with a token-bucket or sliding-window

- [ ] **Document the `os_profile` change in README**
  When `os` → `os_profile`, update the docstring and the README so
  existing users know.

- [ ] **Add a version bump in the docstring header**
  Current: `version: 0.5.0`
  After all fixes: `version: 0.6.0`

---

## P3 — Nice-to-have

- [ ] **Emit progress events during fetch**
  Use `__event_emitter__` with `{"type": "status", "data": …}` so
  the UI shows live progress (e.g. "Fetching…", "Extracting…",
  "Done") instead of a silent wait.

- [ ] **Graceful message for unsupported document formats**
  Currently `.xlsx`, `.pptx`, `.odt`, EPUB, RTF, legacy `.doc` show a
  message saying extraction isn't implemented.  Consider pointing the
  user to Open WebUI's knowledge-base upload as a workaround (already
  done for most, but check consistency).

- [x] **Removed the shared httpx client entirely**
  Done as part of the P0 fix.  `_fetch_with_httpx()` now uses
  `async with httpx.AsyncClient()` per request.
