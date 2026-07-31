"""curl_cffi session lifecycle tests.

Verified against curl_cffi 0.15.0 source: Session/AsyncSession have NO
__del__ (nor does AsyncCurl — only Curl/CurlMime do), so sessions are
closed only explicitly. The API guard below protects that assumption;
the real-network tests verify the session stays open across fetches and
is closed by _aclose().
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `from helpers import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `from smart_fetch_url import ...`

import pytest

from helpers import cleanup_tools, curl_cffi_installed
from smart_fetch_url import Tools

REAL = curl_cffi_installed()


@pytest.mark.skipif(not REAL, reason="curl_cffi not installed")
def test_real_session_class_has_no_destructor():
    """Guard against upstream API changes: sessions must be closed explicitly."""
    import curl_cffi.aio as cca
    import curl_cffi.requests as ccr

    assert not hasattr(ccr.AsyncSession, "__del__"), (
        "curl_cffi AsyncSession has no __del__; sessions are only closed explicitly"
    )
    assert not hasattr(cca.AsyncCurl, "__del__"), (
        "AsyncCurl has no __del__ either; its curl_multi handle is released "
        "only by the explicit async close()"
    )


@pytest.mark.skipif(not REAL, reason="curl_cffi not installed")
def test_real_session_remains_open_until_aclose():
    """Real curl_cffi session: open after a fetch, closed by _aclose()."""
    import curl_cffi.requests as ccr

    async def scenario():
        tools = Tools()
        try:
            res = await tools._fetch_with_curl_cffi(
                "https://example.com", "firefox", {}, 10000
            )
            assert res.status_code == 200
            sess = tools._curl_sessions["firefox::direct"]
            assert isinstance(sess, ccr.AsyncSession)
            assert getattr(sess, "_closed", False) is False, (
                "real curl_cffi session must remain open after the fetch returns"
            )
            await tools._aclose()
            assert getattr(sess, "_closed", False) is True, (
                "_aclose() must close the real session"
            )
        finally:
            await cleanup_tools(tools)

    asyncio.run(scenario())


@pytest.mark.skipif(not REAL, reason="curl_cffi not installed")
def test_real_fetch_reuses_session_and_aclose_closes_it():
    """End-to-end via the public API: session reused, then closed by _aclose()."""
    async def scenario():
        tools = Tools()
        try:
            out = await tools.smart_fetch_url(
                ["https://example.com"],
                format="skimmd",
            )
            assert isinstance(out, str) and len(out) > 0
            assert "Example Domain" in out

            sess = tools._curl_sessions.get("firefox::direct")
            assert sess is not None, "a curl session must exist after a real fetch"
            assert getattr(sess, "_closed", False) is False, (
                "session must remain open after the fetch returns"
            )

            # second fetch reuses the same open session
            first = tools._curl_sessions["firefox::direct"]
            await tools.smart_fetch_url(["https://example.com"], format="skimmd")
            assert tools._curl_sessions["firefox::direct"] is first
            assert getattr(first, "_closed", False) is False

            # explicit teardown closes it
            await tools._aclose()
            assert getattr(first, "_closed", False) is True
        finally:
            await cleanup_tools(tools)

    asyncio.run(scenario())
