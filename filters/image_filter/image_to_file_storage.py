"""
title: Image to File Storage
author: pi-agent
version: 2.0.0
required_open_webui_version: 0.5.0
description: >
    Prevents images uploaded via the "+" button from being injected into the
    LLM context. Images are already stored as permanent files by Open WebUI's
    upload API; this filter removes them from the message payload so they
    neither undergo RAG embedding (via chat_completion_files_handler) nor
    get converted to base64 (via convert_url_images_to_base64). The stored
    file references remain accessible via file URL for downstream tools.

    What it does at the inlet:
    1. Scans body["files"] (top-level) and removes image-type entries so
       chat_completion_files_handler skips them (no wasted RAG on images).
    2. Scans user message content for image_url blocks (reconstructed from
       DB history) and strips them to prevent base64 conversion.

    Files are already persisted by the upload API — no additional storage
    call is needed.
"""

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IMAGE_MIME_PREFIXES = ("image/",)
IMAGE_FILE_TYPES = ("image",)


def _is_image_file(file_ref: Any) -> bool:
    """Return True if *file_ref* is an image file reference."""
    if not isinstance(file_ref, dict):
        return False

    # Check by content_type
    ct = file_ref.get("content_type", "") or ""
    if ct.lower().startswith("image/"):
        return True

    # Check by type field
    ft = file_ref.get("type", "") or ""
    if ft.lower() in ("image",):
        return True

    return False


def _is_image_url_block(item: Any) -> bool:
    """Return True if *item* is an image_url content block."""
    return isinstance(item, dict) and item.get("type") == "image_url" and bool(item.get("image_url", {}).get("url"))


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Execution order. Lower values run first.",
        )
        strip_files_metadata: bool = Field(
            default=True,
            description=(
                "When True, removes image file references from "
                "body['files'] (top-level) so they never reach "
                "chat_completion_files_handler via metadata['files']. "
                "Images are already stored as permanent files by the upload API."
            ),
        )
        strip_image_url_context: bool = Field(
            default=True,
            description=(
                "When True, removes image_url content blocks from user "
                "messages to prevent convert_url_images_to_base64 from "
                "injecting base64 into the LLM context. "
                "The image URLs remain accessible from the stored message."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        self.icon = "🖼️"

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        **kwargs,
    ) -> dict:
        """Process user messages and strip images from LLM-bound context.

        Called by Open WebUI in the filter pipeline (priority 0 so it
        runs before chat_completion_files_handler and
        convert_url_images_to_base64).

        At the time this filter runs, file references are at ``body["files"]``
        (top-level key).  ``process_chat_payload`` pops them into ``metadata``
        *after* the filter pipeline completes.
        """
        modified = False
        total_images = 0

        # ---- 1. Strip image file refs from body["files"] -------------------
        # These files were uploaded via the "+" button and stored by the
        # upload API.  At this point they live at body["files"] (top-level).
        # `process_chat_payload` will pop them into `metadata["files"]` after
        # the filter pipeline, and then chat_completion_files_handler will
        # try to run RAG embedding on them.  Removing images here prevents
        # wasteful RAG on pixel data.
        if self.valves.strip_files_metadata:
            files: Optional[list] = body.get("files")
            if isinstance(files, list) and any(_is_image_file(f) for f in files):
                non_image_files = [f for f in files if not _is_image_file(f)]
                if len(non_image_files) != len(files):
                    image_count = len(files) - len(non_image_files)
                    total_images += image_count
                    modified = True
                    log.info(
                        "Removed %d image(s) from body.files (kept %d non-image file(s))",
                        image_count,
                        len(non_image_files),
                    )
                    body["files"] = non_image_files if non_image_files else None

        # ---- 2. Strip image_url blocks from user message content ---------
        # Messages reconstructed from DB have message['files'] converted to
        # image_url blocks.  These get converted to base64 by
        # convert_url_images_to_base64() later in the pipeline.
        messages: list[dict] = body.get("messages", [])
        if self.valves.strip_image_url_context and messages:
            for message in messages:
                if message.get("role") != "user":
                    continue

                content = message.get("content")
                if not isinstance(content, list):
                    continue

                # Filter out any image_url blocks
                new_content = [item for item in content if not _is_image_url_block(item)]

                if len(new_content) != len(content):
                    removed = len(content) - len(new_content)
                    total_images += removed
                    modified = True
                    log.info("Stripped %d image_url block(s) from user message", removed)

                # If after stripping there's no content at all, insert an
                # empty text block to keep the message valid.
                message["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        # ---- 3. Notify the user -----------------------------------------
        if modified and total_images > 0 and __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"Removed {total_images} image(s) from LLM context "
                                "(files remain stored — accessible via file URL)"
                            ),
                            "done": True,
                        },
                    }
                )
            except Exception:
                log.debug("status event failed (non-fatal)")

        return body
