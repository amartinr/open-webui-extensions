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
