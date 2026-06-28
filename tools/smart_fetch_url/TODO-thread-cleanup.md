# TODO — Thread cleanup (`fix/thread-cleanup`)

## Outstanding issues

### 1. Zombie threads on cancellation (unfixable in pure Python)

`fut.cancel()` on a `ThreadPoolExecutor` future **cannot stop a thread that is already running** — it only cancels tasks still waiting in the queue.  When the user hits Stop (or a per-fetch timeout fires), the call to `loop.run_in_executor()` is cancelled, but the underlying thread keeps running trafilatura / selectolax / pypdf until it finishes its current work.

**Impact**: up to `THREAD_TIMEOUT_SEC` (5 s) of wasted CPU per cancelled operation.

**Possible mitigations** (all require deeper architectural changes):
- Run each CPU-bound task in a **subprocess** via `concurrent.futures.ProcessPoolExecutor` — subprocesses can be killed (`SIGKILL`).  The trade-off is higher overhead per task and no shared memory.
- Set a **watchdog flag** that CPU-bound functions check periodically.  Requires modifying trafilatura/pypdf behaviour, which is impractical.
- Accept the limitation — the thread finishes and returns to the pool; no leak, just delay.

### 2. `selectolax` tree crossing thread boundaries

`_detect_content_type_sync()` returns a parsed `selectolax.parser.HTMLParser` tree created **inside a pool thread**.  That tree is then passed to `_basic_extract()`, which uses it inside **another** `_run_in_thread()` call (possibly a different OS thread).

`selectolax` is a C extension (lexbor).  Its thread-safety guarantees are undocumented.  If two threads access the same tree concurrently, undefined behaviour may occur.

**Mitigation**: Either document that this is best-effort, or deep-copy the tree.  selectolax does not expose a `copy()` method, so this would require serialising to HTML and re-parsing — defeating the optimisation purpose.

### 3. Single shared thread pool under batch pressure

All CPU-bound work (trafilatura, selectolax, pypdf, docx XML) shares the same `ThreadPoolExecutor(max_workers=8)`.  In `batch_fetch_urls()` with 50 URLs, many calls to `_run_in_thread()` queue up simultaneously.  The pool acts as a bottleneck.

**Mitigation**: The pool size was raised from 4 to 8 in this branch.  Going higher risks starving the main event loop (GIL contention).  A separate pool for I/O vs CPU work could help, but the current design already runs HTTP fetches in async (`curl_cffi` / `httpx`), so only CPU-bound extraction goes through the pool.

### 4. Repeated `import trafilatura` on every call

`_extract_content()` runs `import trafilatura` and `from trafilatura.core import extract_with_metadata` inside the hot path.  Python's import system caches modules in `sys.modules`, so the second call is a dict lookup — not a real leak, but unnecessary work.

**Fix**: Move the imports to module level or to `Tools.__init__()`.

### 5. ~~`raw_bytes` held until `_execute_fetch()` returns~~ ✅ FIXED

~~For the binary/document path, `raw_bytes` (potentially many MB for a large PDF) stays in scope until the end of `_execute_fetch()`.  Already mitigated for the text path (`raw_bytes = None`), but the document path only frees it after `_extract_document_content()` returns and the method exits.~~

**Fix**: Replaced `raw_bytes = None` with `del raw_bytes` in both the document and text paths.  `del` removes the name from the local namespace entirely so the frame no longer holds a slot for it — tracebacks from later exceptions cannot resurrect the bytes.  The document path also hoists `word_count` extraction before the delete, keeping formatting/output work fully separated from the raw body.
