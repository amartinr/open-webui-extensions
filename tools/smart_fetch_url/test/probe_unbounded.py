"""Probe: how many smart_fetch pool threads survive in a no-cache harness?

Usage: python probe_unbounded.py <repo_dir>

Phase A (post-fix): create 6 Tools instances, each with a live pool thread,
drop all references, gc — count threads again. The atexit pinning is gone
(weakref.finalize holds only a weakref), so instances are collected, the
executors lose their last strong reference, and idle workers exit on their
own (they hold only a weakref to the executor).
Phase B: explicit _aclose() on 4 live instances — count again (expect: 0).
"""
import asyncio
import gc
import sys
import threading
import time

REPO = sys.argv[1]
sys.path.insert(0, REPO)

import smart_fetch_url  # noqa: F401
from smart_fetch_url import Tools


def count_threads():
    return sum(1 for t in threading.enumerate() if t.name.startswith("smart_fetch"))


def make_instances(n):
    refs = []
    for _ in range(n):
        t = Tools()
        t._get_thread_pool()
        t._thread_pool.submit(lambda: time.sleep(0.5))
        refs.append(t)
    return refs


def line(label, value):
    print(f"{label}: {value}", flush=True)


def drain():
    """Poll until all smart_fetch threads exit (workers notice the dead executor)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and count_threads() > 0:
        time.sleep(0.1)
        gc.collect()


line("baseline", count_threads())

# ── Phase A: del + gc must reclaim instances and their pool threads ──
instances = make_instances(6)
time.sleep(0.7)
line("after_create", count_threads())

del instances
gc.collect()
drain()
line("after_del_gc_no_atexit", count_threads())

# ── Phase B: explicit _aclose() on live instances ─────────────────────
instances2 = make_instances(4)
time.sleep(0.7)


async def close_all(insts):
    for inst in insts:
        await inst._aclose()


asyncio.run(close_all(instances2))
time.sleep(0.3)
drain()
line("after_aclose", count_threads())
