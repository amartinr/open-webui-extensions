# On-disk fetch cache for `smart_fetch_url` — design & implementation plan

> **Status:** Approved design — implementation plan follows (Section 9)
> **Scope:** `tools/smart_fetch_url` (Open WebUI Workspace Tool)
> **Branch:** `feat/smart_fetch_url_cache`
> **Verified against:** Open WebUI `main` @ `2a960a59` (2026-08-31);
> deployed instance build `d3e8bf3405e848cfba377814d0aa7ba7290e414d`
> (container `open-webui`, verified via `docker inspect`)
>
> **Provenance:** the dual-clock pattern (freshness vs retention) follows the
> earlier cache design draft, itself a port of the search-cache pattern from
> `amartinr/pi-searxng`. This document consolidates that draft with the
> corrections agreed during review (§13).

---

## 1. Purpose

`smart_fetch_url` fetches URLs with TLS fingerprinting (curl_cffi) and
returns clean content in several formats (`skimmd | markdown | html | txt |
json | raw`). The network fetch is by far the most expensive operation — up
to `timeout_ms` (default 15 s) per URL.

**Observed agent behaviour:** when the agent fetches a URL, it sometimes
requests the *same* URL again shortly after, for two reasons:

1. **Content truncated by length** — the result was cut off by `max_chars`
   and the agent re-requests the same URL with a larger limit.
2. **Inadequate format** — the agent sees the returned format does not suit
   it and requests another.

**Objective:** do not re-fetch the same resource for those immediate retries,
and by the same token stop hammering the URL / upstream server. A retry
(reformat or re-truncate) should be served from already-fetched content in
well under a second, without a second network request.

This is an independent tool improvement; it does not depend on any gateway
choice (Bifrost/LiteLLM).

## 2. Deployment ground truth (evidence, not assumptions)

Decisions below rest on facts verified from the Open WebUI source
(`main` @ `2a960a59`) and from the live instance (`docker inspect open-webui`):

| Fact | Evidence |
|---|---|
| Tools are **cached per process**, not re-instantiated per call | `load_tool_module_by_id` execs the content once and returns `module.Tools()` (`backend/open_webui/utils/plugin.py:206`); `get_tools` reuses the module from `request.app.state.TOOLS` and reloads only when the DB content changed (`backend/open_webui/utils/tools.py:303–308`) |
| The tool instance **survives across requests** (same worker) | All invocations in a chat request hit the same `tool['callable']` bound to that instance (`utils/middleware.py:3150+`; `get_tools` at `utils/middleware.py:2916`) |
| Deployment is **single worker** | `Cmd: [bash start.sh]`, no `UVICORN_WORKERS` in env → `start.sh` default `--workers 1` |
| Host is **RAM-constrained**; models run in-process | `USE_EMBEDDING_MODEL_DOCKER=sentence-transformers/all-MiniLM-L6-v2`, `AUXILIARY_EMBEDDING_MODEL=TaylorAI/bge-micro-v2`, `WHISPER_MODEL=base`, `SENTENCE_TRANSFORMERS_HOME` inside the data dir |
| `DATA_DIR` is **real disk, persistent** | Mount `openwebui_data` (named volume, driver `local`) → `/app/backend/data`; not tmpfs |
| The per-tool cache directory **already exists** | `CACHE_DIR = DATA_DIR/'cache'` (`config.py:181`); `CACHE_DIR/'tools'/<tool_id>` is created on tool save (`routers/tools.py:402–403`) |
| The tool can learn its own `tool_id` | Open WebUI injects `__id__` into the callable **if declared in the signature** (`utils/tools.py:220`, `:343`) |

### Consequences for the design

- An **in-memory** cache (instance or module dict) would work in this single
  worker, but it **adds sustained RAM** (up to `max_entries` full `raw_html`
  bodies resident) on a host that is already short on memory. → **Disk.**
- Because the instance persists across requests, even a modest disk cache
  covers the retry scenarios of §1 (they are cross-request by nature: the
  agent decides to retry only after seeing the first result).
