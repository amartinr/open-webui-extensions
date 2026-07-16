# Implementation Plan - Filter A: `rag_default_off`

**File:** `filters/rag_mode_selector/rag_default_off.py`

**Purpose:** Always-on filter (no `self.toggle`, no UI chip), priority 0. Runs on every request and transforms any conversation with attached files into `full_files` mode: suppresses the built-in RAG pipeline, persists file references to `chat.meta`, and injects the full document content as a system message.

Filter A is fully autonomous. It does not need Filter B to exist. When Filter A is active and Filter B is absent, every conversation with files gets full-document context permanently.

---

## 1. File and Class Structure

```
rag_default_off.py
├── Frontmatter (YAML in docstring)
│   ├── title: RAG Default Off
│   ├── author: ...
│   ├── version: 1.0.0
│   ├── required_open_webui_version: 0.5.0+
│   └── description: Suppresses built-in RAG and injects full file content
├── Imports
├── class Filter
│   ├── class Valves(BaseModel)
│   │   └── priority: int = 0
│   ├── def __init__(self)
│   │   ├── self.valves = self.Valves()
│   │   └── (no self.toggle - always active, no UI surface)
│   └── async def inlet(self, body, __chat_id__, __user__, __event_emitter__) -> dict
└── Module-level helper functions (outside the class)
    ├── _normalize_refs(files) -> list[dict]
    ├── _mutate_chat_meta(chat_id, **updates)
    ├── _persist_file_references(chat_id, refs)
    ├── _read_persisted_refs(chat_id) -> list[dict] | None
    ├── _read_injection_record(chat_id) -> dict | None
    ├── _persist_injection_record(chat_id, record)
    ├── _find_injection_marker(messages, record) -> bool
    ├── _build_full_content_block(content_parts, injection_id) -> str
    ├── _resolve_and_inject(messages, file_refs) -> tuple[list[dict], dict]
    └── _safe_emit(event_emitter, event_type, data)
```

---

## 2. Inlet Signature

```python
async def inlet(
    self,
    body: dict,
    __chat_id__: str | None = None,
    __user__: dict | None = None,
    __event_emitter__: Callable | None = None,
) -> dict:
```

- **`body`**: The full chat completion payload that will be sent to the LLM. Contains `messages`, `metadata`, `files`, `model`, etc.
- **`__chat_id__`**: Conversation UUID from `extra_params`. Can be `None` on direct API calls without a chat context.
- **`__user__`**: Dict with user data (`id`, `email`, `role`). Needed to verify file ownership if required.
- **`__event_emitter__`**: For sending status events to the frontend (optional but good practice).

---

## 3. Inlet Flow - Step by Step

```
inlet(body)
│
├── 1. Extract files from body
│     ├── Read body["metadata"].get("files", [])  (frontend-provided references)
│     └── Read chat.meta["rag_mode_files"]         (persisted references from previous turns)
│     └── Prefer body files for current turn, fall back to persisted
│
├── 2. Persist file references (if there are files AND __chat_id__)
│     └── Write to chat.meta["rag_mode_files"] = [{"id": "...", "name": "..."}, ...]
│     └── Use async DB: get Chat ORM row → mutate meta → commit
│         (via _mutate_chat_meta helper)
│
├── 3. Clear files from body (suppress built-in RAG)
│     └── body["metadata"].pop("files", None)
│     └── body.pop("files", None)
│
├── 4. Check whether content has already been injected
│     ├── Read chat.meta["full_files_injected"] (the injection record)
│     ├── If record exists:
│     │   └── Scan messages for [injection:<record.id>] marker
│     │       (via _find_injection_marker)
│     │   ├── If marker FOUND → skip injection (content still in context)
│     │   └── If marker ABSENT → content was dropped (compaction),
│     │       must re-inject (proceed to injection)
│     └── If no record → first injection, proceed
│
├── 5. Inject full content (if needed)
│     └── For each file ref: Files.get_file_by_id(id) → data.get("content", "")
│     └── Build block with embedded [injection:<uuid>] marker
│         (via _resolve_and_inject)
│     └── Insert as {"role": "system", "content": block} at position 0
│     └── Persist injection record to chat.meta["full_files_injected"]
│         = {"id": str(uuid4), "at": int(timestamp), "files": [...]}
│
├── 6. Set rag_mode flag for downstream consumers (Pipe)
│     └── body["metadata"]["rag_mode"] = "full_files"
│
└── return body
```

### 3.1 Resolving file references

The filter needs files from **two sources**:

