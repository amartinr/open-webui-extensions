# TODO — smart_fetch_url refactoring

Branch: `refactor/smart-fetch`
Base: review feedback on fitness for the Open WebUI harness.

---

## P0 — Bugs / resource leaks ✅

- [x] **Close `_httpx_client` properly** — `_fetch_with_httpx()` uses
  `async with httpx.AsyncClient()` per request.
- [x] **`pypdf` not needed in requirements** — transitive dependency of
  Open WebUI itself.

## P0·UX — User-facing issues in the harness ✅

Status events via `__event_emitter__` giving real-time progress feedback.
All 5 changes implemented:

### ✅ Change 1 — Helper `_emit_status()`

**Location**: `Tools._emit_status()` (next to `_emit_sources`)

Centralises the status event payload:
```python
async def _emit_status(self, emitter, description, done=False):
    if emitter is None:
        return
    try:
        await emitter({
            "type": "status",
            "data": {"description": description, "done": done},
        })
    except Exception:
        pass  # best-effort
```

### ✅ Change 2 — Status events in `smart_fetch_url()`

| # | Point | Event | `done` |
|---|-------|-------|--------|
| A | Entering `try` in `smart_fetch_url()` | `"🌐 {url}"` | `False` |
| B | Each success return (extractable doc, binary, raw, normal) | `"✅ {url}"` | `True` |
| C | `except` in `smart_fetch_url()` | `"❌ {url}"` | `True` |

**Verbose** (`Tools.Valves.verbose` / `UserValves.verbose`):
- `verbose=True` adds `({word_count}w, {elapsed:.1f}s)` to success events when extraction occurred (Option 4 — Combined)
- Early validation returns (empty URL, bad protocol) are outside the `try` and have no `__event_emitter__` — correct.

### ✅ Change 3 — Status events in `batch_fetch_urls()`

Batch manages its own events; sub-calls pass `__event_emitter__=None`.

| # | Point | Event | `done` |
|---|-------|-------|--------|
| A | Before `asyncio.gather` | `"[0/{n}] Fetching {n} URLs…"` | `False` |
| B | Inside `fetch_one()` per completed URL | `"[{i+1}/{n}] ✅ {url}"` / `❌ {url}` | `False` |
| C | After `asyncio.gather` | `"✅ Fetched {n} URLs"` | `True` |

### ✅ Change 4 — `done=True` coverage verified

Audit of all `return` paths in `smart_fetch_url()`:
- URL vacía / protocolo inválido → fuera del `try`, sin `__event_emitter__` ✅ aceptable
- Documento extraíble, binario no texto, raw format, éxito normal, excepción → todos emiten `done=True` (Change 2)

### ✅ Change 5 — Zombie threads: wrapper `_run_in_thread()`

**Location**: `Tools._get_thread_pool()`, `Tools._run_in_thread()`, `Tools.__del__()`

Replaces 7 `asyncio.to_thread()` calls with a wrapper backed by a dedicated
`ThreadPoolExecutor(4 workers)` with `cancel_futures=True` on shutdown.
Includes a 30s safety timeout. Handles `CancelledError` and `TimeoutError`
by cancelling the future before re-raising.

Callsites replaced (by method):
1. `_extract_content()` — trafilatura `_do_extract`
2. `_extract_content()` — `self._strip_html(raw_html)` fallback
3. `_detect_content_type()` — `Tools._detect_content_type_sync(raw_html)`
4. `_basic_extract()` — selectolax `_do_extract`
5. `_try_alternate_fallback()` — `_find_alternates`
6. `_extract_pdf()` — pypdf `_do_extract`
7. `_extract_docx()` — docx XML `_do_extract`

Calls with positional args (`self._strip_html`, `Tools._detect_content_type_sync`)
use `lambda` to capture arguments, since `_run_in_thread` accepts a single
callable.

**Known limitation**: `cancel_futures=True` only cancels futures not yet
started; already-running threads are not killed. Mitigated by the 30s timeout.

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

- [ ] **Graceful message for unsupported document formats**
  Currently `.xlsx`, `.pptx`, `.odt`, EPUB, RTF, legacy `.doc` show a
  message saying extraction isn't implemented.  Consider pointing the
  user to Open WebUI's knowledge-base upload as a workaround (already
  done for most, but check consistency).

- [x] **Removed the shared httpx client entirely**
  Done as part of the P0 fix.  `_fetch_with_httpx()` now uses
  `async with httpx.AsyncClient()` per request.
