"""
Phase 4 — Resource lifecycle regression tests.

Verifies the fixes for resource management in Tools:

- F1: instances are no longer pinned by atexit (weakref.finalize instead);
       del + gc.collect() reclaims them without any atexit.unregister call.
- F2: _aclose() closes every cached curl session, empties the cache, is
       idempotent, and never raises even if a session's close() fails.
- F3: the session cache is keyed by (browser, proxy); a proxy change creates
       a fresh session with the correct proxy, same-key calls reuse, and the
       cache is bounded by MAX_CACHED_SESSIONS with LRU eviction that closes
       the evicted session.
"""

import asyncio
import gc
import sys
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import Tools  # noqa: E402


# ═══════════════════════════════════════════════
#  Test helpers
# ═══════════════════════════════════════════════

class FakeResponse:
    headers = {"content-type": "text/html; charset=utf-8"}
    url = "https://example.com/final"
    status_code = 200
    content = b"<html><body><p>hello world</p></body></html>"
    text = "<html><body><p>hello world</p></body></html>"


class FakeAsyncSession:
    """Records every created session; close() sets ``closed``."""

    _created: list["FakeAsyncSession"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeAsyncSession._created.append(self)

    async def get(self, *args, **kwargs):
        return FakeResponse()

    async def close(self):
        self.closed = True


def install_fake_sessions():
    """Monkeypatch curl_cffi's AsyncSession with a recording fake.

    The tool imports AsyncSession lazily inside _fetch_with_curl_cffi, so
    patching the module attribute is enough.
    """
    import curl_cffi.requests as ccr

    original = ccr.AsyncSession
    FakeAsyncSession._created = []
    ccr.AsyncSession = FakeAsyncSession
    return ccr, original


def restore_sessions(ccr, original):
    ccr.AsyncSession = original


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


def test_many_instances_with_live_pools_reclaimed():
    """No-cache harness scenario: many Tools() with live pool threads.

    Pre-fix, atexit pinned every instance, so their pool threads survived
    del + gc indefinitely. Post-fix the instances are collected, the
    executors lose their last strong reference, and the worker threads exit
    on their own (workers hold only a weakref to the executor).
    """
    import threading
    import time

    instances = []
    for _ in range(4):
        t = Tools()
        t._get_thread_pool().submit(time.sleep, 0.2)
        instances.append(t)
    time.sleep(0.4)

    refs = [weakref.ref(t) for t in instances]
    del t  # the loop variable would otherwise pin the last instance
    del instances
    gc.collect()
    for wr in refs:
        assert wr() is None, "no atexit handler may pin instances"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        alive = sum(
            1 for th in threading.enumerate() if th.name.startswith("smart_fetch")
        )
        if alive == 0:
            break
        time.sleep(0.1)
        gc.collect()
    assert alive == 0, "pool threads must be reclaimed after del + gc"


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
            assert len(FakeAsyncSession._created) == 1
            assert FakeAsyncSession._created[0].kwargs["proxies"] is None
            assert tools._curl_sessions.get("chrome::direct") is not None
            assert "chrome" not in tools._curl_sessions, "no legacy bare-key entry"

            # proxy change -> fresh session with the correct proxy
            await tools._fetch_with_curl_cffi(
                "https://b.example", "chrome", {}, 5000,
                proxy="http://proxy-b:8080",
            )
            assert len(FakeAsyncSession._created) == 2, (
                "proxy change must create a new session (old key no longer reused)"
            )
            new = FakeAsyncSession._created[1]
            assert new.kwargs["proxies"] == {
                "http": "http://proxy-b:8080",
                "https": "http://proxy-b:8080",
            }
            assert tools._curl_sessions.get("chrome::http://proxy-b:8080") is new
            # superseded session stays cached under its own key (reusable)
            assert "chrome::direct" in tools._curl_sessions
        finally:
            restore_sessions(ccr, original)
            await tools._aclose()

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
            assert len(FakeAsyncSession._created) == 1, (
                "same (browser, proxy) key must reuse the cached session"
            )
        finally:
            restore_sessions(ccr, original)
            await tools._aclose()

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
                s for s in FakeAsyncSession._created
                if (s.kwargs.get("proxies") or {}).get("https") == "http://p1:1"
            )
            assert evicted.closed, (
                "evicted session must be closed so its connection pool does not linger"
            )
        finally:
            restore_sessions(ccr, original)
            await tools._aclose()

    asyncio.run(scenario())