- The canonical location is `<DATA_DIR>/cache/tools/<tool_id>/` — real disk
  (no RAM), persistent across container recreation and OOM restarts, and
  consistent with where Open WebUI already keeps model caches
  (`cache/embedding/models`, `cache/whisper/models`).
- Single worker ⇒ no cross-worker cache consistency concerns. Multi-worker
  would still be safe (atomic writes), but it is not the deployment being
  designed for.

## 3. Non-goals

- **Not** a permanent content cache serving stale data — freshness is bounded.
- **Not** a replacement for gateway-level *semantic caching*.
- **Not** cross-request coordination: no single-flight, no per-key locking,
  no deduplication of concurrent fetches. Two simultaneous requests for the
  same URL may both fetch — acceptable.
- **Not** a multi-user-isolated cache: no user/session in the key; only
  public web content is stored.
- **Only plain-text content is cached.** Binary (PDF, DOCX, images, video,
  fonts) is never written to disk. No `raw_bytes` sidecar in this phase.
- **Errors are never cached.** Non-2xx, timeouts, exceptions and empty bodies
  always go through the network path.

## 4. Design decisions

### D1. Cache at the raw-HTML level, not per format

The dominant cost is the network fetch; extraction (trafilatura/selectolax in
a thread) is cheap by comparison and runs per call anyway. The cache stores
the fetched **`raw_html`**; every format, the metadata and the `max_chars`
truncation are regenerated from it on each request. This covers both agent
scenarios: a different format re-extracts the same `raw_html`, and a larger
`max_chars` re-truncates the *full* content (the stored body was never cut).

### D2. On disk, in Open WebUI's per-tool cache directory

Cache root resolution, evaluated lazily on first cache use (never at import
time, so standalone execution keeps working):

1. `SMART_FETCH_CACHE_DIR` env var — explicit override (tests, unusual
   deployments).
2. `from open_webui.config import CACHE_DIR` when importable (in-process
   execution) → `CACHE_DIR / 'tools' / <tool_id> / 'fetch_cache'`.
3. Fallback (standalone/tests): `<tempdir>/smart_fetch_url_cache/<tool_id>`.

`<tool_id>` comes from the `__id__` kwarg that Open WebUI injects when the
method declares it (see §7). `fetch_cache` is a dedicated subdirectory so we
never collide with anything else Open WebUI may put in the per-tool dir.

In the current deployment this resolves to
`/app/backend/data/cache/tools/<tool_id>/fetch_cache` — real disk, RAM-free,
persistent.

### D3. Two clocks: freshness (refetch) vs retention (delete)

A cache entry has two independent clocks:

- **Freshness** — how old the *content* may be before a refetch. Default
  **300 s**, based on `createdAt` (epoch seconds stored in the payload at
  fetch time; admin valve `cache_freshness_seconds`). Enabling/disabling is
  the job of the `cache_enabled` valves (§6), not of the freshness value.
  On a stale entry the request refetches, replaces the content and resets
  `createdAt`.
- **Retention** — how long an entry may stay on disk *since last use*.
  Default **3600 s (1 h)**, measured from `lastAccessed`. An entry still being used is
  never deleted, even when its content is stale (it is simply refetched on
  next use). Enforced by the periodic sweep (§D8), not at read time.

Separating the two is what keeps a heavily-used entry alive past its
freshness window: staleness and deletion are independent decisions.

