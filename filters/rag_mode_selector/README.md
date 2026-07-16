# RAG Mode Selector — Exploratory Filters

**Status: ⚠️ Exploratory — No viable solution found.**

This project explored the feasibility of implementing a per-conversation RAG toggle for Open WebUI through custom Filters, allowing users to switch between full-document context and semantic retrieval (RAG) on demand.

---

## What was built

### Filter A: `rag_default_off` (always-on, priority 0)
- Suppresses the built-in RAG pipeline by clearing `body["metadata"]["files"]`
- Persists file references to `chat.meta["rag_mode_files"]` across turns
- Injects full document content as a deterministic system message (preserving provider-side prefix caching)
- Maintains in-memory injection guard and file content cache for performance

### Filter B: `rag_enable` (toggleable, priority 1)
- Restores file references from `chat.meta["rag_mode_files"]` into `body["metadata"]["files"]`
- Removes the `rag_mode` flag for downstream consumers (e.g. the `agent_loop_guard` Pipe)
- Preserves the full content block injected by Filter A as a fallback

Both filters are correctly implemented and validated against the Open WebUI source code and architecture. They do what they are supposed to do.

---

## Why it didn't work

The premise of Filter B is that the built-in RAG pipeline (`chat_completion_files_handler`) would reliably produce semantic retrieval + reranking when file references are restored. In practice, **the RAG pipeline in Open WebUI does not function reliably** under several common configurations.

### Root causes

1. **Bug #25101 — `chat_completion_files_handler` runs without `function_calling != 'native'` guard**
   Unlike every other handler in the middleware (folder files, model knowledge, memory, web search, image generation, code interpreter), `chat_completion_files_handler` is not gated on native function calling mode. This causes redundant or broken context injection when native FC is enabled.
   - [https://github.com/open-webui/open-webui/issues/25101](https://github.com/open-webui/open-webui/issues/25101)

2. **PR #25150 — Attempted structural fix, closed unmerged**
   A comprehensive PR by Classic298 attempted to rewrite the file-context pipeline in native FC mode for cache-optimal behavior. Despite being technically sound, the PR was closed by its author with the note: *"what i built here works, but i am no longer confident whether this is needed as-is because 90-95% of this can be achieved through other means today in Open WebUI already."*
   - [https://github.com/open-webui/open-webui/pull/25150](https://github.com/open-webui/open-webui/pull/25150)

3. **`add_file_context()` injects `<file url="uuid"/>` tags in native FC mode**
   When native function calling is enabled, Open WebUI injects `<file>` tags referencing file UUIDs into user messages instead of actual content. The model cannot resolve these UUIDs to content without built-in tools, and the legacy RAG pipeline produces no usable context alongside these tags.

4. **Inconsistent retrieval across configurations**
   The RAG pipeline behaves differently depending on `function_calling` mode (legacy vs native), `RAG_SYSTEM_CONTEXT`, model capabilities, and whether `BYPASS_EMBEDDING_AND_RETRIEVAL` is set. The interaction between these settings is not well-coordinated, making it impossible to rely on the pipeline from an external Filter.

---

## What was learned

- Open WebUI's filter and pipe systems are well-designed for request interception and modification
- The two-filter architecture (always-on + toggleable) coordinated by priority is a clean pattern
- However, **Filters cannot fix broken core infrastructure** — they can only modify data that passes through them
- A reliable RAG toggle would require either:
  - Fixes to `chat_completion_files_handler` (guard against native FC) and the broader file-context pipeline
  - Or reimplementing the retrieval logic entirely within a Pipe (which is impractical — reimplementing RAG outside the core)

---

## Requirements

- Open WebUI 0.5.0 or later
- Python 3.11+

## Installation

### Filter A — `rag_default_off` (always-on)
1. Admin Panel → Functions → Create Function → Type: Filter
2. Paste the contents of `rag_default_off.py`
3. Set `priority = 0` in Valves
4. Assign to the model under Model Settings → Filters

### Filter B — `rag_enable` (toggleable)
1. Admin Panel → Functions → Create Function → Type: Filter
2. Paste the contents of `rag_enable.py`
3. Set `priority = 1` in Valves
4. Ensure `self.toggle = True` (appears as a chip in the chat UI)
5. Assign to the same model under Model Settings → Filters
6. Configure to start OFF by default (Default Filters)

---

## References

- [Issue #25101 — `chat_completion_files_handler` missing native FC guard](https://github.com/open-webui/open-webui/issues/25101)
- [Issue #22181 — RAG not triggered for custom models](https://github.com/open-webui/open-webui/issues/22181)
- [Issue #24314 — `chat_completion_files_handler` AttributeError](https://github.com/open-webui/open-webui/issues/24314)
- [PR #25150 — Cache-optimal file context (closed, unmerged)](https://github.com/open-webui/open-webui/pull/25150)
- [Open WebUI Backend Processing Pipeline (DeepWiki)](https://deepwiki.com/open-webui/open-webui/6-backend-processing-pipeline)

---

## License

MIT
