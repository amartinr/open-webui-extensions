"""Config-surface tests for the on-disk fetch cache (see CACHE.md).

Step-1 scope: the constants and valve defaults exist and have the agreed
values. The cache is not wired to the fetch path yet, so there is no
behavioural change to test here — those come with the insertion step.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smart_fetch_url as sf
from smart_fetch_url import Tools


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


# ── key derivation (CACHE.md §D4) ────────────────────────────────────────

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
    assert k(url, "firefox", None, "markdown") == k(url, "firefox", None, "markdown")
    # sha256 hex, 64 chars
    assert len(k(url, "firefox", None)) == 64
    # URL case-insensitivity via normalization
    assert k("https://EXAMPLE.com/path?q=1", "firefox", None) == k(url, "firefox", None)
    # sensitive to browser, proxy, accept group
    assert k(url, "chrome", None) != k(url, "firefox", None)
    assert k(url, "firefox", "http://p:1") != k(url, "firefox", None)
    assert k(url, "firefox", None, "json") != k(url, "firefox", None, "markdown")
    assert k(url, "firefox", None, "raw") != k(url, "firefox", None, "markdown")
    # html-family formats share the entry
    assert k(url, "firefox", None, "skimmd") == k(url, "firefox", None, "markdown")
    # the plaintext URL never appears in the key
    assert url not in k(url, "firefox", None)
    assert "Example.com" not in k(url, "firefox", None)