**Implementation note (deviation from the draft):** `lastAccessed` is the
**file mtime**, refreshed on hits with `os.utime()` — which updates mtime
*without rewriting the file*. (The draft claimed mtime "cannot be updated
without rewriting"; that is incorrect.) Benefits:

- A hit costs one payload read (+ a rare `os.utime`, throttled to once per
  entry per 60 s) instead of a full JSON rewrite per hit.
- The sweep becomes **stat-only**: it never reads payloads to learn
  `lastAccessed`, so a directory of 100 × (up to MiB) entries is swept by
  statting names, not reading megabytes.
- After an atomic refetch-write, mtime and `createdAt` are both ≈ now, which
  is the correct semantics anyway.

`createdAt` stays inside the JSON payload (it must survive rewrites; mtime
cannot carry it).

### D4. Cache key: what shapes the upstream request

Key material is a compound string, hashed with SHA-256 for the filename
(hash avoids filesystem length/character limits and keeps URLs with query
tokens out of file names):

```
accept_group \n browser \n normalized_url
```

- `accept_group` — derived from `format`: `json` (uses `DEFAULT_JSON_ACCEPT`),
  `raw` (`DEFAULT_RAW_ACCEPT`), `html` (everything else — `skimmd`,
  `markdown`, `html`, `txt` all share `DEFAULT_ACCEPT`, so they share cache
  entries; see `smart_fetch_url.py:861–868`).
- `browser` — the TLS fingerprint profile changes the response of some
  anti-bot sites. Kept in the key: it also exists as a UserValve, so it can
  vary per request.
- `normalized_url` — lowercase scheme and host, default port dropped,
  fragment removed, **query preserved as-is**, userinfo preserved if present.

**Deliberately excluded — the proxy.** It is an admin-only valve, effectively
constant per deployment (every request in this instance goes through the same
proxy), so it does not fragment the cache in practice; a rare runtime change
of the valve is assumed not to change the content within the freshness
window, so a proxy switch does not invalidate entries. (This mirrors the
"cost of a constant in the key is zero" argument in reverse: excluding it
only costs correctness in a case that does not occur here.)

**Rule:** whatever affects the upstream *request* goes in the key; whatever
affects only formatting/truncation *after* the fetch does not. Therefore
`format` (except for its Accept group), `max_chars` and `include_replies`
are **not** in the key — they act on the cached `raw_html`, after the fetch.
(Note: `include_replies` changes trafilatura's `include_comments`, which is a
post-fetch extraction parameter — correct to exclude.) The fetcher identity
(curl_cffi vs the httpx fallback) is also excluded: it is constant per
deployment (curl_cffi is always installed in production, so the fallback
never fires there), so its content never mixes within one cache.

### D5. Entry payload

Each file is one JSON document:

```json
{
  "createdAt": 1750000000,
  "raw_html": "<full text content...>",
  "final_url": "https://...",
  "status_code": 200,
  "content_type": "text/html; charset=utf-8",
  "resp_headers": {}
}
```

- `lastAccessed` is **not** stored — it is the file mtime (D3).
- `raw_bytes` is never stored (text-only scope, D6).

### D6. What may be cached (the write rule)

An entry is written only when **all** hold:

1. HTTP status `200–299` (curl_cffi does not raise on 4xx/5xx, so this must
   be checked explicitly against `FetchResult.status_code`),
2. the body was decoded as text — i.e. `raw_html` is non-empty (the fetchers
   already only populate `raw_html` for text-like Content-Types: `text/*`
   plus `_TEXT_LIKE_APPLICATION_TYPES`; `smart_fetch_url.py:963, 1014`),
3. `len(raw_html) <= CACHE_MAX_RAW_HTML_BYTES` (constant, **2 MiB**) — caps
   both disk footprint and the RAM materialised per hit.

**Why text-only is load-bearing:** a cache hit reconstructs a `FetchResult`
with `raw_bytes=None`. If binary were ever cached, `_execute_fetch`'s
document path would receive `raw_bytes=None` and report "no response body
available". Because binary never hits the cache, every hit is text and the
`raw_bytes=None` reconstruction is always safe.

### D7. Non-blocking, best-effort I/O

- All file operations (read/write/stat/delete/utime, key hashing if it ever
  shows up in profiling) run through `asyncio.to_thread` — the **default
  executor**, never the tool's 4-slot `_run_in_thread` pool (that pool is
  reserved for CPU-bound extraction, carries the 5 s
  `THREAD_TIMEOUT_SEC`/zombie semantics, and mixing cache I/O into it could
  starve extraction).
- **Writes are fire-and-forget:** after a miss the result is returned to the
  caller immediately; persistence runs as a background task. Pending write
  tasks are kept in a module-level set so GC does not reap them mid-write;
  the set drops them on completion. A lost write at process exit is
  acceptable for a best-effort cache.
- `lastAccessed` updates are deferred: `os.utime` only when the mtime is
  older than 60 s (`CACHE_TOUCH_INTERVAL_SEC`).
