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

  **Implementation plan:**

  | Milestone | Description | done |
  |---|---|:---:|
  | Resolving | `"🔍 Resolving {url}"` | `False` |
  | Fetching  | `"🌐 Fetching {url}…"` | `False` |
  | Extracting | `"📄 Extracting content…"` | `False` |
  | Done      | `"✅ Done — {word_count} words"` | `True` |

  For batch mode, emit per-item progress:
  ```python
  await __event_emitter__(
      "type": "status",
      "data": {"description": f"[{i+1}/{n}] ✅ {url}", "done": False},
  )
  # … and a final one when all URLs complete:
  await __event_emitter__(
      "type": "status",
      "data": {"description": f"✅ Fetched {n} URLs", "done": True},
  )
  ```

  **Key change for batch**: stop passing `__event_emitter__=None` to
  individual `smart_fetch_url` calls.  Instead, pass it through so
  each sub-fetch emits its own status events.

- [ ] **`asyncio.to_thread()` leaves zombie threads on cancellation**
  Every CPU-bound operation (trafilatura, selectolax, pypdf, …) runs
  via `asyncio.to_thread()`.  If the user cancels generation, the asyncio
  task is cancelled but the thread keeps running to completion.

  Consequences:
  - CPU/memory wasted on work nobody needs
  - Thread-pool slots occupied, potentially starving legitimate requests
  - Worst case: rapid cancel → re‑fetch cycle causes thread pile-up

  Solutions:
  - Wrap `to_thread` calls with a shield or timeout
  - Use a custom thread pool with `cancel_futures=True` on `shutdown()`
    (but `to_thread` doesn't expose the underlying `ThreadPoolExecutor`)
  - Detect cancellation via `asyncio.current_task().cancelling()` and
    skip heavy work when possible

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
