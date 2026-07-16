"""
title: RAG Default Off
author: your_name
version: 1.0.0
required_open_webui_version: 0.5.0
description: >
    Always-on filter that suppresses built-in RAG and injects full file
    content into context. Part of the RAG Mode Selector system.
    Pair with 'rag_enable' (priority 1) to let users toggle RAG on demand.
"""

import logging
from typing import Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — module-level, outside the Filter class
# ---------------------------------------------------------------------------


def _normalize_refs(files: list[dict]) -> list[dict]:
    """Extract {id, name} from file objects, discard everything else."""
    return [
        {"id": f["id"], "name": f.get("name", "unknown")}
        for f in files
        if isinstance(f, dict) and f.get("id")
    ]


async def _persist_file_references(chat_id: str | None, refs: list[dict]) -> None:
    """Write file references to ``chat.meta['rag_mode_files']``.

    Uses the same in-place mutation pattern as ``Chats.update_chat_tags_by_id``:
    fetch the ORM row → mutate ``meta`` → commit.
    """
    if not chat_id or not refs:
        return

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return
        chat_item.meta = {
            **(chat_item.meta or {}),
            "rag_mode_files": refs,
        }
        await db.commit()


async def _read_persisted_refs(chat_id: str | None) -> list[dict] | None:
    """Read ``chat.meta['rag_mode_files']``, return ``None`` if absent."""
    if not chat_id:
        return None

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return None
        return (chat_item.meta or {}).get("rag_mode_files")


def _has_full_files_marker(messages: list[dict]) -> bool:
    """Check whether ``[ FULL_FILES_START ]`` exists in any system message.

    Handles both plain-string content and multimodal content (list of
    ``ContentPart`` objects used by Gemini, Claude, etc.).
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


def _build_full_files_block(files_content: list[tuple[str, str]]) -> str:
    """Build the delimited full-files block.

    Args:
        files_content: List of ``(filename, content)`` tuples.

    Returns:
        A single string with start/end markers and all file contents.
    """
    parts = ["[ FULL_FILES_START ]\n"]
    for filename, content in files_content:
        parts.append(f"\n--- {filename} ---\n{content}\n")
    parts.append("\n[ FULL_FILES_END ]")
    return "".join(parts)


async def _inject_full_content(
    messages: list[dict],
    file_refs: list[dict],
) -> list[dict]:
    """Inject full file content as a system message at position 0.

    Skips files that can't be found in the DB or have empty content.
    Each lookup failure is logged and does not abort the remaining files.
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
                log.warning("rag_default_off: file not found in DB — %s", file_id)
                continue
            content = (file_model.data or {}).get("content", "")
            if content:
                files_content.append((ref.get("name", "unknown"), content))
        except Exception:
            log.exception("rag_default_off: error reading file %s", file_id)
            continue

    if not files_content:
        return messages

    block = _build_full_files_block(files_content)
    messages.insert(0, {"role": "system", "content": block})
    return messages


async def _safe_emit(event_emitter: Callable, event_type: str, data: dict) -> None:
    """Emit an event, swallowing any error so a UI glitch never crashes the filter."""
    try:
        await event_emitter({"type": event_type, "data": data})
    except Exception:
        log.debug("rag_default_off: event_emitter failed (non-fatal)")


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    """Always-on filter (priority 0) that enforces *full_files* mode.

    Every request with attached files gets:
    1.  File references persisted to ``chat.meta["rag_mode_files"]``.
    2.  ``body["metadata"]["files"]`` and ``body["files"]`` cleared
        (suppresses the built-in RAG pipeline).
    3.  Full document content injected as a system message.
    4.  ``body["metadata"]["rag_mode"]`` set to ``"full_files"`` for
        downstream consumers (e.g. the ``agent_loop_guard`` Pipe).
    """

    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()
        # No self.toggle — always active, no UI chip

    async def inlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __user__: dict | None = None,
        __event_emitter__: Callable | None = None,
    ) -> dict:
        log.info("rag_default_off: inlet — chat_id=%s", __chat_id__)

        # ---- 0. Guard: ensure metadata is a writable dict ------------------
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata

        # ---- 1. Resolve file references ----------------------------------
        # Source A: fresh files from the current request (first upload turn).
        # Source B: persisted references from chat.meta (subsequent turns).
        files_from_body = metadata.pop("files", None) or []
        body.pop("files", None)  # also clear top-level key, if present

        new_refs = _normalize_refs(files_from_body) if files_from_body else None
        persisted_refs = None

        if new_refs:
            log.info(
                "rag_default_off: fresh files from body — %d file(s)",
                len(new_refs),
            )
        else:
            # No fresh files — try the persisted store (subsequent turn)
            persisted_refs = await _read_persisted_refs(__chat_id__)
            if persisted_refs:
                log.info(
                    "rag_default_off: restored %d file(s) from chat.meta",
                    len(persisted_refs),
                )

        # The refs to use for content injection:
        inject_refs = new_refs or persisted_refs or []

        # ---- 2. Persist fresh references (first turn with files) ---------
        if new_refs:
            await _persist_file_references(__chat_id__, new_refs)
            log.info(
                "rag_default_off: persisted %d file(s) to chat.meta",
                len(new_refs),
            )

        # ---- 3. Inject full content (if not already present) --------------
        messages = body.get("messages", [])
        if inject_refs:
            if _has_full_files_marker(messages):
                log.info(
                    "rag_default_off: [FULL_FILES_START] already present — skip injection",
                )
                if __event_emitter__:
                    _safe_emit(
                        __event_emitter__,
                        "status",
                        {"description": "Full files mode (cached)", "done": True},
                    )
            else:
                messages = await _inject_full_content(messages, inject_refs)
                body["messages"] = messages
                log.info(
                    "rag_default_off: injected full content for %d file(s)",
                    len(inject_refs),
                )
                if __event_emitter__:
                    _safe_emit(
                        __event_emitter__,
                        "status",
                        {
                            "description": f"Full files mode — {len(inject_refs)} file(s)",
                            "done": True,
                        },
                    )
        else:
            log.info("rag_default_off: no files — no-op")

        # ---- 4. Set flag for downstream consumers ------------------------
        metadata["rag_mode"] = "full_files"

        return body
