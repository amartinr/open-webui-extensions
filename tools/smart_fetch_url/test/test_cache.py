"""Tests for the on-disk fetch cache (see CACHE.md).

Coverage by step:
- config surface: valve/constant defaults and schema constraints;
- key derivation (D4): URL normalization, accept groups, sha256 filename;
- directory resolution + file primitives (D2, D5–D7): atomic write/read,
  corrupt-entry handling, freshness predicate, throttled touch, background
  writes. The cache is not wired to the fetch path yet — the wiring tests
  come with the insertion step.
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smart_fetch_url as sf
from smart_fetch_url import Tools


# ── config surface (step 1) ─────────────────────────────────────────────

def test_admin_valve_defaults():
    v = Tools.Valves()
    assert v.cache_enabled is True
    assert v.cache_freshness_seconds == 300
    assert v.cache_retention_seconds == 3600
    assert v.cache_max_entries == 100
    assert v.debug_logging is False


def test_user_valve_defaults():
    uv = Tools.UserValves()
    assert uv.cache_enabled is True


def test_constants_present():
    assert sf.CACHE_MAX_RAW_HTML_BYTES == 2_000_000
    assert sf.CACHE_TOUCH_INTERVAL_SEC == 60
    assert sf.SWEEP_INTERVAL_SEC == 300
    assert sf.SWEEP_ORPHAN_AGE_SEC == 60
    assert sf.DEFAULT_CACHE_FRESHNESS_SEC == 300
    assert sf.DEFAULT_CACHE_RETENTION_SEC == 3600
    assert sf.DEFAULT_CACHE_MAX_ENTRIES == 100


def test_freshness_zero_is_valid_config():
    """freshness <= 0 is a legitimate admin value (cache disabled).

    The valve schema must accept 0; the resolution layer (wired later)
    treats it as disabled. Retention and max entries reject 0/negative via
    their constraints.
    """
    v = Tools.Valves(cache_freshness_seconds=0)
    assert v.cache_freshness_seconds == 0

    import pydantic

    for kwargs in ({"cache_retention_seconds": 0}, {"cache_max_entries": 0}):
        try:
            Tools.Valves(**kwargs)
        except pydantic.ValidationError:
            pass
        else:
            raise AssertionError(f"expected ValidationError for {kwargs}")


# ── key derivation (step 2, CACHE.md §D4) ───────────────────────────────

def test_accept_group():
    assert sf._accept_group("json") == "json"
    assert sf._accept_group("raw") == "raw"
    for fmt in ("skimmd", "markdown", "html", "txt"):
        assert sf._accept_group(fmt) == "html"


def test_normalize_url():
    n = sf._normalize_url
    # scheme + host lowercased
    assert n("HTTP://Example.COM/a") == "http://example.com/a"
    # default ports dropped, non-default kept
    assert n("https://example.com:443/a") == "https://example.com/a"
    assert n("http://example.com:80/a") == "http://example.com/a"
    assert n("https://example.com:8443/a") == "https://example.com:8443/a"
    # fragment removed, query preserved as-is
    assert n("https://example.com/p?q=1&x=2#frag") == "https://example.com/p?q=1&x=2"
    assert n("https://example.com/p#frag") == "https://example.com/p"
    # userinfo preserved
    assert n("https://user:pass@Example.com/p") == "https://user:pass@example.com/p"
    # empty path becomes "/"
    assert n("https://example.com") == "https://example.com/"
    # unparseable input is left untouched
    assert n("not a url") == "not a url"


def test_cache_key_deterministic_and_sensitive():
    k = sf._cache_key
    url = "https://Example.com/path?q=1"
    # deterministic
    assert k(url, "firefox", "markdown") == k(url, "firefox", "markdown")
    # sha256 hex, 64 chars
    assert len(k(url, "firefox")) == 64
    # URL case-insensitivity via normalization
    assert k("https://EXAMPLE.com/path?q=1", "firefox") == k(url, "firefox")
    # sensitive to browser and accept group
    assert k(url, "chrome") != k(url, "firefox")
    assert k(url, "firefox", "json") != k(url, "firefox", "markdown")
    assert k(url, "firefox", "raw") != k(url, "firefox", "markdown")
    # html-family formats share the entry
    assert k(url, "firefox", "skimmd") == k(url, "firefox", "markdown")
    # the plaintext URL never appears in the key
    assert url not in k(url, "firefox")
    assert "Example.com" not in k(url, "firefox")


# ── directory resolution + file primitives (step 3, CACHE.md §D2–D7) ───

def _entry(created: float | None = None) -> dict:
    return {
        "createdAt": created if created is not None else sf._cache_now(),
        "raw_html": "<html><body><p>hello</p></body></html>",
        "final_url": "https://example.com/final",
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "resp_headers": {},
    }


def _tmp_root() -> Path:
    return Path(tempfile.mkdtemp()) / "cache"


def test_cache_root_standalone_fallback():
    """Without open_webui importable, the root is a per-tool dir under TMPDIR.

    (In-process, _cache_root imports open_webui.config.CACHE_DIR and appends
    /tools/<tool_id>/fetch_cache — not exercised here, where open_webui is
    not installed; the path-based primitives below are directory-agnostic.)
    """
    root = sf._cache_root(None)
    assert root.name == "smart_fetch_url"
    assert root.parent.name == "smart_fetch_url_cache"
    assert sf._cache_root("my_tool").name == "my_tool"


def test_write_read_roundtrip_and_permissions():
    async def scenario():
        root = _tmp_root()
        key = "k" * 64
        entry = _entry()
        await sf._cache_store(key, root, entry)
        path = sf._entry_path(root, key)
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600
        assert await asyncio.to_thread(sf._read_entry, path) == entry

    asyncio.run(scenario())


def test_no_tmp_leftover_after_write():
    async def scenario():
        root = _tmp_root()
        key = "k" * 64
        await sf._cache_store(key, root, _entry())
        assert [p for p in root.iterdir() if p.suffix == ".tmp"] == []

    asyncio.run(scenario())


def test_corrupt_entry_is_deleted():
    root = _tmp_root()
    root.mkdir(parents=True)
    path = sf._entry_path(root, "k" * 64)
    path.write_text("{not json", encoding="utf-8")
    assert sf._read_entry(path) is None
    assert not path.exists(), "corrupt entry must be deleted"


def test_read_io_error_returns_none_keeps_file():
    root = Path("/nonexistent-dir-xyz") / "cache"
    assert sf._read_entry(sf._entry_path(root, "k" * 64)) is None


def test_entry_is_fresh():
    now = 1_000_000.0
    assert sf._entry_is_fresh(_entry(created=now - 100), 300, now)
    assert not sf._entry_is_fresh(_entry(created=now - 301), 300, now)
    assert not sf._entry_is_fresh({"raw_html": "x"}, 300, now), "no createdAt => stale"


def test_async_lookup_fresh_stale_missing():
    async def scenario():
        root = _tmp_root()
        key = "k" * 64
        await sf._cache_store(key, root, _entry(created=sf._cache_now()))
        entry, existed = await sf._cache_lookup(key, root, 300)
        assert entry is not None and existed
        # backdate createdAt beyond the window → stale, still "existed"
        await sf._cache_store(key, root, _entry(created=sf._cache_now() - 999))
        entry, existed = await sf._cache_lookup(key, root, 300)
        assert entry is None and existed is True
        # missing key
        entry, existed = await sf._cache_lookup("x" * 64, root, 300)
        assert entry is None and existed is False

    asyncio.run(scenario())


def test_touch_throttled():
    root = _tmp_root()
    root.mkdir(parents=True)
    key = "k" * 64
    path = sf._entry_path(root, key)
    path.write_text("x", encoding="utf-8")
    old = sf._cache_now() - 120
    os.utime(path, (old, old))
    sf._touch_entry(path)
    assert path.stat().st_mtime >= sf._cache_now() - 2, "old entry must be touched"
    mtime_after_first = path.stat().st_mtime
    sf._touch_entry(path)  # fresh (< 60 s) → skipped
    assert path.stat().st_mtime == mtime_after_first, "touch must be throttled"


def test_spawn_write_persists():
    async def scenario():
        root = _tmp_root()
        key = "k" * 64
        sf._spawn_cache_write(key, root, _entry())
        await sf._drain_pending_writes()
        assert sf._entry_path(root, key).exists()

    asyncio.run(scenario())


def test_list_entries():
    root = _tmp_root()
    root.mkdir(parents=True)
    a, b = "a" * 64, "b" * 64
    sf._write_entry(sf._entry_path(root, a), _entry())
    sf._write_entry(sf._entry_path(root, b), _entry())
    (root / "junk.tmp").write_text("x")
    names = {p.name for p in sf._list_entries(root)}
    assert names == {a + ".json", b + ".json"}


# ── periodic sweep (step 4, CACHE.md §D8) ───────────────────────────────

def _backdate(path: Path, seconds: float) -> None:
    t = sf._cache_now() - seconds
    os.utime(path, (t, t))


def test_sweep_removes_expired_keeps_fresh():
    root = _tmp_root()
    root.mkdir(parents=True)
    old, fresh = "a" * 64, "b" * 64
    p_old, p_fresh = sf._entry_path(root, old), sf._entry_path(root, fresh)
    sf._write_entry(p_old, _entry())
    sf._write_entry(p_fresh, _entry())
    _backdate(p_old, 7200)  # unused > 1 h retention
    removed_orphans, removed_expired, evicted = sf._sweep_once(
        root, 3600, 100, sf._cache_now()
    )
    assert (removed_orphans, removed_expired, evicted) == (0, 1, 0)
    assert not p_old.exists()
    assert p_fresh.exists()


def test_sweep_keeps_stale_but_accessed():
    """Freshness is not the sweep's job: an entry whose content is old but
    that was accessed recently (mtime fresh) survives."""
    root = _tmp_root()
    root.mkdir(parents=True)
    path = sf._entry_path(root, "k" * 64)
    sf._write_entry(path, _entry(created=sf._cache_now() - 9999))
    # mtime (lastAccessed) is now → within retention
    assert sf._sweep_once(root, 3600, 100, sf._cache_now()) == (0, 0, 0)
    assert path.exists()


def test_sweep_evicts_lru_beyond_cap():
    root = _tmp_root()
    root.mkdir(parents=True)
    paths = []
    for i, ch in enumerate("abc"):
        p = sf._entry_path(root, ch * 64)
        sf._write_entry(p, _entry())
        _backdate(p, (2 - i) * 3600)  # a oldest (2 h), c newest
        paths.append(p)
    _, _, evicted = sf._sweep_once(root, 999999, 2, sf._cache_now())
    assert evicted == 1
    assert not paths[0].exists(), "oldest lastAccessed must be evicted first"
    assert paths[1].exists() and paths[2].exists()


def test_sweep_cleans_orphan_tmp_keeps_fresh_tmp():
    root = _tmp_root()
    root.mkdir(parents=True)
    old_tmp, fresh_tmp = root / "old.123.tmp", root / "fresh.456.tmp"
    old_tmp.write_text("x")
    fresh_tmp.write_text("x")
    _backdate(old_tmp, 300)
    removed_orphans, _, _ = sf._sweep_once(root, 3600, 100, sf._cache_now())
    assert removed_orphans == 1
    assert not old_tmp.exists()
    assert fresh_tmp.exists(), "fresh tmp may belong to a concurrent writer"


def test_sweep_missing_dir_never_raises():
    assert sf._sweep_once(_tmp_root(), 3600, 100, sf._cache_now()) == (0, 0, 0)


def test_sweep_loop_integration():
    """The singleton loop enforces retention on its own cadence."""
    async def scenario():
        root = _tmp_root()
        root.mkdir(parents=True)
        path = sf._entry_path(root, "k" * 64)
        sf._write_entry(path, _entry())
        _backdate(path, 7200)
        sf._cache_sweep_start(root, retention_seconds=3600, interval_sec=0.05)
        try:
            deadline = asyncio.get_running_loop().time() + 1.5
            while path.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert not path.exists(), "loop must reap the expired entry"
        finally:
            await sf._cache_sweep_stop()

    asyncio.run(scenario())


def test_sweep_start_is_singleton():
    async def scenario():
        root = _tmp_root()
        try:
            sf._cache_sweep_start(root, interval_sec=3600)
            first = sf._SWEEP_TASK
            sf._cache_sweep_start(root, interval_sec=3600)
            assert sf._SWEEP_TASK is first, "second start must not spawn a new task"
        finally:
            await sf._cache_sweep_stop()

    asyncio.run(scenario())


# ── wiring into the fetch path (step 5, CACHE.md §5) ────────────────────

class _Resp:
    """Stand-in for a curl_cffi response (mirrors helpers.FakeResponse)."""

    headers = {"content-type": "text/html; charset=utf-8"}
    url = "https://example.com/final"
    status_code = 200
    content = b"<html><body><p>hello world</p></body></html>"
    text = "<html><body><p>hello world</p></body></html>"


class _CountingSession:
    """Recording fake: counts AsyncSession creations and get() calls."""

    created: list = []
    calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        _CountingSession.created.append(self)

    async def get(self, *args, **kwargs):
        _CountingSession.calls += 1
        return _Resp()

    async def close(self):
        self.closed = True


class _Resp404:
    headers = {"content-type": "text/html; charset=utf-8"}
    url = "https://example.com/404"
    status_code = 404
    content = b"<html><body>not found</body></html>"
    text = "<html><body>not found</body></html>"


class _Resp404Session(_CountingSession):
    async def get(self, *args, **kwargs):
        _CountingSession.calls += 1
        return _Resp404()


_LONG_TEXT = (
    "<html><body><p>"
    + "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    * 4
    + "</p></body></html>"
)


class _LongResp:
    headers = {"content-type": "text/html; charset=utf-8"}
    url = "https://example.com/final"
    status_code = 200
    content = _LONG_TEXT.encode()
    text = _LONG_TEXT


class _LongSession(_CountingSession):
    async def get(self, *args, **kwargs):
        _CountingSession.calls += 1
        return _LongResp()


class _RespPdf:
    headers = {"content-type": "application/pdf"}
    url = "https://example.com/doc.pdf"
    status_code = 200
    content = b"%PDF-1.4 fake"
    text = "%PDF-1.4 fake"


class _RespPdfSession(_CountingSession):
    async def get(self, *args, **kwargs):
        _CountingSession.calls += 1
        return _RespPdf()


def _patch_curl(session_cls):
    import curl_cffi.requests as ccr

    original = ccr.AsyncSession
    ccr.AsyncSession = session_cls
    _CountingSession.created = []
    _CountingSession.calls = 0
    return ccr, original


def _cfg(root: Path, freshness: float = 300.0) -> sf._CacheConfig:
    return sf._CacheConfig(freshness_seconds=freshness, root=root)


def test_wiring_miss_then_cross_format_hit():
    """First fetch stores; a second fetch in another html-family format is a
    hit (shared accept group); json is a separate entry (network again)."""
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        try:
            tools = Tools()
            root = _tmp_root()
            cfg = _cfg(root)
            r1 = await tools._fetch_with_fingerprint(
                "https://example.com", "firefox", 5000, format="markdown", cache_cfg=cfg
            )
            await sf._drain_pending_writes()
            assert _CountingSession.calls == 1
            key = sf._cache_key("https://example.com", "firefox", "markdown")
            assert sf._entry_path(root, key).exists()

            # same URL, skimmd format → same key → served from cache
            r2 = await tools._fetch_with_fingerprint(
                "https://example.com", "firefox", 5000, format="skimmd", cache_cfg=cfg
            )
            assert _CountingSession.calls == 1, "cross-format hit must not fetch"
            assert r2.raw_html == r1.raw_html
            assert r2.final_url == "https://example.com/final"
            assert r2.raw_bytes is None, "cache hits carry no raw bytes"

            # json → distinct accept group → fresh fetch
            await tools._fetch_with_fingerprint(
                "https://example.com", "firefox", 5000, format="json", cache_cfg=cfg
            )
            await sf._drain_pending_writes()
            assert _CountingSession.calls == 2
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            await tools._aclose()

    asyncio.run(scenario())


def test_wiring_stale_entry_refetches_and_rewrites():
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        try:
            tools = Tools()
            root = _tmp_root()
            cfg = _cfg(root)
            key = sf._cache_key("https://example.com", "firefox", "markdown")
            # pre-seed a stale entry (createdAt far in the past)
            await sf._cache_store(
                key, root, _entry(created=sf._cache_now() - 9999)
            )
            await tools._fetch_with_fingerprint(
                "https://example.com", "firefox", 5000, format="markdown", cache_cfg=cfg
            )
            await sf._drain_pending_writes()
            assert _CountingSession.calls == 1, "stale entry must refetch"
            entry = await asyncio.to_thread(
                sf._read_entry, sf._entry_path(root, key)
            )
            assert entry is not None
            assert entry["createdAt"] > sf._cache_now() - 5, "createdAt must reset"
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            await tools._aclose()

    asyncio.run(scenario())


def test_wiring_write_rule_skips_errors_and_binary():
    """404 and binary responses are never written (CACHE.md §D6)."""
    async def scenario():
        for session_cls in (_Resp404Session, _RespPdfSession):
            ccr, original = _patch_curl(session_cls)
            try:
                tools = Tools()
                root = _tmp_root()
                cfg = _cfg(root)
                await tools._fetch_with_fingerprint(
                    "https://example.com", "firefox", 5000, format="markdown", cache_cfg=cfg
                )
                await sf._drain_pending_writes()
                assert list(sf._list_entries(root)) == [], "nothing may be cached"
            finally:
                import curl_cffi.requests as ccr

                ccr.AsyncSession = original
                await tools._aclose()

    asyncio.run(scenario())


def test_wiring_no_cfg_never_touches_cache():
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        try:
            tools = Tools()
            root = _tmp_root()
            await tools._fetch_with_fingerprint(
                "https://example.com", "firefox", 5000, format="markdown"
            )
            await sf._drain_pending_writes()
            assert _CountingSession.calls == 1
            assert list(sf._list_entries(root)) == []
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            await tools._aclose()

    asyncio.run(scenario())


def test_wiring_end_to_end_reformat_without_refetch():
    """Full pipeline: skimmd then markdown on the same URL → one network call.

    Uses a long body: short (< 30 extracted words) pages are emptied by the
    pre-existing alternate-fallback step regardless of the cache.
    """
    async def scenario():
        ccr, original = _patch_curl(_LongSession)
        real_root = sf._cache_root
        root = _tmp_root()
        sf._cache_root = lambda tool_id=None: root
        try:
            tools = Tools()
            try:
                out1 = await tools.smart_fetch_url(
                    ["https://example.com"], format="skimmd"
                )
                await sf._drain_pending_writes()  # background write lands
                out2 = await tools.smart_fetch_url(
                    ["https://example.com"], format="markdown"
                )
                await sf._drain_pending_writes()
                assert _CountingSession.calls == 1, "second format must not refetch"
                assert "lorem ipsum" in out1 and "lorem ipsum" in out2
                assert len(sf._list_entries(root)) == 1, "one shared entry"
            finally:
                await tools._aclose()
                await sf._cache_sweep_stop()
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            sf._cache_root = real_root

    asyncio.run(scenario())


def test_wiring_user_toggle_off_disables_cache():
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        real_root = sf._cache_root
        root = _tmp_root()
        sf._cache_root = lambda tool_id=None: root
        try:
            tools = Tools()
            uv = Tools.UserValves(cache_enabled=False)
            try:
                for _ in range(2):
                    await tools.smart_fetch_url(
                        ["https://example.com"],
                        format="skimmd",
                        __user__={"valves": uv},
                    )
                await sf._drain_pending_writes()
                assert _CountingSession.calls == 2, "user toggle off ⇒ every call fetches"
                assert list(sf._list_entries(root)) == []
            finally:
                await tools._aclose()
                await sf._cache_sweep_stop()
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            sf._cache_root = real_root

    asyncio.run(scenario())


def test_wiring_freshness_zero_disables_cache():
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        real_root = sf._cache_root
        root = _tmp_root()
        sf._cache_root = lambda tool_id=None: root
        try:
            tools = Tools()
            tools.valves.cache_freshness_seconds = 0
            try:
                for _ in range(2):
                    await tools.smart_fetch_url(["https://example.com"], format="skimmd")
                await sf._drain_pending_writes()
                assert _CountingSession.calls == 2, "freshness <= 0 ⇒ cache off"
                assert list(sf._list_entries(root)) == []
            finally:
                await tools._aclose()
                await sf._cache_sweep_stop()
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            sf._cache_root = real_root

    asyncio.run(scenario())


# ── gated per-decision logging (step 6, CACHE.md §7) ────────────────────

def test_debug_logging_gated_and_token_free(caplog):
    """Off by default (nothing below warning); with cfg.debug on, one info
    line per decision — and never the query string."""
    async def scenario():
        ccr, original = _patch_curl(_CountingSession)
        try:
            tools = Tools()
            root = _tmp_root()
            url_hit = "https://example.com/path?secret=1&token=abc"
            url_miss = "https://example.com/other?secret=2"
            # warm the cache for url_hit with logging off (silent store)
            await tools._fetch_with_fingerprint(
                url_hit, "firefox", 5000, format="markdown", cache_cfg=_cfg(root)
            )
            await sf._drain_pending_writes()
            # debug on: url_hit is fresh → hit; url_miss → miss
            cfg_on = sf._CacheConfig(freshness_seconds=300.0, root=root, debug=True)
            await tools._fetch_with_fingerprint(
                url_hit, "firefox", 5000, format="markdown", cache_cfg=cfg_on
            )
            await tools._fetch_with_fingerprint(
                url_miss, "firefox", 5000, format="markdown", cache_cfg=cfg_on
            )
            await sf._drain_pending_writes()
        finally:
            import curl_cffi.requests as ccr

            ccr.AsyncSession = original
            await tools._aclose()

    with caplog.at_level(logging.INFO, logger="smart_fetch_url"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records]
    cache_lines = [l for l in lines if l.startswith("fetch_cache:")]
    assert len(cache_lines) == 2, f"exactly miss+hit, got: {cache_lines}"
    assert any("hit example.com/path" in l for l in cache_lines)
    assert any("miss example.com/other" in l for l in cache_lines)
    assert all("secret" not in l and "token" not in l for l in cache_lines), (
        "query strings must never reach the logs"
    )


def test_debug_logging_write_skip_reasons(caplog):
    async def scenario():
        for session_cls in (_Resp404Session,):
            ccr, original = _patch_curl(session_cls)
            try:
                tools = Tools()
                cfg_on = sf._CacheConfig(
                    freshness_seconds=300.0, root=_tmp_root(), debug=True
                )
                await tools._fetch_with_fingerprint(
                    "https://example.com/404", "firefox", 5000,
                    format="markdown", cache_cfg=cfg_on,
                )
                await sf._drain_pending_writes()
            finally:
                import curl_cffi.requests as ccr

                ccr.AsyncSession = original
                await tools._aclose()

    with caplog.at_level(logging.INFO, logger="smart_fetch_url"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "write-skip reason=http_404 example.com/404" in l for l in lines
    )