| Source | When it has data | Example |
|---|---|---|
| `body["metadata"]["files"]` | First turn after upload | `[{"id": "abc", "name": "doc.pdf"}]` |
| `chat.meta["rag_mode_files"]` | Subsequent turns | `[{"id": "abc", "name": "doc.pdf"}]` |

On subsequent turns, the frontend does **not** re-send file references (confirmed by Trace 2 in DESIGN.md). So the flow is:

```
if body["metadata"]["files"] is non-empty:
    refs = body["metadata"]["files"]
    persist them to chat.meta (step 2)
elif chat.meta["rag_mode_files"] is non-empty:
    refs = chat.meta["rag_mode_files"]
else:
    refs = []  → nothing to inject, skip step 4
```

---

## 4. Persisting to `chat.meta`

**Problem identified during code review:** There is no `Chats.update_chat_meta()` method. The existing `update_chat_by_id()` only updates `chat` and `title`, not `meta`.

**Solution:** Use a generic `_mutate_chat_meta(chat_id, **updates)` that follows the pattern from `Chats.update_chat_tags_by_id()` (see `models/chats.py` lines 519-520): fetch the Chat ORM row, mutate `chat_item.meta` in-place with `**updates`, and commit.

```python
async def _mutate_chat_meta(chat_id: str | None, **updates) -> None:
    """Atomically merge updates into chat.meta."""
    if not chat_id or not updates:
        return

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return
        chat_item.meta = {
            **(chat_item.meta or {}),
            **updates,
        }
        await db.commit()
```

Both `_persist_file_references` and `_persist_injection_record` delegate to this helper.

### 4.1 Fallback: No chat_id

If `__chat_id__` is `None` (direct API call, temporary chat), skip persistence entirely. The filter still works for the current turn using only `body["metadata"]["files"]`.

---

## 5. Full Content Injection

### 5.1 Injection markers

No magic strings like `[ FULL_FILES_START ]` / `[ FULL_FILES_END ]`. Instead, Filter A uses a **unique injection ID** per injection, embedded directly in the content:

```
[injection:a1b2c3d4-e5f6-...]
--- document.pdf ---
Full content of the first file...

--- notes.docx ---
Full content of the second file...
```

- The ID (`a1b2c3d4-e5f6-...`) is a `uuid4()` generated per injection.
- Filter B locates the block by reading the injection id from `chat.meta["full_files_injected"]` and scanning messages for `[injection:<id>]`.

### 5.2 Injection record

Each injection creates a record stored in `chat.meta["full_files_injected"]`:

```json
{
  "id": "a1b2c3d4-e5f6-...",
  "at": 1746000000,
  "files": [{"id": "abc", "name": "doc.pdf"}, ...]
}
```

### 5.3 Re-injection detection (dual check)

On every turn, Filter A:

1. Reads `chat.meta["full_files_injected"]` - if absent, no injection has happened yet.
2. If the record exists, scans messages for `[injection:<record.id>]` via `_find_injection_marker()`.
   - **Marker found** → content still in context → skip injection.
   - **Marker absent** → content was dropped (compaction, manual message edit) → re-inject with a new id.

This dual check is robust against context compaction, which drops message content but leaves `chat.meta` intact.

### 5.4 Block builder

```python
def _build_full_content_block(
    content_parts: list[tuple[str, str]], injection_id: str
) -> str:
    """Build the full content block with an embedded injection ID."""
    sections = []
    for filename, content in content_parts:
        sections.append(f"--- {filename} ---\n{content}")
    body = "\n\n".join(sections)
    return f"[injection:{injection_id}]\n{body}"
```

### 5.5 Content resolver

```python
async def _resolve_and_inject(
    messages: list[dict],
    file_refs: list[dict],
) -> tuple[list[dict], dict]:
    """Resolve file contents from the DB and inject as a system message.

    Returns (updated_messages, injection_record).
    """
    if not file_refs:
        return messages, {}

    from open_webui.models.files import Files

    content_parts: list[tuple[str, str]] = []
    for ref in file_refs:
        file_id = ref.get("id")
        if not file_id:
            continue
        try:
            file_model = await Files.get_file_by_id(file_id)
            if file_model is None:
                log.warning("rag_default_off: file not found in DB - %s", file_id)
                continue
            raw = (file_model.data or {}).get("content", "")
            if raw:
                content_parts.append((ref.get("name", "unknown"), raw))
        except Exception:
            log.exception("rag_default_off: error reading file %s", file_id)
            continue

    if not content_parts:
        return messages, {}

    injection_id = str(uuid4())
    block = _build_full_content_block(content_parts, injection_id)
    messages.insert(0, {"role": "system", "content": block})

    record = {
        "id": injection_id,
        "at": int(time.time()),
        "files": file_refs,
    }
    return messages, record
```

