"""
title: RAG Enable
author: your_name
version: 1.0.0
required_open_webui_version: 0.5.0
description: >
    Toggleable filter that restores built-in RAG when enabled.
    Part of the RAG Mode Selector system.
    Pair with 'rag_default_off' (priority 0) to let users toggle RAG on demand.
    When ON: restores file references from chat.meta, clears the rag_mode
    flag so the built-in RAG pipeline executes normally, and preserves the
    full-content block injected by rag_default_off as a fallback so the
    model can always answer.
    When OFF: passthrough (inlet not called by Open WebUI).
"""

import logging
from typing import Callable

from pydantic import BaseModel

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers  (module-level, outside the Filter class)
# ---------------------------------------------------------------------------


async def _read_persisted_refs(chat_id: str | None) -> list[dict] | None:
    """Read ``chat.meta['rag_mode_files']``, return ``None`` if absent.

    Uses a targeted ``SELECT meta`` query instead of loading the full
    ``Chat`` row (which pulls the potentially large ``chat`` JSON column
    with all messages into memory).
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


def _normalize_refs(refs: list[dict]) -> list[dict]:
    """Normalise a list of file references to ``{id, name}`` shape."""
    return [
        {"id": r["id"], "name": r.get("name", "unknown")}
        for r in refs
        if isinstance(r, dict) and r.get("id")
    ]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    """Toggleable filter (priority 1) that restores built-in RAG when enabled.

    When the user activates this filter (chip ON), it reverses the decisions
    made by ``rag_default_off`` (Filter A, priority 0):

    1. **Restore file references.**  Reads ``chat.meta["rag_mode_files"]``
       and repopulates ``body["metadata"]["files"]`` so the built-in RAG
       pipeline (``chat_completion_files_handler``) sees files and runs
       semantic retrieval + reranking.

    2. **Remove the ``rag_mode`` flag.**  Pops ``body["metadata"]["rag_mode"]``
       so downstream consumers (e.g. the ``agent_loop_guard`` Pipe) know
       that standard RAG is active and leave semantic KB tools in place.

    3. **Preserve the full content block** injected by Filter A.  Although the
       RAG pipeline runs in parallel, the content block guarantees the model
       can always answer even when the RAG pipeline produces no usable context
       (e.g. empty RAG template, ``add_file_context()`` injecting ``<file>``
       tags the model cannot resolve, or bug #25101).  The redundancy is
       harmless (DESIGN.md §8.2 point 4).

    When the filter is OFF (chip disabled), Open WebUI **does not call**
    ``inlet`` at all, so Filter A's decisions stand and the conversation
    remains in full-document (``full_files``) mode.  No passthrough logic
    is needed here.
    """

    class Valves(BaseModel):
        priority: int = 1

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True  # appears as a clickable chip in the chat UI

    async def inlet(
        self,
        body: dict,
        __chat_id__: str | None = None,
        __user__: dict | None = None,
        __event_emitter__: Callable | None = None,
    ) -> dict:
        """Restore RAG mode for this request.

        Called **only** when the user has enabled this filter's chip.
        """
        log.info("rag_enable: inlet ON - chat_id=%s", __chat_id__)

        # ---- 1. Read persisted file references from chat.meta -------------
        refs = await _read_persisted_refs(__chat_id__)
        if not refs:
            log.info("rag_enable: no persisted file references - no-op")
            return body

        log.info("rag_enable: restoring %d file(s) from chat.meta", len(refs))

        # Normalise to {id, name} — the shape Open WebUI expects in metadata.files.
        refs = _normalize_refs(refs)

        # ---- 2. Restore file references in body ---------------------------
        # This is the signal that makes the built-in RAG pipeline run:
        #   middleware.py line 1779:
        #     if files := body.get('metadata', {}).get('files', None)
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata

        metadata["files"] = refs

        # ---- 3. Remove the rag_mode flag ----------------------------------
        # The agent_loop_guard Pipe only removes semantic KB tools when
        # rag_mode == "full_files".  Removing the key signals "RAG mode" to
        # the Pipe (see design doc §7 — Filter ↔ Pipe coordination).
        metadata.pop("rag_mode", None)

        # ---- 4. Keep the full content block from Filter A -----------------
        # In an ideal world we would remove it here and let the RAG pipeline
        # inject chunks via apply_source_context_to_messages().  In practice,
        # the RAG pipeline may not reliably inject content (e.g. empty RAG
        # template, add_file_context() injecting <file> tags the model can't
        # resolve, or bug #25101 where chat_completion_files_handler runs
        # alongside native FC).
        #
        # Keeping the content block ensures the model can ALWAYS answer even
        # when the RAG pipeline produces no usable context.  The redundancy
        # is harmless — the model sees both full content (system message) and
        # any RAG chunks that get injected.  See DESIGN.md §8.2 point 4.
        filenames = [r.get("name", "") for r in refs if isinstance(r, dict)]
        messages = body.get("messages", [])
        if messages and filenames:
            log.info(
                "rag_enable: preserving content block for %d file(s)",
                len(filenames),
            )

        # ---- 5. Notify the user via status event --------------------------
        if __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"RAG mode restored - {len(refs)} file(s)"
                            ),
                            "done": True,
                        },
                    }
                )
            except Exception:
                log.debug("rag_enable: event_emitter failed (non-fatal)")

        return body