- Cancellation safety mirrors the existing thread policy: if the awaiting
  caller is cancelled, an in-flight `to_thread` op completes in the
  background; the background write task is shielded from the caller's
  cancellation.
- Any read/write error is swallowed and degrades to a normal fetch (with a
  `logger.warning`); the cache never breaks a request. A corrupt JSON file is
  **deleted** on read so it is not re-parsed on every request.

### D8. Sweep: retention, LRU cap, orphan cleanup

A **module-level singleton task**, registered lazily on first cache use
(instances and modules are recreated per tool content version, so the sweep
must not be tied to an instance), runs every **300 s** (`SWEEP_INTERVAL_SEC`),
stat-only (D3):

1. delete `*.tmp` files older than 60 s (orphans of interrupted atomic
   writes; never delete a fresh tmp — a concurrent writer may be mid-write);
2. delete entries with `now - mtime > cache_retention_seconds`;
3. if the entry count still exceeds `cache_max_entries`, evict the entries
   with the oldest mtime (LRU) until under the cap.

Freshness is **not** the sweep's job: stale-but-accessed entries are kept and
refetched on demand. The sweep is idempotent, yields periodically (processes
the directory in chunks through `to_thread`, `await asyncio.sleep(0)`
between chunks), and never raises. Runs even when the cache is disabled
(`cache_enabled = False`, admin or per-user) so leftovers from a
previously-enabled configuration are eventually reaped.

Configuration for the sweep (retention, max entries) comes from a module-level
snapshot refreshed on every cache operation from the active instance's admin
valves (§8). Content edits replace the module and reset the singleton — the
old task (if still running) may overlap briefly; it is idempotent, so this is
benign.

## 5. Impact on the current flow

The cache is inserted at the single point where the network fetch happens:
`_fetch_with_fingerprint` (`smart_fetch_url.py:838`) — the only network door,
used by both `_execute_fetch` (main path) and `_try_alternate_fallback`
(alternate-content fetches get cached too, which is fine). The network body
is extracted into an internal `_fetch_raw`, and the entry point becomes:

**Disabled** (admin `cache_enabled = False`, or the user turned their toggle
off): skip the cache entirely — straight to `_fetch_raw`, no reads, no
writes; behaviour identical to today. Otherwise:

1. **Fresh hit** (`now - createdAt <= freshness`) → reconstruct
   `FetchResult(raw_html, final_url, status_code, content_type,
   resp_headers={}, raw_bytes=None)` from the payload and return; touch mtime
   if due.
2. **Stale hit** → refetch via `_fetch_raw`, then overwrite the entry
   (new content, `createdAt = now`). Retention is untouched — the entry stays.
3. **Miss** → `_fetch_raw`, return the result immediately, and if the write
   rule (D6) passes, persist **in the background**.

Nothing else in the pipeline changes: `_execute_fetch`'s routing, extraction,
formats, metadata, truncation and event emission all operate on the returned
`FetchResult` exactly as today. On a cache hit the httpx-fallback note
(`self._fallback_note`) is simply not set — harmless, because `_execute_fetch`
reads and clears it after the fetch (`smart_fetch_url.py:659–660`).

### Signature addition

`smart_fetch_url` gains `__id__: Optional[str] = None` (Open WebUI injects
the tool id when declared; `utils/tools.py:220,343`). Parameters starting
with `__` are stripped from the model-visible spec, so this does not leak
into the LLM-facing schema. `__id__` is used only to resolve the cache
directory (D2) and is `None`-safe (fallback id `"smart_fetch_url"`).

## 6. Configuration surface

Admin `Valves` (new):

| Valve | Default | Purpose |
|---|---|---|
| `cache_enabled` | `true` | Master switch: serve repeated fetches from disk. When `false`, the cache is off for everyone. |
| `cache_freshness_seconds` | `300` | How long cached content is trusted before refetching. `0` or less disables the cache (semantically: "don't trust the cache"). |
| `cache_retention_seconds` | `3600` | Delete entries unused for this long (enforced by the sweep). |
| `cache_max_entries` | `100` | Max entries on disk; LRU eviction beyond this. |
| `debug_logging` | `false` | Log fetch-cache decisions (hit / stale / miss / write-skip) at info level. Off by default — the cache itself only logs at warning and above. |

