# agent_loop_guard — tests

Test suite of the `agent_loop_guard` pipe (Open WebUI function).

Each test lives with its component (monorepo convention): the pipe's tests
stay in this directory; the tools (`smart_fetch_url`, …) keep their own test
directories. The LiteLLM probes (which separated Bifrost-specific behavior
from the gateway-agnostic DeepSeek contract) live in `probes/litellm/`.

## Inventory

| File | Kind | What it tests | Needs a live… |
|---|---|---|---|
| `test_attached_files_cleanup.py` | unit (pytest) | Cache-safe `<attached_files>` cleanup: deterministic, idempotent, byte-stable history prefix | nothing (module-only) |
| `test_reasoning_forcing.py` | unit (pytest) | DeepSeek-contract forcing: `reasoning_content` added to assistant messages once tool-calling is in scope; user/system/tool untouched; deterministic | nothing (module-only) |

## Unit tests (pytest, no network)

```bash
cd /srv/pi/open-webui-extensions
python3 -m pytest pipes/agent_loop_guard/tests/ -q
# whole repo battery:
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

The modules import `agent_loop_guard` via a `sys.path` insert relative to the
test file — no Open WebUI install required (its imports are lazy inside the
pipe class only).

## Integration probes (node, need live services)

See `probes/litellm/README.md` for the LiteLLM A/B probes:

- `01_toolcall_ab.js` — tool-call continuation replayed WITH vs WITHOUT
  `reasoning_content` (the latter is how Open WebUI reconstructs it); verdict
  backed by LiteLLM's own warning (`transformation.py`) that a missing field
  injects a blank reasoning chain.
- `02_thinking_disabled.js` — whether `thinking: {"type": "disabled"}` (sent by
  Open WebUI on server-side tool-call continuations) kills reasoning through
  LiteLLM.
