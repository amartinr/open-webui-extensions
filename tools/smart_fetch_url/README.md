# Smart Fetch URL

An Open WebUI tool for fetching URLs with browser-grade TLS fingerprinting and clean content extraction.

A Python port of [pi-smart-fetch](https://pi.dev/packages/pi-smart-fetch) by [Thinkscape](https://github.com/Thinkscape/agent-smart-fetch), adapted for Open WebUI.

## Features

- **TLS fingerprinting** - impersonates real browsers (Chrome, Firefox, Safari, Edge) via `curl_cffi`
- **Content-type detection** - classifies content as article, feed (RSS/Atom), or listing (forums, link aggregators) and uses the right extractor for each
- **Smart Content-Type routing** - handles binary files, extractable documents (PDF, DOCX), and text/HTML with different code paths
- **Rich metadata** - title, author, site, language, published date
- **Alternate content fallback** - follows `<link rel="alternate">` when extraction yields thin content
- **Single + batch** — one interface: pass a list with one URL or many; batch uses bounded concurrency
- **Multiple output formats** - markdown, html, text, json, raw, skimmd
- **Fetch cache** - repeated fetches of the same URL are served from an ephemeral on-disk cache (see README "Fetch Cache" and CACHE.md)
- **UserValves** - per-user overrides for all config settings (max_chars, timeout, browser, concurrency) from the chat session, plus the `cache_enabled` fetch-cache toggle

## Requirements

Installed automatically by Open WebUI on first load:

- `curl_cffi` - TLS/HTTP2 fingerprinting
- `trafilatura` - content extraction
- `selectolax` - HTML parsing fallback

## Usage

Import into Open WebUI at **Workspace → Tools → +** and attach to a model.

### `smart_fetch_url`

```
smart_fetch_url(urls, format="skimmd", max_chars=None, include_replies=False)
```

- ``urls`` — http(s) URL(s) to fetch. One URL = full pipeline; several =
  concurrent batch.
- ``format`` — ``skimmd`` (default), ``markdown``, ``html``, ``txt``,
  ``json`` or ``raw``.
- ``max_chars`` — per-result output cap; falls back to the ``max_chars``
  valve when omitted.
- ``include_replies`` — include comments/replies when the extractor
  supports them.
- ``__event_emitter__`` / ``__user__`` / ``__id__`` — injected by the
  Open WebUI harness for progress events, per-user valve overrides and the
  tool id (used to resolve the per-tool cache directory).

Transport settings (``browser``, ``timeout_ms``, ``proxy``,
``concurrency``, …) are **not method arguments** — they are configured
through valves. Configuration is resolved with the following precedence:
**method argument > UserValve (chat) > admin Valve > global default**;
settings that have no method argument resolve as
**UserValve > admin Valve > default**.

Pass a single-element list for a single fetch, or multiple URLs for
concurrent batch fetching — batches are truncated at 10 URLs (with a
warning note), fetched with at most ``batch_concurrency`` in flight.

## Fetch Cache

Repeated fetches of the **same URL within a short window** are served from
an ephemeral on-disk cache instead of hitting the network again — the
agent's immediate retries (another format, a larger ``max_chars``) stop
hammering the upstream site.

- **What is cached:** the fetched text body (`raw_html`), at the raw level —
  every format, metadata and re-truncation are regenerated from it per
  request. Binary responses (PDF/DOCX/images/…) and errors (non-2xx) are
  never cached.
- **Location:** auto-calculated from Open WebUI's own paths —
  `<DATA_DIR>/cache/tools/<tool_id>/fetch_cache` (nothing hardcoded, no env
  override).
- **Freshness:** cached content is trusted for `cache_freshness_seconds`
  (default 300; `0` or less = disabled) before refetching.
- **Retention:** entries unused for `cache_retention_seconds` (default
  3600) are deleted by a periodic sweep, which also caps the directory at
  `cache_max_entries` (default 100) via LRU.
- **Per-user on/off:** the `cache_enabled` user valve turns the cache off
  for your own requests; the admin `cache_enabled` master switch turns it
  off for everyone.
- **Logging:** silent below warning by default; the admin `debug_logging`
  valve logs one line per cache decision (hit / stale-refetch / miss /
  write-skip, URL-safe).

Full design and implementation plan: [CACHE.md](./CACHE.md). Known issues:
[ISSUES.md](./ISSUES.md).

## Output Formats

| Format | Description | Use case |
|---|---|---|
| `skimmd` | Skimmed Markdown (default) — whitelist-based HTML-to-MD converter | Feeds, listings, media-rich pages |
| `markdown` | Clean text via trafilatura | Articles, blog posts |
| `html` | Lightly cleaned HTML | When structure matters |
| `txt` | Plain text, no formatting | Minimal token usage |
| `json` | Structured output with metadata | Programmatic consumption |
| `raw` | Full unprocessed server response | Debugging, passthrough |

### `skimmd` — Skimmed Markdown

Preserves **all links, images, and videos** while stripping navigation, scripts,
and structural noise. Ideal for Reddit frontpages, forum threads, search results,
galleries, or any page where trafilatura's article extraction is too aggressive.

- **Zero external dependencies** — uses only stdlib (`html.parser`, `re`, `urllib`)
- **Whitelist-based** — only known-safe tags survive; everything else is stripped
- **`strip_external=True`** — blocks containing only external links are discarded;
  mixed blocks keep internal content and drop external anchor text
- **Inline in `smart_fetch_url.py`** — no separate import needed when pasted into
  Open WebUI

### UserValves (per-user, configurable from chat)

| Field | Type | Description |
|---|---|---|
| `max_chars` | `int` | Maximum characters to return |
| `timeout_ms` | `int` | Request timeout in milliseconds |
| `default_browser` | `str` | Browser fingerprint profile |
| `batch_concurrency` | `int` | Concurrency for batch fetches |
| `blocked_domains` | `str` | Extra domains to block (added to the admin list) |
| `verbose` | `bool` | Emit detailed status events |
| `cache_enabled` | `bool` | Use the fetch cache for my requests (default `true`; the admin master switch still applies) |

## Resource Lifecycle

How this tool manages its long-lived resources, and what you can rely on
when running it inside Open WebUI.

### One instance per tool content version

The Open WebUI harness caches one module instance per tool in
`request.app.state.TOOLS` and **re-creates it whenever the tool content
changes** (an admin edit). Each superseded instance is dropped from the
cache; see below for how its resources are released.

### Cleanup: `weakref.finalize`, not `atexit`

`Tools()` registers a `weakref.finalize` that shuts down the thread pool
as a last resort. It holds only a weakref to the instance, so instances
are **collectable by GC** (unlike the previous `atexit.register` pinning,
which kept every instantiated `Tools` alive for the process lifetime and
defeated `__del__`). Idle thread-pool workers exit on their own once the
instance is collected — they hold only a weakref to the executor.

### curl sessions: keyed by `(browser, proxy)`, closed explicitly

- `curl_cffi.AsyncSession` has **no destructor** — an open session keeps
  its keep-alive connection pool, cookies and `curl_multi` handle alive
  until `close()` is called explicitly.
- Sessions are cached per `(browser, proxy)` pair
  (`_curl_sessions[f"{browser}::{proxy or 'direct'}"]`), so changing the
  `proxy` valve yields a fresh session with the correct proxy instead of
  silently reusing a stale one.
- The cache is bounded by `MAX_CACHED_SESSIONS = 8` (2 per browser x 4
  browsers) with LRU eviction; evicted sessions are closed so their
  connection pools do not linger.
- `_aclose()` (async) closes every cached session and the thread pool;
  it is idempotent and never raises. The Open WebUI harness has no async
  tool-teardown hook, so call it from your own lifecycle code when you
  manage the instance directly (tests, scripts, previews).

### Zombie threads on timeout (known limitation)

`concurrent.futures` cannot kill a running thread: when an extraction
times out or is cancelled, the worker **continues to completion** and
occupies a pool slot until it finishes. The pool is bounded
(`max(4, CPU_COUNT * 2)` workers) and the per-instance semaphore caps
concurrent extractions at 4, so this is a transient degradation, not a
leak — but on a small server it can cause contention. See the tuning
guidance below.

### Tuning for small (2-vCPU) deployments

At 2 vCPUs the pool has the minimum 4 workers and the GIL serializes
CPU-bound extraction, so timeouts become more likely and abandoned tasks
(zombie threads) pile up on the queue, each retaining its `raw_html`
until it drains. Recommended valve settings:

| Setting | Recommended | Why |
|---|---|---|
| `batch_concurrency` | 2-3 | fewer simultaneous fetches competing for the 4 workers |
| `timeout_ms` | 25000-30000 | counter-intuitive: fewer timeouts => fewer zombies |
| `requests_per_second` | 5 | smooths the input rate |

Raising the internal `THREAD_TIMEOUT_SEC` (default 5) to 10-15 s has the
same effect and is the single most effective knob on that hardware.

## License

MIT - see [LICENSE](./LICENSE).

This project is a derivative of [pi-smart-fetch](https://pi.dev/packages/pi-smart-fetch) by Thinkscape, also MIT licensed.
