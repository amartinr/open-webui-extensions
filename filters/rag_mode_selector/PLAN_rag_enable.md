# Implementation Plan — Filter B: `rag_enable`

**File:** `filters/rag_mode_selector/rag_enable.py`

**Purpose:** Toggleable filter (`self.toggle = True`, priority 1) that appears as a clickable chip in the chat UI. When enabled by the user, it restores standard RAG behaviour: repopulates file references from `chat.meta["rag_mode_files"]` into `body["metadata"]["files"]`, removes the `rag_mode` flag, and strips the full-content block from messages.

When disabled (chip off), the filter is a no-op — Filter A's decisions stand, and `full_files` mode remains active.

Filter B runs **after** Filter A (priority 1 > priority 0), so it receives `body` in the state Filter A left it. It reverses what it needs to.

---

## 1. File and Class Structure

```
rag_enable.py
├── Frontmatter (YAML in docstring)
│   ├── title: RAG Enable
│   ├── author: ...
│   ├── version: 1.0.0
│   ├── required_open_webui_version: 0.5.0+
│   └── description: Toggleable filter that restores standard RAG. Disable for full_files mode.
├── Imports
├── class Filter
│   ├── class Valves(BaseModel)
│   │   └── priority: int = 1
│   ├── def __init__(self)
│   │   ├── self.valves = self.Valves()
│   │   ├── self.toggle = True     ← user-controllable, appears as a chip
│   │   └── self.icon = "..."      ← URL for the chip icon
│   └── async def inlet(self, body, __chat_id__, __metadata__, __user__) -> dict
└── Module-level helper functions (outside the class)
    ├── _read_chat_meta_value(chat_id, key) -> any
    ├── _restore_file_references(chat_id) -> list[dict] | None
    ├── _read_injection_id(chat_id) -> str | None
    └── _remove_injection_block(messages, injection_id) -> list[dict]
```

---

## 2. Inlet Signature

```python
async def inlet(
    self,
    body: dict,
    __chat_id__: str | None = None,
    __metadata__: dict | None = None,
    __user__: dict | None = None,
) -> dict:
```

- **`body`**: The chat completion payload as left by Filter A. Contains the full-content block in `messages`, `rag_mode = "full_files"` in `metadata`, and empty/absent `files`.
- **`__chat_id__`**: Conversation UUID. Needed to read `chat.meta` keys.
- **`__metadata__`**: Full metadata dict. Can be used as fallback for `chat_id`.
- **`__user__`**: User dict for file permission checks if needed.

> **Note on toggle behaviour:** When the user has the chip **OFF** (RAG disabled, full_files mode), this filter's `inlet()` is **not called at all**. The gating happens at dispatch time (`filter.py` lines 37-40). So there is no need for an early return or `if not self.toggle` check inside `inlet()` — when this method runs, the chip is always ON.

---

## 3. Inlet Flow — Step by Step

```
inlet(body)   ← ONLY runs when user has the chip ON
│
├── 1. Restore file references from chat.meta
│     ├── Read chat.meta["rag_mode_files"]
│     │   └── (populated by Filter A on the first turn with files)
│     ├── If empty or None → nothing to restore, skip
│     └── body["metadata"]["files"] ← [{"id": "...", "name": "..."}, ...]
│
├── 2. Remove the rag_mode flag
│     └── body["metadata"].pop("rag_mode", None)
│     └── (This signals the Pipe to NOT filter KB tools)
│
├── 3. Remove the full content block from messages
│     ├── Read chat.meta["full_files_injected"] → {"id": "...", "at": ..., "files": [...]}
│     │   └── (populated by Filter A when it injected the content)
│     ├── If record exists:
│     │   └── Scan all system messages for [injection:<record.id>]
│     │   └── Remove the entire matching message from the list
│     └── If no record or marker not found: no-op
│     └── (Avoids confusing the LLM with both full content and RAG chunks)
│
└── return body
```

---

## 4. Restoring File References

Filter A persists references as `chat.meta["rag_mode_files"]` in this format:

```json
[{"id": "abc-123", "name": "document.pdf"}, {"id": "def-456", "name": "notes.docx"}]
```

Filter B reads them and puts them back into `body["metadata"]["files"]`, which is exactly what `chat_completion_files_handler` (`middleware.py` line 1767) checks:

```python
if files := body.get('metadata', {}).get('files', None):
```