---

## 6. Multimodal Message Handling

Some providers (Gemini, Claude) use `content` as a list of `ContentPart` objects instead of a plain string:

```python
# Plain string (OpenAI-style)
{"role": "user", "content": "Hello"}

# Multimodal list
{"role": "user", "content": [
    {"type": "text", "text": "What's in this image?"},
    {"type": "image_url", "image_url": {"url": "data:image/..."}}
]}
```

Requirements:
- **Marker detection** (`_find_injection_marker`): must scan both string and list formats in system messages.
- **Injection**: always uses plain string format (`{"role": "system", "content": block}`), which is compatible with all providers.

---

## 7. Edge Cases

| Case | Behaviour |
|---|---|
| **No files attached** | `metadata.files` is empty/absent → no persistence, no injection. `rag_mode` is still set. |
| **Direct API call, no `__chat_id__`** | No DB persistence. Refs come from `body["metadata"]["files"]` only. Subsequent turns lose refs. |
| **File not found in DB** | `Files.get_file_by_id()` returns `None` → skip that file, continue with others. |
| **Empty content** | `data["content"]` is `None` or `""` → skip that file. |
| **Injection record exists + marker found** | Content is in context → skip re-injection. |
| **Injection record exists + marker missing** | Content was dropped (compaction) → re-inject with new id. |
| **Multimodal content** | Scan for text blocks inside content lists (marker detection). |
| **Context compaction** | If Open WebUI compacts messages and drops the injected block, the record survives but the marker is lost. Next turn: re-injects. |
| **`Files.get_file_by_id` raises exception** | Catch, log, skip that file, continue. |

---

## 8. Why Not `file_handler`

Explicit confirmation: Filter A does **NOT** declare `file_handler = True` at module level. Instead, it suppresses RAG by clearing `body["metadata"]["files"]` and `body["files"]` inside the inlet.

If we declared `file_handler = True`, the backend would strip files **after all inlets return** (see `filter.py` lines 130-136 in the source):

```python
# Handle file cleanup for inlet
if skip_files:
    if 'files' in form_data.get('metadata', {}):
        del form_data['metadata']['files']
    if 'files' in form_data:
        del form_data['files']
```

This cleanup happens **after** every filter's inlet has run, so Filter B could not restore files - the cleanup is outside Filter B's control. By using `.pop()` inside the inlet, Filter B can later restore files because it runs second (priority 1) and can undo Filter A's changes before the files_handler runs.

---

## 9. Dependencies and Imports

```python
"""
title: RAG Default Off
author: your_name
version: 1.0.0
required_open_webui_version: 0.5.0
description: >
    Always-on filter that suppresses built-in RAG and injects
    full document content into context. Part of the RAG Mode Selector system.
"""

import logging
import time
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()
        # No self.toggle - always active, invisible to users

    async def inlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __user__: dict | None = None,
        __event_emitter__: Callable | None = None,
    ) -> dict:
        # ... implementation ...
```

Open WebUI model imports are imported lazily inside async functions (not at module top level) to avoid import-order issues:

```python
from open_webui.models.files import Files          # get_file_by_id
from open_webui.models.chats import Chat            # ORM model for db.get
from open_webui.internal.db import get_async_db_context  # async session
```

---

## 10. Manual Verification Checklist

| # | Scenario | Steps | Expected result |
|---|---|---|---|
| 1 | Upload file, send message | 1. Upload a PDF<br>2. Ask "What does this document say?" | Built-in RAG does NOT run. Full PDF content appears as a system message with `[injection:<uuid>]` marker. |
| 2 | Follow-up message | 3. Ask "Summarise point 3" | Injection record found + marker present → no re-injection. Content still available. |
| 3 | No files | Start a new chat with no attachments | Filter A is a no-op. No injection. |
| 4 | Multiple files | Upload 2 PDFs + 1 DOCX | All 3 documents are injected in the block. |
| 5 | File deleted from DB | Upload file, delete it from DB, send message | That file is skipped. Others are injected. |
| 6 | Context compaction | Long conversation triggers compaction | Record survives but marker is absent from messages → re-injects with new id. |

---

## 11. Implementation Order

1. Write `rag_default_off.py` with the complete `Filter` class and helpers
2. Review against this plan
3. Deploy to Open WebUI Admin Panel → Functions → Create Function → paste code
4. Assign to a test model with `priority = 0`
5. Run verification checklist (section 10)
6. If Filter B is planned next, proceed to `PLAN_rag_enable.md`
