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
import time
from typing import Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers - module-level, outside the Filter class
# ---------------------------------------------------------------------------


def _normalize_refs(files: list[dict]) -> list[dict]:
    """Extract {id, name} from file objects, discard everything else."""
    return [
        {"id": f["id"], "name": f.get("name", "unknown")}
        for f in files
        if isinstance(f, dict) and f.get("id")
    ]


async def _mutate_chat_meta(chat_id: str | None, **updates) -> None:
    """Atomically merge ``updates`` into ``chat.meta``.

    Uses the same in-place mutation pattern as
    ``Chats.update_chat_tags_by_id``.
    """
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


async def _persist_file_references(chat_id: str | None, refs: list[dict]) -> None:
    """Write file references to ``chat.meta['rag_mode_files']``."""
    await _mutate_chat_meta(chat_id, rag_mode_files=refs)


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


async def _read_injection_record(chat_id: str | None) -> dict | None:
    """Read ``chat.meta['full_files_injected']``, return ``None`` if absent."""
    if not chat_id:
        return None

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        chat_item = await db.get(Chat, chat_id)
        if chat_item is None:
            return None
        return (chat_item.meta or {}).get("full_files_injected")


async def _persist_injection_record(
    chat_id: str | None, record: dict
) -> None:
    """Store the injection record in ``chat.meta['full_files_injected']``."""
    await _mutate_chat_meta(chat_id, full_files_injected=record)


def _build_full_content_block(content_parts: list[tuple[str, str]]) -> str:
    """Build the full content block.

    No unique markers or UUIDs are injected — the content is deterministic
    for a given set of files, allowing provider-side prefix caching
    (e.g. DeepSeek) to work across conversations sharing the same document.

    Args:
        content_parts: List of ``(filename, content)`` tuples.

    Returns:
        A single string with all file contents, separated by filename headers.
    """
    sections = []
    for filename, content in content_parts:
        sections.append(f"--- {filename} ---\n{content}")
    return "\n\n".join(sections)


async def _resolve_and_inject(
    messages: list[dict],
    file_refs: list[dict],
) -> tuple[list[dict], dict]:
    """Resolve file contents from the DB and inject as a system message.

    Args:
        messages: Current message list (will be prepended to).
        file_refs: List of ``{"id": ..., "name": ...}``.

    Returns:
        ``(updated_messages, injection_record)``.
        ``injection_record`` is ``{"at": int, "files": [...]}``.
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

    block = _build_full_content_block(content_parts)
    messages.insert(0, {"role": "system", "content": block})

    record = {
        "at": int(time.time()),
        "files": file_refs,
    }
    return messages, record


async def _safe_emit(
    event_emitter: Callable, event_type: str, data: dict
) -> None:
    """Emit an event, swallowing errors so a UI glitch never crashes the filter."""
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
    3.  Full document content injected as a system message (deterministic
        content without unique markers, preserving provider prefix cache).
    4.  An injection record stored in ``chat.meta["full_files_injected"]``
        so Filter B can later locate and remove the block.
    5.  ``body["metadata"]["rag_mode"]`` set to ``"full_files"`` for
        downstream consumers (e.g. the ``agent_loop_guard`` Pipe).
    """

    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()
        # No self.toggle - always active, no UI chip

    async def inlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __user__: dict | None = None,
        __event_emitter__: Callable | None = None,
    ) -> dict:
        log.info("rag_default_off: inlet - chat_id=%s", __chat_id__)

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
                "rag_default_off: fresh files from body - %d file(s)",
                len(new_refs),
            )
        else:
            persisted_refs = await _read_persisted_refs(__chat_id__)
            if persisted_refs:
                log.info(
                    "rag_default_off: restored %d file(s) from chat.meta",
                    len(persisted_refs),
                )

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
            existing_record = await _read_injection_record(__chat_id__)

            if existing_record:
                # Compare file IDs stored in the record against current refs.
                # If the same files are already recorded, the content is
                # already in the conversation history served to the LLM.
                existing_ids = {f["id"] for f in existing_record.get("files", [])}
                current_ids = {f["id"] for f in inject_refs}

                if existing_ids == current_ids:
                    log.info(
                        "rag_default_off: already injected (%d file(s)) - skip",
                        len(inject_refs),
                    )
                    if __event_emitter__:
                        await _safe_emit(
                            __event_emitter__,
                            "status",
                            {
                                "description": "Full files mode (cached)",
                                "done": True,
                            },
                        )
                    metadata["rag_mode"] = "full_files"
                    return body

                log.info(
                    "rag_default_off: files changed - re-injecting "
                    "(old=%s, new=%s)",
                    existing_ids,
                    current_ids,
                )

            messages, record = await _resolve_and_inject(
                messages, inject_refs
            )
            body["messages"] = messages
            await _persist_injection_record(__chat_id__, record)
            log.info(
                "rag_default_off: injected full content - %d file(s)",
                len(inject_refs),
            )
            if __event_emitter__:
                await _safe_emit(
                    __event_emitter__,
                    "status",
                    {
                        "description": (
                            f"Full files mode - {len(inject_refs)} file(s)"
                        ),
                        "done": True,
                    },
                )
        else:
            log.info("rag_default_off: no files - no-op")

        # ---- 4. Set flag for downstream consumers ------------------------
        metadata["rag_mode"] = "full_files"

        return body
