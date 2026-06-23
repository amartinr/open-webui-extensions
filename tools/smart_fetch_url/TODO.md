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

## P0·UX — User-facing issues in the harness

- [ ] **UI freezes during fetch — no progress feedback**
  Open WebUI pauses token streaming while a tool runs.  The user sees a
  frozen screen for the entire fetch + extraction time.  Currently the
  tool only emits `"source"` events at the very end.

  Consequences:
  - Single slow URL (~15s timeout): user thinks the chat is broken
  - Batch of 50 URLs: 30–90s of silence, no indication of progress
  - `batch_fetch_urls` explicitly passes `__event_emitter__=None`,
    so per-item progress is suppressed entirely

  **Technical constraints (from Open WebUI docs):**

  1. **Must use `"type": "status"`** — this is the only real-time
     feedback type that works in **Native Mode** (the only supported
     mode).  Types like `"message"`, `"chat:message:delta"` and
     `"replace"` are **BROKEN** in Native Mode — they get overwritten
     by native completion snapshots.

  2. **Payload format** (works identically in Default and Native modes):
     ```python
     {
         "type": "status",
         "data": {
             "description": "Human-readable text",
             "done": False,   # False = shimmer animation
             "hidden": False,  # True = saved to history, not shown
         }
     }
     ```

  3. **Always emit a final `done: True`** — without it the shimmer
     animation stays forever, making the tool look stuck even after
     completion.

  4. **`"source"` / `"citation"` events work in both modes** — the
     tool already uses these correctly for the citations list.

  **Implementation plan** — 5 changes, in implementation order:

  ---
  ### ✅ Change 1 — Helper `_emit_status()`

  **File**: `smart_fetch_url.py`
  **Insert**: next to `_emit_sources` (~l. 1406), before or after

  Centralise the status event payload format into a single helper:

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

  **Verification**: `Tools()._emit_status(None, "test")` does not raise.
  **Risk**: None — new method, no callers.

  ---
  ### Change 2 — Status events in `smart_fetch_url()`

  **File**: `smart_fetch_url.py`
  **Insertion points**: between l. 207 and l. 367 (try body)

  **Base events** (always, regardless of verbose):

  | # | Point | Event | `done` |
  |---|-------|-------|--------|
  | A | l. 215 (entering try, after validation) | `"🌐 {url}"` | `False` |
  | B | All success returns (~l. 244, 260, 272, 362) | `"✅ {url}"` | `True` |
  | C | l. 367 (`except`) | `"❌ {url}"` | `True` |

  **Additional info gated by Valve `verbose`**:

  | Option | `verbose=False` (default) | `verbose=True` | Extra info | Use case |
  |--------|--------------------------|----------------|------------|----------|
  | **1 — Word count** | `"✅ {url}"` | `"✅ {url} ({word_count}w)"` | Number of extracted words | LLM knows how much content it received |
  | **2 — Content type** | `"✅ {url}"` | `"✅ {url} [{type}]"` | `article` / `feed` / `PDF` / `raw` | Debug routing internals |
  | **3 — Timing** | `"✅ {url}"` | `"✅ {url} ({elapsed:.1f}s)"` | Total elapsed seconds | Diagnose slowness |
  | **4 — Combined** | `"✅ {url}"` | `"✅ {url} ({word_count}w, {elapsed:.1f}s)"` | Words + time | Power users |

  **TBD** — choose option when implementing:
  `[ ] 1 — Word count` `[ ] 2 — Content type` `[ ] 3 — Timing` `[ ] 4 — Combined`

  Early validation returns (empty URL / bad protocol, l. 211-214) are outside
  the `try` block and do not emit events — correct, since the operation never
  started.

  **Risk**: Low. Each insertion is 1 line. Forgetting a `done=True` is
  visually detectable (perpetual shimmer).

  ---
  ### Change 3 — Status events in `batch_fetch_urls()`

  **File**: `smart_fetch_url.py`
  **Lines affected**: l. 417-460 (`fetch_one` + return)

  Design: batch manages its own events; sub-calls to `smart_fetch_url` do
  NOT emit events (still pass `__event_emitter__=None` as today).

  | # | Point | Event | `done` |
  |---|-------|-------|--------|
  | A | Before the gather | `"[0/{n}] Fetching {n} URLs…"` | `False` |
  | B | Per completed URL (inside `fetch_one`) | `"[{i+1}/{n}] ✅ {single_url}"` | `False` |
  | C | After the gather | `"✅ Fetched {n} URLs"` | `True` |

  **Risk**: Low. Same pattern — only the emitter changes.

  ---
  ### Change 4 — `done=True` coverage in all return paths

  **File**: `smart_fetch_url.py`

  Audit that **all** exit points emit `done=True` when `__event_emitter__`
  is available. Includes:

  | Line | Condition | Status today |
  |------|-----------|-------------|
  | l. 211 | Empty URL | ❌ bare string return |
  | l. 214 | Invalid protocol | ❌ bare string return |
  | l. 244 | Extractable document | ❌ only `_emit_sources` |
  | l. 260 | Binary non-text | ❌ only `_emit_sources` |
  | l. 272 | Raw format | ❌ only `_emit_sources` |
  | l. 362 | Normal success | ❌ only `_emit_sources` |
  | l. 367 | General exception | ❌ return error msg |

  Validation returns (l. 211, 214) are outside the `try` — they don't have
  access to `__event_emitter__`. Acceptable since the operation never started.

  For the rest, Change 2 already covers B/C/D/G/H. This is a cross-check.

  **Risk**: Very low — checklist only.

  ---
  ### Change 5 — Zombie threads: wrapper `_run_in_thread()`

  **File**: `smart_fetch_url.py`
  **Lines affected**: 7 `asyncio.to_thread` callsites

  **Problem**: `asyncio.to_thread()` does not expose the underlying
  `ThreadPoolExecutor`, so we cannot set `cancel_futures=True`. If the user
  cancels generation, the asyncio task is cancelled but the thread runs to
  completion.

  **Proposed solution**: Replace `asyncio.to_thread(func)` with a wrapper
  backed by a dedicated `ThreadPoolExecutor` with `cancel_futures=True`
  on shutdown, plus a safety timeout.

  **Code**:

  ```python
  import concurrent.futures

  class Tools:
      _thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

      def _get_thread_pool(self):
          if self._thread_pool is None:
              self._thread_pool = concurrent.futures.ThreadPoolExecutor(
                  max_workers=4, thread_name_prefix="smart_fetch"
              )
          return self._thread_pool

      async def _run_in_thread(self, func, timeout=30.0):
          loop = asyncio.get_running_loop()
          pool = self._get_thread_pool()
          fut = loop.run_in_executor(pool, func)
          try:
              return await asyncio.wait_for(fut, timeout=timeout)
          except asyncio.CancelledError:
              fut.cancel()
              raise
          except asyncio.TimeoutError:
              fut.cancel()
              raise

      def __del__(self):
          if self._thread_pool is not None:
              self._thread_pool.shutdown(wait=False, cancel_futures=True)
  ```

  **Callsites to replace**:

  | # | Current line | Current code | Replace with |
  |---|-------------|--------------|--------------|
  | 1 | l. 674 | `await asyncio.to_thread(_do_extract)` | `await self._run_in_thread(_do_extract)` |
  | 2 | l. 690 | `await asyncio.to_thread(self._strip_html, raw_html)` | `await self._run_in_thread(lambda: self._strip_html(raw_html))` |
  | 3 | l. 866 | `await asyncio.to_thread(Tools._detect_content_type_sync, raw_html)` | `await self._run_in_thread(lambda: Tools._detect_content_type_sync(raw_html))` |
  | 4 | l. 911 | `await asyncio.to_thread(_do_extract)` | `await self._run_in_thread(_do_extract)` |
  | 5 | l. 954 | `await asyncio.to_thread(_find_alternates)` | `await self._run_in_thread(_find_alternates)` |
  | 6 | l. 1179 | `return await asyncio.to_thread(_do_extract)` | `return await self._run_in_thread(_do_extract)` |
  | 7 | l. 1257 | `return await asyncio.to_thread(_do_extract)` | `return await self._run_in_thread(_do_extract)` |

  **Note**: `asyncio.to_thread` accepts positional args (`*args`). Our
  wrapper does not. For cases with arguments (l. 690, 866), use `lambda`
  to capture them.

  **Risk**: Medium. 7 callsites to touch, and the 30s timeout might be too
  short for very large PDFs (though 30s is generous). Can be parametrised
  per operation type if needed.

  **Known limitation**: `concurrent.futures.ThreadPoolExecutor` with
  `cancel_futures=True` only cancels futures **not yet started**. Once a
  thread is already running, `cancel_futures=True` does not kill it.
  That would require `threading.Event` signalling or `PEP 554` (not
  accepted). Better than nothing, but not a complete solution.

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
