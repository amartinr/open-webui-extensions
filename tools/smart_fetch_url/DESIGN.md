# Smart Fetch URL — Design

Design document for `tools/smart_fetch_url`, an Open WebUI tool that fetches
URLs with browser-grade TLS fingerprinting and returns clean content for the
LLM. Companion to `README.md` (usage) and `CONTENT_DETECTION.md`
(content-type routing).

---

## 1. What the tool does

A single async entry point, `Tools.smart_fetch_url()`, fetches one or more
URLs and converts each response into model-friendly output.

- **Input:** a list of `http(s)` URLs (1 = full pipeline, >1 = batch with
  bounded concurrency; capped at `MAX_BATCH_LENGTH = 10`), plus output
  `format` and optional `max_chars`.
- **Output:** extracted content with a metadata header (single URL) or
  labeled results separated by `---` lines (batch). `format` ∈
  `skimmd | markdown | html | txt | json | raw`.
- **Key capability:** TLS fingerprinting via `curl_cffi` impersonating real
  browser profiles (`firefox | chrome | edge | safari`) to avoid bot
  detection; `httpx` as a no-fingerprint fallback.

The tool is *stateless per call*: all configuration comes from method args →
`UserValves` (chat) → admin `Valves` (server), resolved in that precedence
order.

### Configuration (Valves)

| Valve | Default | Purpose |
|---|---|---|
| `default_browser` | `firefox` | TLS fingerprint profile |
| `proxy` | `None` | HTTP/SOCKS proxy for all fetches |
| `timeout_ms` | `15000` | Per-request network timeout |
| `max_chars` | `16384` | Per-result output cap |
| `max_batch_chars` | `65535` | Total batch output cap |
| `batch_concurrency` | `8` | Concurrent fetches in a batch |
| `requests_per_second` | `10` | Batch rate limit |
| `blocked_domains` | `""` | Domain blocklist (admin + user, additive) |

`UserValves` mirror a subset (`default_browser`, `timeout_ms`, `max_chars`,
`batch_concurrency`, `blocked_domains`, `verbose`) for per-user overrides.

---

## 2. Pipeline

```
smart_fetch_url(urls, ...)
  ├─ validate / resolve valves / blocklist check
  ├─ single URL  → _execute_fetch(url)          [GLOBAL_OPERATION_TIMEOUT_SEC=30]
  │      _fetch_with_fingerprint(url)           [timeout_ms]
  │         ├─ _fetch_with_curl_cffi            (AsyncSession, TLS fingerprint)
  │         └─ _fetch_with_httpx                (fallback, async with)
  │      _execute_fetch routes by Content-Type:
  │         ├─ extractable document (PDF/DOCX) → _extract_document_content
  │         ├─ true binary (image/video/…)     → metadata only
  │         └─ text/HTML/JSON                  → _detect_content_type:
  │                ├─ feed/listing → _basic_extract (skimmd, whitelist HTML→MD)
  │                ├─ article     → trafilatura extraction
  │                └─ unknown     → trafilatura fallback
  │      thin-content fallback → _try_alternate_fallback (<link rel=alternate>)
  │      _format_output / _build_raw_response  (+ metadata header)
  └─ batch (n>1)  → semaphore(concurrency) + _RateLimiter(requests_per_second)
                    → asyncio.gather of per-URL fetch_one() → truncation note
```

Every CPU-bound stage (content-type detection, trafilatura, selectolax,
PDF/DOCX extraction) runs through `_run_in_thread()` so the event loop is
never blocked.

---

## 3. Pool management (the part that matters)

The tool deliberately reuses long-lived resources across calls. This section
is the lifecycle contract: what is pooled, how it is keyed, how it is bounded,
and how it is released.

### 3.1 Resources

| Resource | State | Created | Released |
|---|---|---|---|
| `ThreadPoolExecutor` (`_thread_pool`) | lazy, `__init__` sets `None` | first `_get_thread_pool()` call | `_close()` / `_aclose()` / finalizer / `__del__` |
| `asyncio.Semaphore(4)` (`_thread_semaphore`) | lazy | first `_run_in_thread()` | GC'd with the instance |
| curl `AsyncSession` cache (`_curl_sessions`) | per-`(browser, proxy)` | first fetch with that key | `_aclose()` / LRU eviction |
| httpx `AsyncClient` (fallback) | per call | each `_fetch_with_httpx()` | `async with` — always closed |
| batch `Semaphore` + `_RateLimiter` | per call | each batch | GC'd |

### 3.2 Thread pool

- **Size:** `THREAD_POOL_WORKERS = max(4, CPU_COUNT * 2)` — bounded, cannot
  grow past the CPU-derived cap.
- **Concurrency cap:** every `_run_in_thread()` call takes the instance-level
  `asyncio.Semaphore(4)`, so at most 4 CPU-bound extractions overlap per
  instance regardless of batch size.
- **Observability:** `_pool_pending_ops` counter + `_pool_stats()` feed the
  cancellation/timeout log lines with live pool state.
- **Ownership:** the executor is created lazily and owned by the instance.
  Workers hold only a *weakref* to the executor (CPython ≥3.9), so an idle
  pool with no owner is reclaimed by GC on its own.

### 3.3 curl sessions

