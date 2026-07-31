"""Thread-pool behavior tests.

Verifies the documented behavior of _run_in_thread:

- A timed-out/cancelled call abandons the future, but the running thread
  keeps going to completion (concurrent.futures cannot kill threads) and
  the pool slot is reclaimed only when the work finishes.
- The _pool_pending_ops bookkeeping counter is restored in all paths.
"""

import asyncio
import threading
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `from helpers import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `from smart_fetch_url import ...`

import pytest

from helpers import cleanup_tools
from smart_fetch_url import Tools


def test_timeout_leaves_thread_running():
    async def scenario():
        tools = Tools()
        started = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            time.sleep(1.2)
            finished.set()
            return "done"

        try:
            with pytest.raises(asyncio.TimeoutError):
                await tools._run_in_thread(slow, timeout=0.15)

            assert started.is_set(), "worker must have started before the timeout"
            assert not finished.is_set(), (
                "right after the timeout the thread must STILL be running "
                "(concurrent.futures cannot kill it)"
            )

            # wait for the zombie to finish on its own
            deadline = time.monotonic() + 2.5
            while not finished.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert finished.is_set(), (
                "thread must keep running to completion even after the caller timed out"
            )

            # the bookkeeping counter must not leak
            assert tools._pool_pending_ops == 0, (
                "_pool_pending_ops must be restored in finally"
            )
        finally:
            await cleanup_tools(tools)

    asyncio.run(scenario())


def test_pending_ops_counter_restored_on_success():
    async def scenario():
        tools = Tools()
        try:
            val = await tools._run_in_thread(lambda: 42)
            assert val == 42
            assert tools._pool_pending_ops == 0
        finally:
            await cleanup_tools(tools)

    asyncio.run(scenario())