```python
async def _restore_file_references(chat_id: str) -> list[dict] | None:
    """Read chat.meta['rag_mode_files'] and return the refs list.

    Returns None if the chat doesn't exist or the key is absent.
    """
    if not chat_id:
        return None

    from open_webui.models.chats import Chat
    from open_webui.internal.db import get_async_db_context

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return None
        refs = (chat_item.meta or {}).get("rag_mode_files")
        return refs if refs else None
```

### 4.1 Format compatibility

The references stored by Filter A match the format the frontend sends and that `chat_completion_files_handler` expects:

```python
# Filter A persists:
[{"id": "abc", "name": "doc.pdf"}]

# chat_completion_files_handler reads body["metadata"]["files"]:
# Each item must have "id" (file UUID)
# "name" is optional but used for display

# Since we are restoring manually uploaded files (not collections),
# only "id" and "name" are needed.
```

> **Important:** If Filter B restores files while Filter A's `file_handler` is NOT set, the built-in RAG pipeline will process them normally (chunking, embedding, retrieval, reranking). This is exactly what we want in RAG mode.

---

## 5. Removing the Full Content Block

Filter A embeds an injection marker `[injection:<uuid>]` at the start of the system message and stores the id in `chat.meta["full_files_injected"]`:

```json
{"id": "a1b2c3d4-e5f6-...", "at": 1746000000, "files": [...]}
```

Filter B:
1. Reads the injection id from `chat.meta["full_files_injected"]`.
2. Scans system messages for `[injection:<id>]`.
3. Removes the entire system message that contains it.

```python
def _remove_injection_block(messages: list[dict], injection_id: str) -> list[dict]:
    """Remove the system message containing [injection:<id>].

    Args:
        messages: Current message list.
        injection_id: The UUID to search for.

    Returns:
        A new list with the matching message removed.
    """
    if not injection_id:
        return messages

    marker = f"[injection:{injection_id}]"
    filtered = []
    for msg in messages:
        if msg.get("role") != "system":
            filtered.append(msg)
            continue
        content = msg.get("content", "")
        found = False
        if isinstance(content, str) and marker in content:
            found = True
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and marker in block["text"]
                ):
                    found = True
                    break
        if not found:
            filtered.append(msg)
        # else: drop this message entirely
    return filtered
```

### 5.1 Why remove the whole message, not just the block text?

The full content block is always injected as its own system message at position 0:

```python
messages.insert(0, {"role": "system", "content": block})
```

So removing the entire message that contains the marker is correct. We never inject the marker inline inside another system message.

---

## 6. Edge Cases

| Case | Behaviour |
|---|---|
| **`chat.meta["rag_mode_files"]` is empty** | Nothing to restore. No files are put back. RAG will run on nothing → effectively same as full_files. |
| **`chat.meta["full_files_injected"]` is absent** | Nothing to remove from messages. Filter B still restores files and removes rag_mode flag. |
| **Injection id found but marker not in messages** | No-op for block removal (message may have been removed by compaction). |
| **No `__chat_id__`** (API direct call) | Can't read `chat.meta`. No file restoration, no block removal. Filter B becomes a no-op for everything except removing the (absent) rag_mode flag. |
| **Chat doesn't exist in DB** | `db.get(Chat, chat_id)` returns None → no file restoration, no injection id. |
| **Messages emptied after block removal** | If the only message was the full-content block, the resulting messages list would be empty. The RAG pipeline would still run because files are restored, but there's no user prompt. This is the same behaviour as if a user sent an empty message. |
| **Multimodal content block** | `_remove_injection_block` scans text parts inside content lists. |
| **Filter B enabled mid-conversation** (toggle ON after several turns) | Works. Filter B reads `chat.meta["rag_mode_files"]` (persisted by Filter A on an earlier turn), restores files, reads injection id, removes the block. On the next turn, `chat_completion_files_handler` sees files and runs RAG. |

---

## 7. Interaction with Filter A

The two filters coordinate through **three** keys — no shared references, no internal state:

| Key | Written by | Read by |
|---|---|---|
| `chat.meta["rag_mode_files"]` | Filter A (step 2) | Filter B (step 1) |
| `chat.meta["full_files_injected"]` | Filter A (step 5) | Filter B (step 3) |
| `body["metadata"]["rag_mode"]` | Filter A (step 6) | Filter B removes it (step 2); Pipe reads it |

Execution order (thanks to priority):

