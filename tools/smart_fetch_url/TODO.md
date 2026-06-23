# TODO — smart_fetch_url refactoring

Branch: `refactor/smart-fetch`
Base: review feedback on fitness for the Open WebUI harness.

---

## P0 — Bugs / resource leaks ✅

- [x] **Close `_httpx_client` properly** — `_fetch_with_httpx()` uses
  `async with httpx.AsyncClient()` per request.
- [x] **`pypdf` not needed in requirements** — transitive dependency of
  Open WebUI itself.
- [x] **Pass proxy correctly to curl_cffi and httpx**
  - curl_cffi: `AsyncSession(proxies=)` expects a **dict** `{"http": ..., "https": ...}`,
    not a plain string. Fixed by converting at callsite.
  - httpx: `AsyncClient.get()` does not accept `proxies=` — it must be passed as
    `proxy=` (singular) to the `AsyncClient()` constructor. Fixed accordingly.

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
- Empty URL / invalid protocol → outside `try`, no `__event_emitter__` ✅ acceptable
- Extractable doc, binary non-text, raw format, normal success, exception → all emit `done=True` (Change 2)

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

### ✅ Decided: fallback from curl_cffi to httpx

`_fetch_with_fingerprint()` now only falls back to httpx on `ImportError`.
Any other error from curl_cffi propagates directly to the caller, making
real curl_cffi errors visible instead of being masked by the httpx attempt.

When the fallback triggers:
- A warning is written to the log and to stderr
- A `fallback_note` is set and propagated through `_format_output()` so the
  LLM agent sees a `> ⚠️ Fetched via httpx fallback (...)` line in the result

---

## P1 — Code clarity / maintainability ✅

- [x] **Rename parameter `os` to `os_profile`**
  `os` shadows the built-in module (`import os` is used at the top of
  the file).  Renamed in all method signatures, callsites, docstrings,
  format strings, and README.  JSON output key kept as `"os"` for
  backward compatibility.

- [x] **Deduplicate selectolax parse in feed path**
  When a page is classified as `"feed"`:
  1. `_detect_content_type()` parses HTML with selectolax
  2. `_basic_extract()` parses the same HTML again with selectolax

  **Fix**: `_detect_content_type_sync()` now returns `(category, tree)`
  where `tree` is the parsed `HTMLParser`.  `_basic_extract()` accepts
  an optional `tree` parameter — when provided, it skips re-parsing.
  `smart_fetch_url()` passes the tree from detection to extraction in
  the feed branch.

---

## P2 — Production hardening ✅

- [x] **CancelledError handling in async fetch methods**
  - `_fetch_with_curl_cffi()`: `await session.get()` wrapped in
    `try/except asyncio.CancelledError` — logs and re-raises.
  - `_fetch_with_httpx()`: same pattern around `await client.get()`.
  - `smart_fetch_url()`: `except asyncio.CancelledError` added before
    `except Exception` — emits `❌` status and re-raises.
  - **Defensive timeout**: `_fetch_with_fingerprint()` wrapped with
    `asyncio.wait_for(timeout=max(timeout_ms/1000*2, 30))` to catch
    hangs when Stop does not propagate.  Uses `GLOBAL_DEFENSE_TIMEOUT_SEC`
    constant (30s) to avoid magic numbers.

- [x] **Rate limiting for batch fetches**
  Added `_RateLimiter` helper class (sliding-window with `asyncio.Lock`).
  New admin Valve `requests_per_second` (default 10).  `batch_fetch_urls()`
  calls `rate_limiter.acquire()` before each request, inside the semaphore.

---

## Additional changes (not in original TODO)

- [x] **Remove User-Agent override, let curl_cffi handle it**
  `DEFAULT_USER_AGENTS` dict and the block that injected it into
  `request_headers` were removed.  curl_cffi already sets the correct
  User-Agent matching each impersonate profile (e.g. `firefox_147` →
  `Macintosh; Intel Mac OS X 10.15`).  Our override was incoherent
  with the TLS fingerprint.  Users can still pass a custom UA via
  `headers={"User-Agent": "..."}`.

- [x] **Change default browser/OS to firefox_147/linux**
  `DEFAULT_BROWSER` changed from `chrome_145` to `firefox_147`.
  Default `os_profile` changed from `"windows"` to `"linux"`.
  Firefox + Linux worked where Chrome + Windows was blocked (EL PAÍS).

---

## P3 — Nice-to-have

- [x] **Graceful message for unsupported document formats**
  Currently `.xlsx`, `.pptx`, `.odt`, EPUB, RTF, legacy `.doc` show a
  message saying extraction isn't implemented.  Consider pointing the
  user to Open WebUI's knowledge-base upload as a workaround (already
  done for most, but check consistency).

- [x] **Removed the shared httpx client entirely**
  Done as part of the P0 fix.  `_fetch_with_httpx()` now uses
  `async with httpx.AsyncClient()` per request.
