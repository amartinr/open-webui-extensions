# Memory Leak Fixes — TODO

## ✅ 1. Shared httpx client + per-request headers/timeout in curl_cffi  [DONE]

**What was actually done** (refined after review):

- **curl_cffi**: Kept `async with AsyncSession(...)` per call — `__aexit__` guarantees proper handle cleanup on every path, avoiding stale `CURLM*` handles. Moved `headers` and `timeout` from the `AsyncSession` constructor to `session.get()` for per-request control (prevents a slow-draining body from holding the session slot for the full duration).
- **httpx**: Added `_get_httpx_client()` — a single shared `AsyncClient` created lazily on first use. Reusing one client keeps the TCP connection pool alive across requests (keep-alive), recommended by httpx docs. Removed per-call `async with httpx.AsyncClient(...)`.

**Files**:
- `smart_fetch_url.py` — `__init__` (+`self._httpx_client`), `_get_httpx_client()` (new), `_fetch_with_curl_cffi` (headers+timeout moved to `.get()`), `_fetch_with_httpx` (uses shared client)

---

## ✅ 2. Separate read timeout in `session.get()`  [DONE — rolled into #1]

**Already done** as part of #1: `timeout` is now passed to `session.get(url, headers=headers, timeout=timeout_sec, ...)` instead of the `AsyncSession` constructor.

---

## ✅ 3. Limit alternate fallback cascade  [DONE]

**Fix**: Reduced alternate attempts from 3 → 1 (`candidates[:3]` → `candidates[:1]`). One fallback covers the common case without multiplying batch pressure. Added comment explaining why.

**Files**:
- `smart_fetch_url.py` — `_try_alternate_fallback` line 728

---

## 🟢 4. Pin minimum curl_cffi version

**Issue**: Older versions of `curl_cffi` (< 0.7.x) have known issues with freeing libcurl handles on errors, which is the root cause of potential leaks.

**Fix**: Change the requirements string to pin `curl_cffi>=0.7.0`.

**Files**:
- `smart_fetch_url.py` — module docstring `requirements:`
- `README.md` — requirements section

---

## 🟢 5. Extraction cache for alternate fallback

**Issue**: If an alternate URL resolves to the same page (e.g., `?format=json` variant of the same URL), the HTML is re-parsed by trafilatura/selectolax, duplicating the memory and CPU work.

**Fix**: Keep a simple dict cache `{url: extracted_dict}` within a single `smart_fetch_url` call, keyed by normalized (final) URL.

**Files**:
- `smart_fetch_url.py` — add a local cache dict, check before calling `_extract_content` in `_try_alternate_fallback`
