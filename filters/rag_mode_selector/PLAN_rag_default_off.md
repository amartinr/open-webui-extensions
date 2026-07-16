# Implementation Plan — Filter A: `rag_default_off`

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
│   │   └── (no self.toggle — always active, no UI surface)
│   └── async def inlet(self, body, __chat_id__, __user__, __event_emitter__) -> dict
└── Module-level helper functions (outside the class)
    ├── _get_file_references(body, chat_meta) -> list[dict]
    ├── _persist_file_references(chat_id, files)
    ├── _has_full_files_marker(messages) -> bool
    ├── _build_full_files_block(files_content) -> str
    └── _inject_full_content(messages, file_refs, user) -> list[dict]
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

## 3. Inlet Flow — Step by Step

```
inlet(body)
│
├── 1. Extract files from body
│     ├── Read body["metadata"].get("files", [])  (frontend-provided references)
│     └── Read chat.meta["rag_mode_files"]         (persisted references from previous turns)
│     └── Merge: prefer body files for current turn, fall back to persisted
│
├── 2. Persist file references (if there are files AND __chat_id__)
│     └── Write to chat.meta["rag_mode_files"] = [{"id": "...", "name": "..."}, ...]
│     └── Use async DB: get Chat row → mutate meta → commit
│
├── 3. Clear files from body (suppress built-in RAG)
│     └── body["metadata"].pop("files", None)
│     └── body.pop("files", None)
│
├── 4. Check for [ FULL_FILES_START ] marker in system messages
│     ├── If FOUND → skip injection (content already in context, possibly surviving compaction)
│     └── If NOT FOUND →
│         ├── Resolve file references to actual content
│         │   └── For each ref: Files.get_file_by_id(id) → data.get("content", "")
│         ├── Build delimited block:
│         │   [ FULL_FILES_START ]
│         │   --- filename.ext ---
│         │   full content...
│         │   [ FULL_FILES_END ]
│         └── Insert as {"role": "system", "content": block} at position 0
│
├── 5. Set rag_mode flag for downstream consumers (Pipe)
│     └── body["metadata"]["rag_mode"] = "full_files"
│
└── return body
```

### 3.1 Detailed: Resolving file references (step 4)

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

**Solution:** Follow the pattern used by `Chats.update_chat_tags_by_id()` (see `models/chats.py` lines 519-520): fetch the Chat row, mutate `chat_item.meta` in-place, and commit.

```python
async def _persist_file_references(chat_id: str, files: list[dict]) -> None:
    """Write file references to chat.meta['rag_mode_files'].

    Uses the same in-place mutation pattern as update_chat_tags_by_id.
    """
    if not chat_id or not files:
        return

    from open_webui.models.chats import Chat
    from open_webui.internal.db import get_async_db_context

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return
        # Build the refs without the full data.content blob
        refs = [
            {"id": f.get("id"), "name": f.get("name", "unknown")}
            for f in files
            if f.get("id")
        ]
        chat_item.mata  # [sic — actual field is `meta`, but we use the column name]
        chat_item.meta = {
            **(chat_item.meta or {}),
            "rag_mode_files": refs,
        }
        await db.commit()
```

> **Note:** The field name in the Chat model is `meta` (column `meta` of type `JSON`). The DESIGN.md references it correctly as `chat.meta`.

### 4.1 Fallback: No chat_id

If `__chat_id__` is `None` (direct API call, temporary chat), skip persistence entirely. The filter still works for the current turn using only `body["metadata"]["files"]`.

---

## 5. Full Content Injection

### 5.1 Marker detection

```python
def _has_full_files_marker(messages: list[dict]) -> bool:
    """Check if [ FULL_FILES_START ] marker exists in any system message.

    Handles both string content and multimodal content (list of ContentParts).
    """
    marker = "[ FULL_FILES_START ]"
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and marker in content:
            return True
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and marker in block["text"]
                ):
                    return True
    return False
```

