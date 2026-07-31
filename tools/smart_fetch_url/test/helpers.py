"""Shared helpers for the smart_fetch_url test suite.

Consolidated from the diagnostic suite that previously lived in
dist/owx-tests: recording fakes for curl_cffi sessions, teardown
primitives and environment checks.
"""

import sys
from pathlib import Path

# Make both the repo root (for `from smart_fetch_url import ...`) and this
# directory (for `from helpers import ...`) importable regardless of how
# pytest imports the test package.
TOOL_DIR = Path(__file__).resolve().parent.parent
for path in (TOOL_DIR, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

REPO_DIR = TOOL_DIR


class FakeResponse:
    """Stand-in for a curl_cffi response with the fields the tool reads."""

    headers = {"content-type": "text/html; charset=utf-8"}
    url = "https://example.com/final"
    status_code = 200
    content = b"<html><body><p>hello world</p></body></html>"
    text = "<html><body><p>hello world</p></body></html>"


class FakeAsyncSession:
    """Recording fake for curl_cffi ``AsyncSession``.

    Every created instance is appended to ``created`` so tests can assert
    how many sessions were built and with what kwargs; ``close()`` sets
    ``closed`` so tests can verify explicit session teardown.
    """

    created: list["FakeAsyncSession"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeAsyncSession.created.append(self)

    async def get(self, *args, **kwargs):
        return FakeResponse()

    async def close(self):
        self.closed = True


def install_fake_sessions():
    """Monkeypatch ``curl_cffi.requests.AsyncSession`` with the fake.

    The tool imports ``AsyncSession`` lazily inside
    ``_fetch_with_curl_cffi``, so patching the module attribute is enough.

    Returns ``(ccr_module, original_class)`` for ``restore_sessions()``.
    """
    import curl_cffi.requests as ccr

    original = ccr.AsyncSession
    FakeAsyncSession.created = []
    ccr.AsyncSession = FakeAsyncSession
    return ccr, original


def restore_sessions(ccr, original):
    """Put the real ``AsyncSession`` back after ``install_fake_sessions``."""
    ccr.AsyncSession = original


def curl_cffi_installed() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


async def cleanup_tools(tools):
    """Full async teardown of a Tools instance (sessions + thread pool).

    Uses the tool's own ``_aclose()`` primitive; falls back to ``_close()``
    for pre-fix code under test.
    """
    if hasattr(tools, "_aclose"):
        await tools._aclose()
    else:
        tools._close()
