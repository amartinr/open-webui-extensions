"""
Phase 4 — Resource lifecycle regression tests.

Verifies the fixes for resource management in Tools:

- F1: instances are no longer pinned by atexit (weakref.finalize instead);
       del + gc.collect() reclaims them without any atexit.unregister call.
- F2: _aclose() closes every cached curl session, empties the cache, is
       idempotent, never raises even if a session's close() fails, and
       _close() remains pool-only (sessions need the async path).
- F3: the session cache is keyed by (browser, proxy); a proxy change creates
       a fresh session with the correct proxy, same-key calls reuse, and the
       cache is bounded by MAX_CACHED_SESSIONS with LRU eviction that closes
       the evicted session.
"""

import asyncio
import gc
import subprocess
import sys
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `from helpers import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `from smart_fetch_url import ...`

from helpers import (
    FakeAsyncSession,
    REPO_DIR,
    cleanup_tools,
    install_fake_sessions,
    restore_sessions,
)
from smart_fetch_url import Tools

PROBE = Path(__file__).resolve().parent / "probe_unbounded.py"


# ═══════════════════════════════════════════════
#  F1 — no atexit pinning
# ═══════════════════════════════════════════════

def test_instance_collected_without_atexit_unregister():
    """The instance MUST be collected after del + gc with no atexit.unregister.

    This is the inversion of the pre-fix behavior, where atexit kept a strong
    reference to every Tools() for the process lifetime.
    """
    t = Tools()
    wr = weakref.ref(t)
    del t
    gc.collect()
    assert wr() is None, (
        "instance must be collectable without atexit.unregister (no pinning)"
    )


def test_instances_and_threads_reclaimed_in_no_cache_harness():
    """No-cache harness scenario: many Tools() with live pool threads.

    Runs the probe in a subprocess for clean isolation (thread counts).
    Pre-fix, atexit pinned every instance so the pool threads survived
    del + gc indefinitely. Post-fix the instances are collected, the
    executors lose their last strong reference, and the worker threads
    exit on their own (workers hold only a weakref to the executor).

    Output lines: baseline / after_create / after_del_gc_no_atexit /
    after_aclose.
    """
    r = subprocess.run(
        [sys.executable, str(PROBE), str(REPO_DIR)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"probe failed:\n{r.stderr}"
    print(r.stdout)

    parsed = {}
    for line in r.stdout.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            parsed[k.strip()] = int(v.strip())

    assert parsed["baseline"] == 0, "clean process should have no smart_fetch threads"
    assert parsed["after_create"] >= 6, "6 instances x pool submit should spawn threads"
    assert parsed["after_del_gc_no_atexit"] == 0, (
        "instances must be reclaimed after del + gc (no atexit pinning), so "
        "the pool threads must exit on their own"
    )
    assert parsed["after_aclose"] == 0, (
        "explicit _aclose() must terminate the pool threads"
    )


# ═══════════════════════════════════════════════
#  F2 — _aclose() closes sessions explicitly
# ═══════════════════════════════════════════════

def test_aclose_closes_all_sessions_and_empties_cache():
    async def scenario():
        tools = Tools()
        tools._curl_sessions = {
            "firefox::direct": FakeAsyncSession(),
            "chrome::direct": FakeAsyncSession(),
        }
        sessions = list(tools._curl_sessions.values())
        await tools._aclose()
        for s in sessions:
            assert s.closed, "every cached session must be closed by _aclose()"
        assert tools._curl_sessions == {}, "cache must be emptied"

        # idempotent
        await tools._aclose()
        assert tools._curl_sessions == {}
        assert tools._thread_pool is None, "_aclose() must shut down the pool"

    asyncio.run(scenario())


def test_aclose_survives_session_errors():
    async def scenario():
        tools = Tools()

        class ExplodingSession:
            async def close(self):
                raise RuntimeError("boom")

        tools._curl_sessions["edge::direct"] = ExplodingSession()
        tools._curl_sessions["safari::direct"] = FakeAsyncSession()
        await tools._aclose()  # must not raise
        assert tools._curl_sessions == {}
        # _close_session never raises either
        await tools._close_session(ExplodingSession())

    asyncio.run(scenario())


def test_aclose_handles_sync_close():
    """_close_session tolerates sessions exposing a sync close() (legacy)."""
    async def scenario():
        tools = Tools()

        class SyncSession:
            closed = False

            def close(self):
                self.closed = True

        s = SyncSession()
        await tools._close_session(s)
        assert s.closed

    asyncio.run(scenario())


def test_close_is_pool_only():
    """_close() must NOT close or drop curl sessions.

    Closing sessions requires await (AsyncSession.close()), which the sync
    _close() path cannot do; only _aclose() handles sessions. This pins the
    documented contract.
    """
    async def scenario():
        tools = Tools()
        tools._curl_sessions["firefox::direct"] = FakeAsyncSession()
        sess = tools._curl_sessions["firefox::direct"]

        tools._close()
        assert tools._curl_sessions["firefox::direct"] is sess, (
            "_close() must not remove sessions from the cache"
        )
        assert sess.closed is False, (
            "_close() must not close curl sessions (async-only)"
        )
        await cleanup_tools(tools)

    asyncio.run(scenario())


# ═══════════════════════════════════════════════
#  F3 — (browser, proxy) keying + LRU bound
# ═══════════════════════════════════════════════

def test_proxy_change_creates_new_session():
    async def scenario():
        tools = Tools()
        ccr, original = install_fake_sessions()
        try:
            await tools._fetch_with_curl_cffi(
                "https://a.example", "chrome", {}, 5000, proxy=None
            )
            assert len(FakeAsyncSession.created) == 1
            assert FakeAsyncSession.created[0].kwargs["proxies"] is None
            assert tools._curl_sessions.get("chrome::direct") is not None
            assert "chrome" not in tools._curl_sessions, "no legacy bare-key entry"

            # proxy change -> fresh session with the correct proxy
            await tools._fetch_with_curl_cffi(
                "https://b.example", "chrome", {}, 5000,
                proxy="http://proxy-b:8080",
            )
            assert len(FakeAsyncSession.created) == 2, (
                "proxy change must create a new session (old key no longer reused)"
            )
            new = FakeAsyncSession.created[1]
            assert new.kwargs["proxies"] == {
                "http": "http://proxy-b:8080",
                "https": "http://proxy-b:8080",
            }
            assert tools._curl_sessions.get("chrome::http://proxy-b:8080") is new
            # superseded session stays cached under its own key (reusable)
            assert "chrome::direct" in tools._curl_sessions
        finally:
            restore_sessions(ccr, original)
            await cleanup_tools(tools)

    asyncio.run(scenario())


def test_same_key_reuses_session():
    async def scenario():
        tools = Tools()
        ccr, original = install_fake_sessions()
        try:
            for _ in range(3):
                await tools._fetch_with_curl_cffi(
                    "https://a.example", "firefox", {}, 5000, proxy="http://p1:1"
                )
            assert len(FakeAsyncSession.created) == 1, (
                "same (browser, proxy) key must reuse the cached session"
            )
        finally:
            restore_sessions(ccr, original)
            await cleanup_tools(tools)

    asyncio.run(scenario())


def test_session_cache_lru_bound():
    """Cache never exceeds MAX_CACHED_SESSIONS; evicted sessions are closed."""
    async def scenario():
        tools = Tools()
        ccr, original = install_fake_sessions()
        try:
            for i in range(8):
                await tools._fetch_with_curl_cffi(
                    "https://x.example", "firefox", {}, 5000, proxy=f"http://p{i}:1"
                )
            assert len(tools._curl_sessions) == 8

            # touch the oldest entry (p0) to refresh its LRU position
            await tools._fetch_with_curl_cffi(
                "https://x.example", "firefox", {}, 5000, proxy="http://p0:1"
            )
            # inserting a 9th distinct proxy evicts the least-recently-used (p1)
            await tools._fetch_with_curl_cffi(
                "https://x.example", "firefox", {}, 5000, proxy="http://new:1"
            )
            assert len(tools._curl_sessions) == 8, "cache must stay bounded"
            assert "firefox::http://p1:1" not in tools._curl_sessions, (
                "LRU must evict the least-recently-used session"
            )
            assert "firefox::http://p0:1" in tools._curl_sessions, (
                "touched session must survive eviction"
            )

            evicted = next(
                s for s in FakeAsyncSession.created
                if (s.kwargs.get("proxies") or {}).get("https") == "http://p1:1"
            )
            assert evicted.closed, (
                "evicted session must be closed so its connection pool does not linger"
            )
        finally:
            restore_sessions(ccr, original)
            await cleanup_tools(tools)

    asyncio.run(scenario())