`UserValves` (new, per-user override from the chat session):

| Field | Type / options | Purpose |
|---|---|---|
| `cache_enabled` | `bool` — default `true` | Per-user cache on/off, a plain toggle from the chat (same style as `verbose`). Default `true` = follow the system setting; untoggle to disable the cache for your own requests. |

**Resolution (per request, in `smart_fetch_url`, like every other user
valve):** no method argument exists for the cache, so precedence is
**UserValve > admin Valve > default** — with the admin as the master switch:

- effective enabled = `admin.cache_enabled` **AND** `user.cache_enabled` **AND**
  `cache_freshness_seconds > 0` (freshness `<= 0` means "don't trust the
  cache" — treated as disabled, no reads and no writes, so a literal
  freshness of 0 can never cause a read-stale-refetch-rewrite churn);
- a user cannot turn the cache on when the admin disabled it (`false`
  anywhere wins);
- turning the toggle off only stops *that user's requests* from reading and
  writing — the shared directory and its entries are untouched, and other
  users keep using it.

Module constants:

| Constant | Value | Purpose |
|---|---|---|
| `CACHE_MAX_RAW_HTML_BYTES` | `2_000_000` | Per-entry size cap (D6) |
| `CACHE_TOUCH_INTERVAL_SEC` | `60` | mtime touch throttle (D3) |
| `SWEEP_INTERVAL_SEC` | `300` | Sweep cadence (D8) |
| `SWEEP_ORPHAN_AGE_SEC` | `60` | `.tmp` orphan age (D8) |

Env override: `SMART_FETCH_CACHE_DIR` (D2). No new dependencies — stdlib only
(`hashlib`, `json`, `os`, `time`, `pathlib`, `urllib.parse`, `tempfile`).

## 7. Implementation plan (ordered steps)

1. **Constants + valves.** Add the §6 constants, the four admin valves to
   `class Valves`, and a plain `cache_enabled: bool = True` toggle to
   `class UserValves` — mirroring the existing `verbose` valve style
   (`smart_fetch_url.py:121, 172`).
2. **Key derivation.** `_normalize_url()`, `_accept_group(format)`,
   `_cache_key(...)` → sha256 hex. Unit-testable pure functions.
3. **Directory resolution.** Lazy `_cache_root()` implementing D2's priority
   order; `_ensure_dir()` on first use. Guard the `open_webui.config` import
   in try/except.
4. **File primitives (sync, run via `asyncio.to_thread`).** `_read_entry`,
   `_write_entry` (atomic: write `path.tmp` with mode `0o600`, `os.replace`),
   `_touch(path)` (`os.utime`), `_delete`, `_list_entries`, `_now()` (one
   module-level clock function, monkeypatchable in tests).
5. **Async wrappers.** `_cache_get(key, config)` → payload or `None`
   (corrupt ⇒ delete + `None`); `_cache_set(key, payload, config)` creating a
   shielded background task tracked in the module-level pending set.
6. **Insertion point.** Extract the curl_cffi/httpx attempt from
   `_fetch_with_fingerprint` into `_fetch_raw`; add the disabled/fresh-hit/
   stale/miss logic of §5; add `__id__` to `smart_fetch_url`. Resolve the
   cache config in `smart_fetch_url` (user valve → admin valve → default,
   §6) and pass it down the existing explicit-parameter chain (like
   `browser`/`timeout_ms`) as a small immutable `_CacheConfig`
   (enabled, freshness, retention, max_entries). **Never** read per-user
   cache settings from instance state deep in the call chain: the instance
   is shared across concurrent requests and valves are re-set per request,
   so that would race and could apply one user's setting to another's
   request.
7. **Sweep.** Module-level `_cache_sweep_loop()` singleton started lazily by
   the first cache operation; implements D8; cancellable; `_cache_shutdown()`
   helper for tests and for hosts that manage the instance directly (same
   convention as `_aclose()`).
