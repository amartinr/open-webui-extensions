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


def test_async_read_fresh_and_stale():
    async def scenario():
        root = _tmp_root()
        key = "k" * 64
        await sf._cache_store(key, root, _entry(created=sf._cache_now()))
        assert await sf._cache_read_fresh(key, root, 300) is not None
        # backdate createdAt beyond the window → treated as a miss
        await sf._cache_store(key, root, _entry(created=sf._cache_now() - 999))
        assert await sf._cache_read_fresh(key, root, 300) is None
        # missing key
        assert await sf._cache_read_fresh("x" * 64, root, 300) is None

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
