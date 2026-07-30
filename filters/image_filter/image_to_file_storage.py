"""
title: Image to File Storage
author: pi-agent
description: Strips images from the LLM payload to prevent RAG on pixel data and base64 bloat. Injects <attached_files> with <file> tags so the model can reference images by ID/URL.
required_open_webui_version: 0.5.0
version: 2.1.0
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

    ct = file_ref.get("content_type", "") or ""
    if ct.lower().startswith("image/"):
        return True

    ft = file_ref.get("type", "") or ""
    if ft.lower() in ("image",):
        return True

    return False


def _is_image_url_block(item: Any) -> bool:
    """Return True if *item* is an image_url content block."""
    return isinstance(item, dict) and item.get("type") == "image_url" and bool(item.get("image_url", {}).get("url"))


def _format_file_tag(file: dict) -> str:
    """Build a <file> tag matching Open WebUI's own add_file_context() format.

    See open_webui/utils/middleware.py::add_file_context() — the same shape
    ensures deterministic output regardless of which layer injects the tag.
    """
    file_id = file.get("id") or file.get("url", "")
    attrs = f'type="{file.get("type", "file")}"'
    if file_id:
        attrs += f' id="{file_id}"'
    url = file.get("url", "")
    if url:
        attrs += f' url="{url}"'
    if file.get("content_type"):
        attrs += f' content_type="{file["content_type"]}"'
    if file.get("name"):
        attrs += f' name="{file["name"]}"'
    return f"<file {attrs}/>"


def _build_attached_files(
    body_files: list[dict],
    image_urls: list[str],
) -> str:
    """Build an <attached_files> block from image file refs and image_urls.

    *body_files* are the image entries removed from ``body["files"]``.
    *image_urls* are URL strings extracted from ``image_url`` content blocks.
    """
    tags: list[str] = []
    seen: set[str] = set()

    for f in body_files:
        key = f.get("url") or f.get("id") or ""
        if key and key not in seen:
            seen.add(key)
            tags.append(_format_file_tag(f))

    for url in image_urls:
        if url not in seen:
            seen.add(url)
            tags.append(_format_file_tag({"url": url, "type": "image"}))

    if not tags:
        return ""
    return "<attached_files>\n" + "\n".join(tags) + "\n</attached_files>\n\n"


def _prepend_to_user_message(messages: list[dict], text: str) -> None:
    """Prepend *text* to the last user message's content (text or list)."""
    last_user = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user = msg
            break
    if last_user is None:
        return

    content = last_user.get("content")
    if isinstance(content, list):
        if content and isinstance(content[0], dict) and content[0].get("type") == "text":
            content[0]["text"] = text + content[0]["text"]
        else:
            content.insert(0, {"type": "text", "text": text})
    elif isinstance(content, str):
        last_user["content"] = text + content


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
                "injecting base64 into the LLM context."
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
        messages: list[dict] = body.get("messages", [])
        modified = False
        total_images = 0
        stripped_files: list[dict] = []
        stripped_urls: list[str] = []

        # ---- 1. Strip image file refs from body["files"] -------------------
        if self.valves.strip_files_metadata:
            files: Optional[list] = body.get("files")
            if isinstance(files, list) and any(_is_image_file(f) for f in files):
                non_image_files = [f for f in files if not _is_image_file(f)]
                if len(non_image_files) != len(files):
                    image_count = len(files) - len(non_image_files)
                    total_images += image_count
                    modified = True
                    stripped_files = [f for f in files if _is_image_file(f)]
                    log.info(
                        "Removed %d image(s) from body.files (kept %d non-image file(s))",
                        image_count,
                        len(non_image_files),
                    )
                    body["files"] = non_image_files if non_image_files else None

        # ---- 2. Strip image_url blocks from user message content ---------
        if self.valves.strip_image_url_context and messages:
            for message in messages:
                if message.get("role") != "user":
                    continue

                content = message.get("content")
                if not isinstance(content, list):
                    continue

                # Collect URLs before stripping
                for item in content:
                    if _is_image_url_block(item):
                        url = item.get("image_url", {}).get("url", "")
                        if url:
                            stripped_urls.append(url)

                new_content = [item for item in content if not _is_image_url_block(item)]
                if len(new_content) != len(content):
                    removed = len(content) - len(new_content)
                    total_images += removed
                    modified = True
                    log.info("Stripped %d image_url block(s) from user message", removed)

                message["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        # ---- 3. Inject <attached_files> block into last user message -------
        # Mirrors Open WebUI's add_file_context() format so the model is aware
        # of the attached images and can reference them by ID/URL.  The block
        # is deterministic (same input → same output) so it does not break
        # prefix-based context caching.
        if modified and total_images > 0 and stripped_files + stripped_urls:
            ref_block = _build_attached_files(stripped_files, stripped_urls)
            if ref_block and messages:
                _prepend_to_user_message(messages, ref_block)
                log.info("Injected <attached_files> into user message (%d image(s))", total_images)

        # ---- 4. Notify the user -----------------------------------------
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