8. **Logging.** Minimal by default: the cache emits nothing below
   **warning** unless the admin valve `debug_logging` is on. With the valve
   on, one info line per cache decision, URL-safe (host + path only, no
   query/tokens): `fetch_cache: hit host=%s` / `stale-refetch` / `miss` /
   `write-skip reason=%s` — this is what the acceptance criteria (A1) are
   verified with. (Lines go through `logger.info` gated by the valve, not
   `logger.debug`: the deployment runs `GLOBAL_LOG_LEVEL=INFO`, which would
   filter debug out at the root regardless of the valve.)
9. **Tests** (Section 8).
10. **Docs.** Update `README.md` (valves table, a "Fetch cache" paragraph in
    Resource Lifecycle) and cross-link from `DESIGN.md`. Note in the tool
    frontmatter description that fetching is cached for
    `cache_freshness_seconds`.
11. **Deploy.** Re-paste the updated content into Open WebUI (Admin → Tools →
    save — the DB is the source of truth). Saving reloads the module on the
    next request (`tools.py:306`); no restart needed. New valves appear in
    the admin panel after save.

## 8. Testing plan

New file `test/test_cache.py` (+ small additions to `test/helpers.py`),
offline, using the existing fakes (`FakeAsyncSession`) and pointing
`SMART_FETCH_CACHE_DIR` at a `tmp_path` per test:

- **Key derivation:** normalization (case, default port, fragment, query
  preservation), accept-group sharing (`skimmd/markdown/html/txt` same key;
  `json` and `raw` separate), browser in key, proxy deliberately out of it.
- **Hit/stale/miss:** fresh hit serves payload and performs no fetch (fake
  session records call count); stale entry triggers exactly one refetch and
  rewrites `createdAt`; miss fetches and (after awaiting the pending-write
  set) persists.
- **Write rule:** binary content-type and 4xx/5xx and empty body and
  oversized `raw_html` are never written; `raw_bytes` never stored.
- **Corruption:** invalid JSON on disk ⇒ deleted and treated as miss.
- **Lifecycle:** mtime touch throttling; sweep evicts by retention; LRU cap
  eviction (oldest mtime first); `.tmp` orphan cleanup; stale-but-accessed
  entries survive the sweep; sweep idempotent.
- **Concurrency:** two concurrent requests for the same URL both fetch (no
  single-flight) and both write atomically; no corruption, no blocking.
- **User override:** user toggle off ⇒ no reads/writes for that user's
  requests even with the admin valve on; user toggle on + admin
  `cache_enabled=false` ⇒ still off (admin wins).
- **Disabled:** admin `cache_enabled=false` or user toggle off behaves
  exactly like today (no reads, no writes).
- **Clock:** `_now()` monkeypatched where time-dependent.
- `_cache_shutdown()` in teardown (no dangling tasks/threads — same
  discipline as `probe_unbounded.py` for the pool).

Existing suites keep passing unchanged: they call `_fetch_with_curl_cffi`
directly (below the cache) or `_fetch_with_fingerprint` with
`SMART_FETCH_CACHE_DIR` isolated (`test_real_urls.py`).

## 9. Acceptance criteria

- **A1 — Reformat without refetch:** same URL in `skimmd`, then in
  `markdown`, within the freshness window → no second network request
  (with the `debug_logging` valve on: `fetch_cache: hit`; upstream access
  log shows one request).
- **A2 — Re-truncation by length:** small `max_chars`, then larger, within
  the window → more content returned, no refetch.
- **A3 — Freshness:** once `createdAt` exceeds the window, the next request
  refetches (no stale content served) and rewrites the entry (retention
  untouched).
- **A4 — Retention:** an entry never accessed again is deleted by the sweep
  after `retention`; an entry that keeps being accessed is never deleted,
  even when stale.
- **A5 — Concurrency:** two simultaneous requests for the same URL stay
  responsive and correct; duplicate fetches are acceptable; no corruption.
- **A6 — Binary exclusion:** a PDF/DOCX/image URL is never written to disk
  and always triggers a fresh fetch.
- **A7 — Error exclusion:** a failing URL (404/timeout) is never written and
  is fetched again on the next request.
