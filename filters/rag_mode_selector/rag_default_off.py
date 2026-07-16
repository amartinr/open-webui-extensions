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
# Module-level caches  (survive across inlet calls within the same process)
# ---------------------------------------------------------------------------

# Maps file_id -> full text content, populated on first DB read.
# Cleared whenever fresh files arrive in a request body.
_file_content_cache: dict[str, str] = {}

# Maps chat_id -> frozenset of file_ids that have been injected on a
# previous turn.  When the same file set is seen again and the content
# block is still present in messages[0], the entire injection step is
# skipped — no DB reads, no string building.
_injection_guard: dict[str, frozenset] = {}


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


async def _persist_file_references(chat_id: str | None, refs: list[dict]) -> None:
    """Write file references to ``chat.meta['rag_mode_files']``.

    Uses a targeted ``UPDATE`` that touches only the ``meta`` column,
    avoiding a full-row ``SELECT`` (which would pull the potentially
    large ``chat`` JSON column into memory).
    """
    if not chat_id or not refs:
        return

    from sqlalchemy import select, update
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        # Load only the meta column, not the full Chat row.
        result = await db.execute(select(Chat.meta).where(Chat.id == chat_id))
        row = result.one_or_none()
        if row is None:
            return
        current_meta = row[0] or {}
        await db.execute(
            update(Chat)
            .where(Chat.id == chat_id)
            .values(meta={**current_meta, "rag_mode_files": refs})
        )
        await db.commit()


async def _read_persisted_refs(chat_id: str | None) -> list[dict] | None:
    """Read ``chat.meta['rag_mode_files']``, return ``None`` if absent.

    Uses a targeted ``SELECT meta`` query instead of loading the full
    ``Chat`` row (which includes the potentially large ``chat`` JSON
    column with all messages).
    """
    if not chat_id:
        return None

    from sqlalchemy import select
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.chats import Chat

    async with get_async_db_context() as db:
        result = await db.execute(select(Chat.meta).where(Chat.id == chat_id))
        row = result.one_or_none()
        if row is None:
            return None
        meta = row[0] or {}
        return meta.get("rag_mode_files")


def _build_full_content_block(content_parts: list[tuple[str, str]]) -> str:
    """Build a deterministic full-content block.

    No unique markers or UUIDs — the content is purely a function of the
    file set, allowing provider-side prefix caching (e.g. DeepSeek) to
    work across conversations that share the same document.

    Args:
        content_parts: List of ``(filename, content)`` tuples.

    Returns:
        A single string with all file contents separated by filename headers.
    """
    sections = []
    for filename, content in content_parts:
        sections.append(f"--- {filename} ---\n{content}")
    return "\n\n".join(sections)


async def _resolve_and_inject(
    messages: list[dict],
    file_refs: list[dict],
) -> list[dict]:
    """Resolve file contents from the DB and inject as a system message.

    Uses the module-level ``_file_content_cache`` to avoid re-reading the
    same files from the database on subsequent turns.  The cache is
    invalidated by the caller whenever fresh files arrive.

    Open WebUI does not preserve filter-injected system messages between
    turns in ``body["messages"]``, so the content is re-injected on every
    request *when the guard permits it*.  The content is deterministic
    (no UUIDs), so this does not break provider-side prefix caching.

    Args:
        messages: Current message list (will be prepended to).
        file_refs: List of ``{"id": ..., "name": ...}``.

    Returns:
        Updated message list with the content block at position 0.
    """
    if not file_refs:
        return messages

    from open_webui.models.files import Files

    content_parts: list[tuple[str, str]] = []
    for ref in file_refs:
        file_id = ref.get("id")
        if not file_id:
            continue

        # Check cache first — avoids a DB round-trip per file.
        cached = _file_content_cache.get(file_id)
        if cached is not None:
            content_parts.append((ref.get("name", "unknown"), cached))
            continue

        try:
            file_model = await Files.get_file_by_id(file_id)
            if file_model is None:
                log.warning("rag_default_off: file not found in DB - %s", file_id)
                continue
            raw = (file_model.data or {}).get("content", "")
            if raw:
                _file_content_cache[file_id] = raw
                content_parts.append((ref.get("name", "unknown"), raw))
        except Exception:
            log.exception("rag_default_off: error reading file %s", file_id)
            continue

    if not content_parts:
        return messages

    block = _build_full_content_block(content_parts)
    messages.insert(0, {"role": "system", "content": block})
    return messages


async def _safe_emit(
    event_emitter: Callable, event_type: str, data: dict
) -> None:
    """Emit an event, swallowing errors so a UI glitch never crashes the filter."""
    try:
        await event_emitter({"type": event_type, "data": data})
    except Exception:
        log.debug("rag_default_off: event_emitter failed (non-fatal)")


def _is_content_still_in_messages(messages: list[dict], file_refs: list[dict]) -> bool:
    """Quick O(1) check: is the full-file content block still in ``messages[0]``?

    Looks for ``--- <first filename> ---`` in the first system message.
    This is a fast heuristic — it does not scan the full body for every
    filename, only the first message and the first filename.
    """
    if not messages or not file_refs:
        return False
    first = messages[0]
    if first.get("role") != "system":
        return False
    first_name = file_refs[0].get("name", "")
    if not first_name:
        return False
    return f"--- {first_name} ---" in (first.get("content") or "")


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    """Always-on filter (priority 0) that enforces *full_files* mode.

    Every request with attached files gets:
    1.  File references persisted to ``chat.meta["rag_mode_files"]``
        (needed when the frontend stops re-sending file refs).
    2.  ``body["metadata"]["files"]`` and ``body["files"]`` cleared
        (suppresses the built-in RAG pipeline).
    3.  Full document content injected as a system message at position 0.
        Content is deterministic (no UUIDs), preserving prefix caching.
    4.  ``body["metadata"]["rag_mode"]`` set to ``"full_files"`` for
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
            # Fresh files arrived — invalidate the file-content cache so
            # the next injection round re-reads from the database.
            _file_content_cache.clear()
            # Also clear the per-chat injection guard: the file set changed.
            _injection_guard.pop(__chat_id__, None)

        # ---- 3. Inject full content --------------------------------------
        messages = body.get("messages", [])

        if inject_refs:
            # ---- 3a. Injection guard: skip if nothing changed -----------
            file_ids = frozenset(
                r["id"]
                for r in inject_refs
                if isinstance(r, dict) and r.get("id")
            )
            cached_ids = _injection_guard.get(__chat_id__)
            guard_skip = (
                cached_ids is not None
                and cached_ids == file_ids
                and _is_content_still_in_messages(messages, inject_refs)
            )

            if guard_skip:
                log.info(
                    "rag_default_off: injection guard hit — "
                    "%d file(s) unchanged, skipping",
                    len(inject_refs),
                )
            else:
                messages = await _resolve_and_inject(messages, inject_refs)
                body["messages"] = messages
                _injection_guard[__chat_id__] = file_ids
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
