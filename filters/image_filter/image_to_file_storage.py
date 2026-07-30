"""
title: Image to File Storage
author: pi-agent
description: Strips images from the LLM payload to prevent RAG on pixel data and base64 bloat. Injects <attached_files> with <file> tags so the model can reference images by ID/URL.
required_open_webui_version: 0.5.0
version: 2.2.0
"""

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_image_file(file_ref: Any) -> bool:
    """Return True if *file_ref* is an image file reference."""
    if not isinstance(file_ref, dict):
        return False
    ct = (file_ref.get("content_type") or "").lower()
    if ct.startswith("image/"):
        return True
    ft = (file_ref.get("type") or "").lower()
    if ft in ("image",):
        return True
    return False


def _is_image_url_block(item: Any) -> bool:
    """Return True if *item* is an image_url content block."""
    if not isinstance(item, dict):
        return False
    if item.get("type") != "image_url":
        return False
    return bool(item.get("image_url", {}).get("url"))


def _is_base64_uri(url: str) -> bool:
    """Check if a URL is a base64 data URI."""
    return url.startswith("data:image/") and ";base64," in url


def _format_file_tag(file: dict) -> str:
    """Build a <file> tag matching Open WebUI's add_file_context() format."""
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


def _build_attached_files(tags: list[str]) -> str:
    """Wrap <file> tags in an <attached_files> block."""
    if not tags:
        return ""
    return "<attached_files>\n" + "\n".join(tags) + "\n</attached_files>\n\n"


def _prepend_to_user_message(messages: list[dict], text: str) -> None:
    """Prepend *text* to the last user message's content."""
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
        priority: int = Field(default=0, description="Execution order. Lower values run first.")

    def __init__(self):
        self.valves = self.Valves()
        self.icon = "🖼️"

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__=None,
        __event_emitter__=None,
        __metadata__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        messages: list[dict] = body.get("messages", [])
        total_images = 0
        file_tags: list[str] = []

        # ── Step 1: handle body["files"] (uploaded via "+" button) ──────────
        # These are already persisted by the upload API. Remove them from the
        # metadata so RAG skips them, but keep the file IDs for reference.
        files: Optional[list] = body.get("files")
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    total_images += 1
                    file_tags.append(_format_file_tag(f))
                else:
                    non_images.append(f)
            if non_images:
                body["files"] = non_images
            else:
                body["files"] = None

        # ── Step 2: handle base64 images in message content ─────────────────
        # User pasted an image (Ctrl+V) — it's a data: URI in the content list.
        # It was never uploaded, so persist it now via get_image_url_from_base64()
        # to obtain a proper file URL.
        if messages:
            for message in messages:
                if message.get("role") != "user":
                    continue

                content = message.get("content")
                if not isinstance(content, list):
                    continue

                new_content = []
                for item in content:
                    if not _is_image_url_block(item):
                        new_content.append(item)
                        continue

                    url = item.get("image_url", {}).get("url", "")
                    if _is_base64_uri(url) and __request__:
                        # Persist the base64 image as a permanent file
                        persisted_url = await self._persist_base64_image(
                            __request__, url, __metadata__, __user__
                        )
                        total_images += 1
                        if persisted_url:
                            # Use the file URL for the <file> tag
                            file_tags.append(
                                _format_file_tag({"url": persisted_url, "type": "image"})
                            )
                            log.info("Persisted base64 image to %s", persisted_url)
                        else:
                            log.warning("Failed to persist base64 image")
                    else:
                        # Already a file URL, just keep it as a reference
                        total_images += 1
                        file_tags.append(_format_file_tag({"url": url, "type": "image"}))

                message["content"] = new_content if new_content else [
                    {"type": "text", "text": ""}
                ]

        # ── Step 3: inject <attached_files> block into last user message ────
        if total_images > 0 and file_tags and messages:
            ref_block = _build_attached_files(file_tags)
            if ref_block:
                _prepend_to_user_message(messages, ref_block)
                log.info("Injected <attached_files> into user message (%d image(s))", total_images)

        # ── Step 4: notify the user ─────────────────────────────────────────
        if total_images > 0 and __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"Stored {total_images} image(s) as files. "
                                f"Removed from LLM context."
                            ),
                            "done": True,
                        },
                    }
                )
            except Exception:
                log.debug("status event failed (non-fatal)")

        return body

    # ------------------------------------------------------------------
    async def _persist_base64_image(
        self,
        request: Any,
        base64_url: str,
        metadata: dict | None,
        user: dict | None,
    ) -> str | None:
        """Persist a base64 image and return its file URL."""
        try:
            from open_webui.utils.files import get_image_url_from_base64

            meta = {
                "chat_id": (metadata or {}).get("chat_id"),
                "message_id": (metadata or {}).get("message_id"),
                "session_id": (metadata or {}).get("session_id"),
            }
            return await get_image_url_from_base64(request, base64_url, meta, user)
        except Exception as exc:
            log.warning("Failed to persist base64 image: %s", exc)
            return None
