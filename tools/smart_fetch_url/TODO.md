1.  [ ] **Extract batch/single dispatch from `smart_fetch_url`**
    The public method is ~200 lines with the batch loop (`async def fetch_one`),
    per-item error handling, and truncation logic all inline. Extract into
    `_fetch_batch()` and `_fetch_single()` so the public entry point is a
    short dispatcher and each path is independently readable/testable.

2.  [ ] **Replace `_RateLimiter` sliding-window lock with a token-bucket**
    The current `asyncio.Lock` forces all coroutines to queue sequentially
    even when no sleep is needed. At 10 req/s it's harmless, but the design
    doesn't scale if someone raises the rate. A token-bucket (or simply
    `asyncio.sleep` without a lock) would be more efficient.

3.  [ ] **Decouple tool logic from Open WebUI harness**
    `__user__` and `__event_emitter__` parameters are threaded through the
    entire pipeline. A thin `Context` adapter would isolate the core fetch
    logic from the framework, improve testability, and make the tool easier
    to port if needed. Low priority — debatable whether it's worth the
    churn for an Open WebUI tool.