- **Keying:** `_curl_sessions[f"{browser}::{proxy or 'direct'}"]`. The proxy
  is part of the key, so changing the `proxy` valve yields a fresh session
  with the correct proxy instead of silently reusing a stale one.
- **Bounding:** `MAX_CACHED_SESSIONS = 8` (2 per browser × 4 browsers is the
  realistic max). On a cache miss when full, the **least-recently-used**
  session is evicted and **closed** (dict insertion order + pop/reinsert
  refresh on hit).
- **Why explicit closing:** `curl_cffi.AsyncSession` (0.15.0) has **no
  `__del__`** (nor does `AsyncCurl`; only `Curl`/`CurlMime` do). An open
  session keeps its keep-alive connection pool, cookies and `curl_multi`
  handle alive until `close()` is called explicitly. The old claim that
  sessions are "GC'd with the instance" was false.

### 3.4 Cleanup paths (why no atexit)

- `weakref.finalize(self, _close_sync, weakref.ref(self))` is registered in
  `__init__`. It holds only a weakref to the instance, so instances are
  **collectable by GC** — the previous `atexit.register(self._close)` pinned
  every instantiated `Tools` for the process lifetime and defeated `__del__`.
  The finalizer shuts down the lazily-created pool at fire time (never
  captures the pool in `__init__`, since it is created after).
- `_close()` — **sync, pool-only**, idempotent. Cannot close sessions because
  `AsyncSession.close()` is async.
- `_aclose()` — **async, full teardown**: closes every cached session
  (never raises), empties the cache, then `_close()`. This is the primitive
  for hosts that manage the instance themselves (tests, scripts, previews).
- `__del__` — defensive fallback, pool-only (same constraint as `_close`).
- **Harness reality:** Open WebUI caches one instance per tool content
  version in `request.app.state.TOOLS` and re-creates it on content change
  (tools.py L303–307). There is no async tool-teardown hook, so in production
  the cache keeps ≤4 sessions + 1 pool alive for the process lifetime —
  **bounded, accepted**. The finalizer matters for no-cache harnesses and
  tool edits, where old instances would otherwise pile up.

### 3.5 Timeouts and zombie threads (known limitation)

`concurrent.futures` cannot kill a running thread. When `_run_in_thread`
times out (`THREAD_TIMEOUT_SEC = 5`) or is cancelled, the caller abandons the
future but the worker **runs to completion**, occupying a pool slot until it
finishes. The batch path additionally wraps each URL in
`GLOBAL_OPERATION_TIMEOUT_SEC = 30`.

- This is a *transient degradation under load*, not a leak: the pool is
  bounded, the semaphore caps overlap, and the slot frees when the work ends.
- On small (2-vCPU) servers the GIL serializes CPU-bound extraction, so
  timeouts become likelier and zombie load compounds. Mitigation is
  **tuning, not code**: `batch_concurrency` 2–3, `timeout_ms` 25–30 s,
  `requests_per_second` 5, and raising `THREAD_TIMEOUT_SEC` to 10–15 s
  (counter-intuitively, *longer* timeouts reduce zombies because fewer tasks
  are abandoned). See README "Tuning for small (2-vCPU) deployments".

### 3.6 Early release of large payloads

`_execute_fetch` explicitly `del`s `result`, `resp_headers`, `raw_bytes` and
`raw_html` immediately after their last use (PDFs can be 50 MB+), keeping peak
memory low. Under CPython reference counting these are immediate frees.

---

## 4. Error model

- Per-URL failures never abort the batch: `fetch_one()` catches and formats
  each error (`forbidden | cancelled | timeout | generic`) into the result
  block, and `asyncio.gather` collects all outcomes.
- A user cancellation of the whole batch cancels pending tasks; already-
  running thread-pool work continues to completion (see §3.5) and the tool
  returns a "Batch cancelled" note.
- Errors are returned as formatted output (with `status_code=0` and an
  `error` field), not raised, so the LLM sees a uniform shape.

---

## 5. Testing

Tracked suite in `test/` (39 tests, `pytest test/`):

- `test_detect_content_type.py`, `test_extract_content.py` — content-type
  routing and extraction behavior (21 tests).
- `test_real_urls.py` — real-network validation (4 tests, skippable).
- `test_resource_lifecycle.py` — the pool contract: no atexit pinning
  (subprocess probe `probe_unbounded.py`), `_aclose()` semantics, `_close()`
  pool-only, `(browser, proxy)` keying, LRU bound + eviction close.
- `test_thread_pool.py` — zombie-thread documented behavior, `_pool_pending_ops`
  restoration.
- `test_curl_sessions.py` — API guard (no `__del__`) + real session
  lifecycle (open after fetch, closed by `_aclose()`, reused across calls).

Historical analysis of the pool-management fixes (hypotheses H1–H6 and the
implementation plan) is preserved in `dist/owx-tests/{RESULTS,ACTION_PLAN}.md`.

## 6. Related documents

- **`CACHE.md`** — the on-disk fetch cache (v0.11.0): design decisions
  (freshness vs retention clocks, key derivation, sweep) and the
  implementation plan. The cache lives outside this pool contract: it is
  plain disk I/O offloaded to the default executor, deliberately not routed
  through the 4-slot `_run_in_thread` pool above.
- **`ISSUES.md`** — known bugs and open problems.