```
Request arrives
  └─> compact_messages_for_request
  └─> Filter A inlet (prio=0)
  │     Persists refs, clears files, injects content,
  │     stores injection record, sets rag_mode flag
  └─> Filter B inlet (prio=1) — ONLY if chip is ON
  │     Restores files, removes rag_mode flag,
  │     reads injection id, removes content block from messages
  └─> chat_completion_files_handler
  │     If body["metadata"]["files"] is non-empty → runs RAG
  │     If empty → skipped
  └─> Pipe (agent_loop_guard)
        Reads rag_mode flag to filter KB tools
```

---

## 8. Configuration (Admin Panel)

| Setting | Value |
|---|---|
| **Type** | Filter |
| **`self.toggle`** | `True` (appears as chip in UI) |
| **`priority`** | `1` (runs after Filter A's priority 0) |
| **Model Filter assignment** | Same model as Filter A |
| **Default Filters** | `rag_enable` should start **OFF** by default (full_files is the default mode) |

### 8.1 Admin steps

1. **Admin Panel → Functions → Create Function**
2. Type: **Filter**
3. Paste `rag_enable.py` contents
4. Ensure `self.toggle = True` (user-controllable chip)
5. Set `priority = 1` in `Valves`
6. Save

7. **Workspace → Models → [your model] → Filters**
8. Add both `rag_default_off` and `rag_enable` to the model
9. **Default Filters**: ensure `rag_enable` is **unchecked** (starts OFF)
10. Save

---

## 9. Dependencies and Imports

```python
"""
title: RAG Enable
author: your_name
version: 1.0.0
required_open_webui_version: 0.5.0
description: >
    Toggleable filter that restores standard RAG when enabled.
    Part of the RAG Mode Selector system. Disable this chip for
    full-document context mode.
"""

import logging
from typing import Callable, Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        priority: int = 1

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True
        self.icon = "https://example.com/icons/rag-toggle.svg"  # TODO: replace with real URL

    async def inlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __metadata__: dict | None = None,
        __user__: dict | None = None,
    ) -> dict:
        # ... implementation ...
```

Open WebUI model imports (lazy, inside async functions):

```python
from open_webui.models.chats import Chat
from open_webui.internal.db import get_async_db_context
```

---

## 10. Manual Verification Checklist

| # | Scenario | Prerequisites | Steps | Expected result |
|---|---|---|---|---|
| 1 | RAG mode — first turn | Filter A + B assigned to model, Filter B chip ON | 1. Upload a PDF<br>2. Ask a question | RAG runs. Retrieval chunks appear. Full content block is absent. |
| 2 | Switch to full_files mid-conversation | After step 1, toggle Filter B OFF | 3. Ask a follow-up | Injection record exists but marker absent from messages → Filter A re-injects. Full content appears. RAG suppressed. |
| 3 | Switch back to RAG | After step 2, toggle Filter B ON | 4. Ask another question | Files restored from `chat.meta`, injection block removed via `[injection:<id>]`, RAG runs again. |
| 4 | RAG mode — no files | Filter B ON, no files uploaded | Ask any question | No file restoration (nothing in meta). Filter B is a no-op for files but removes the (absent) rag_mode flag and (absent) injection block. |
| 5 | Multiple uploads in RAG mode | Filter B ON | Upload 2 files, ask question | Both files processed by RAG pipeline. |
| 6 | Toggle ON at chat start | Filter B ON, no prior messages | Upload file, ask question | RAG runs immediately. Filter A persists refs and injects content. Filter B restores refs and removes the injection block before `chat_completion_files_handler` runs. |

---

## 11. Dependencies on Filter A

Filter B **requires** Filter A to be active on the same model. Without Filter A:

- `chat.meta["rag_mode_files"]` is never populated → Filter B has nothing to restore.
- `chat.meta["full_files_injected"]` is never populated → Filter B has no injection id to search for.
- The full content block never exists → Filter B has nothing to remove.
- The `rag_mode` flag is never set → Filter B removes a non-existent key.
- Files pass through normally → the built-in RAG runs regardless of Filter B's state.

In this scenario, Filter B is effectively a no-op. The two filters form a matched pair.

---

## 12. Implementation Order

1. Complete and verify Filter A (`rag_default_off`) first
2. Write `rag_enable.py` with the complete `Filter` class and helpers
3. Deploy to Open WebUI Admin Panel
4. Assign both filters to the same test model (A: prio 0, B: prio 1)
5. Configure `rag_enable` to start OFF by default
6. Run verification checklist (section 10)
7. Proceed to Pipe integration (see DESIGN.md section 6 — out of scope for this project)