### 5.2 Block builder

```python
def _build_full_files_block(files_content: list[tuple[str, str]]) -> str:
    """Build the delimited full-files block.

    Args:
        files_content: list of (filename, content) tuples.

    Returns:
        A single string with markers and all file contents.
    """
    parts = ["[ FULL_FILES_START ]\n"]
    for filename, content in files_content:
        parts.append(f"\n--- {filename} ---\n{content}\n")
    parts.append("\n[ FULL_FILES_END ]")
    return "".join(parts)
```

### 5.3 Content resolver

```python
async def _inject_full_content(
    messages: list[dict],
    file_refs: list[dict],
    user: dict | None,
) -> list[dict]:
    """Inject full file content as a system message at position 0.

    Skips files that can't be found or have no content.
    """
    if not file_refs:
        return messages

    from open_webui.models.files import Files

    files_content: list[tuple[str, str]] = []
    for ref in file_refs:
        file_id = ref.get("id")
        if not file_id:
            continue
        try:
            file_model = await Files.get_file_by_id(file_id)
            if file_model is None:
                continue
            content = (file_model.data or {}).get("content", "")
            if content:
                files_content.append((ref.get("name", "unknown"), content))
        except Exception:
            continue

    if not files_content:
        return messages

    block = _build_full_files_block(files_content)
    messages.insert(0, {"role": "system", "content": block})
    return messages
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
- **Marker detection** (`_has_full_files_marker`): must scan both string and list formats.
- **Injection**: always uses plain string format (`{"role": "system", "content": block}`), which is compatible with all providers.

---

## 7. Edge Cases

| Case | Behaviour |
|---|---|
| **No files attached** | `metadata.files` is empty/absent → no persistence, no injection. `rag_mode` is still set. |
| **Direct API call, no `__chat_id__`** | No DB persistence. Refs come from `body["metadata"]["files"]` only. Subsequent turns lose refs. |
| **File not found in DB** | `Files.get_file_by_id()` returns `None` → skip that file, continue with others. |
| **Empty content** | `data["content"]` is `None` or `""` → skip that file. |
| **Marker `FULL_FILES_START` present** | Don't re-inject. Content is already in context from a previous turn. |
| **Multimodal content** | Scan for text blocks inside content lists. |
| **Context compaction** | If Open WebUI compacts messages and drops the injected block, the marker is lost. Next turn: marker absent → re-injects. |
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

This cleanup happens **after** every filter's inlet has run, so Filter B could not restore files — the cleanup is outside Filter B's control. By using `.pop()` inside the inlet, Filter B can later restore files because it runs second (priority 1) and can undo Filter A's changes before the files_handler runs.

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
from typing import Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()
        # No self.toggle — always active, invisible to users

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
| 1 | Upload file, send message | 1. Upload a PDF<br>2. Ask "What does this document say?" | Built-in RAG does NOT run. Full PDF content appears as a system message. |
| 2 | Follow-up message | 3. Ask "Summarise point 3" | Marker `[ FULL_FILES_START ]` already present → no re-injection. Content still available. |
| 3 | No files | Start a new chat with no attachments | Filter A is a no-op. No injection. |
| 4 | Multiple files | Upload 2 PDFs + 1 DOCX | All 3 documents are injected in the block. |
| 5 | File deleted from DB | Upload file, delete it from DB, send message | That file is skipped. Others are injected. |
| 6 | Context compaction | Long conversation triggers compaction | If block is dropped, marker is lost. Next turn re-injects. |

---

## 11. Implementation Order

1. Write `rag_default_off.py` with the complete `Filter` class and helpers
2. Review against this plan
3. Deploy to Open WebUI Admin Panel → Functions → Create Function → paste code
4. Assign to a test model with `priority = 0`
5. Run verification checklist (section 10)
6. If Filter B is planned next, proceed to `PLAN_rag_enable.md`