- **A8 — Degradation:** with admin `cache_enabled=false` or the user's
  toggle off (or cache I/O failing), behaviour is identical to today.
- **A9 — Non-blocking:** with a large cache directory or slow disk, the event
  loop stays responsive (measured during A5; no cache op on the loop).

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Cookie/consent-wall pages: first-visit content (e.g. consent banner) cached for the freshness window | Bounded by 300 s freshness; same trade-off as any freshness cache; acceptable for ephemeral scope. Do not follow `Set-Cookie` signals in this phase. |
| URLs with signed/expiring query tokens pollute the cache | Tokens make keys unique → bounded by LRU cap; hash filenames keep tokens out of the filesystem. |
| Concurrent processes (future multi-worker) race on the same dir | Atomic `os.replace`; duplicate fetches accepted (no single-flight); sweep races are benign (stat + delete). |
| Tool content edit replaces the module mid-sweep | Old sweep task may overlap briefly; idempotent and stat-only, so harmless. |
| `/app/backend/data` on tmpfs in some other deployment | Not the case here (named volume, driver `local`); `SMART_FETCH_CACHE_DIR` exists for deployments that need another location. |
| RAM spikes from large cached bodies | `CACHE_MAX_RAW_HTML_BYTES = 2 MiB` caps the per-hit materialisation and the disk footprint. |
| Importing `open_webui.config` couples the tool to OWUI internals | Lazy, guarded import with documented fallbacks (D2); used only to resolve the canonical directory. |

## 11. Changes vs the earlier draft

| # | Draft | This document |
|---|---|---|
| 1 | Claimed Open WebUI re-instantiates tools per call | Corrected with evidence: instance cached per process in `app.state.TOOLS`, reloaded only on content change (§2) — the *RAM* argument now justifies disk, not lifecycle |
| 2 | Both clocks in the JSON; deferred 60 s rewrites for `lastAccessed` | `lastAccessed` = file mtime + `os.utime`; sweep is stat-only (D3) |
| 3 | "Served in milliseconds" | Reframed: hit avoids the network leg; extraction (200–500 ms) still runs per request (Purpose/A1) |
| 4 | Location generic (`$TMPDIR/smart_fetch_url_cache`) | Resolved to Open WebUI's per-tool cache dir with the full evidence chain (§2, D2) |
| 5 | Size bounded by entry count only | Added `CACHE_MAX_RAW_HTML_BYTES` (D6) |
| 6 | Acceptance criterion about "streaming pipeline / `reasoning_content`" | Removed (pipe-specific, not applicable to this tool); criteria now log-verifiable (A1) |
| 7 | Implicit single/multi-worker neutrality | Single worker confirmed for the target deployment; multi-worker noted as safe but out of scope (§2) |
| 8 | Disable only via `cache_freshness_seconds = 0` (admin-only) | Requirement review: per-user on/off. Admin `cache_enabled` master switch (`bool`) + plain per-user toggle `cache_enabled: bool` (default `true` = follow the system) — freshness is now a pure duration (§6) |
| 9 | Proxy in the key material | Review: proxy dropped from the key — admin-only and effectively constant per deployment; a runtime valve change is assumed not to alter content within the freshness window. Browser stays (also a UserValve) (D4) |

## References

- Open WebUI `main` @ `2a960a59` (2026-08-31): `backend/open_webui/utils/plugin.py:206, 324–337`; `backend/open_webui/utils/tools.py:220, 303–308, 343`; `backend/open_webui/utils/middleware.py:2916, 3150+`; `backend/open_webui/config.py:181`; `backend/open_webui/env.py:222, 267`; `backend/start.sh` (`UVICORN_WORKERS` default 1).
- This tool: `smart_fetch_url.py` — valves `:121`, `_execute_fetch` `:623`, `_fetch_with_fingerprint` `:838`, text classification `:963/:1014`, Accept selection `:861–868`, `_try_alternate_fallback` `:1390`.
- Deployment (container `open-webui`): env + mounts via `docker inspect` (no `UVICORN_WORKERS`; volume `openwebui_data` → `/app/backend/data`).
