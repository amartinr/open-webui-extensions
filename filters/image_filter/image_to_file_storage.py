"""
title: Image to File Storage
author: pi-agent
description: Strips images from the LLM payload to prevent RAG on pixel data and base64 bloat. Injects <attached_files> with <file> tags so the model can reference images by ID/URL.
required_open_webui_version: 0.5.0
version: 2.1.0
"""

import json
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
    return isinstance(item, dict) and item.get("type") == "image_url" and bool(item.get("image_url", {}).get("url"))


def _is_base64_data_uri(item: Any) -> bool:
    """Return True if *item* is an image_url block with base64 data (already converted)."""
    if not _is_image_url_block(item):
        return False
    url = item.get("image_url", {}).get("url", "")
    return url.startswith("data:image/") and ";base64," in url


def _get_url(item: Any) -> str:
    return item.get("image_url", {}).get("url", "")


def _format_file_tag(file: dict) -> str:
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


def _build_attached_files(body_files: list[dict], image_urls: list[str]) -> str:
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


def _trunc(s: str, n: int = 120) -> str:
    """Truncate string for logging."""
    return s[:n] + "..." if len(s) > n else s


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Execution order. Lower values run first.")
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
        messages: list[dict] = body.get("messages", [])
        modified = False
        total_stripped = 0
        stripped_files: list[dict] = []
        stripped_urls: list[str] = []

        # ── DEBUG: dump shape of incoming body ──────────────────────────────
        files_key = body.get("files")
        log.info("=== ImageFilter INLET ===")
        log.info("body.files type=%s value=%s", type(files_key).__name__, files_key)

        if isinstance(files_key, list):
            for i, f in enumerate(files_key):
                log.info("  files[%d]: type=%s, id=%s, url=%s", i, f.get("type"), f.get("id"), _trunc(str(f.get("url",""))))

        log.info("messages count=%d", len(messages))
        for idx, m in enumerate(messages):
            role = m.get("role")
            ctype = type(m.get("content")).__name__
            files_in_msg = bool(m.get("files"))
            log.info("  messages[%d]: role=%s, content_type=%s, has_files_field=%s", idx, role, ctype, files_in_msg)

            content = m.get("content")
            if isinstance(content, list):
                for j, item in enumerate(content):
                    if item.get("type") == "text":
                        log.info("    content[%d]: text (len=%d, first=%.100s)", j, len(item.get("text","")), item.get("text","")[:100])
                    elif item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        is_b64 = url.startswith("data:image/")
                        log.info("    content[%d]: image_url is_base64=%s url=%s", j, is_b64, _trunc(url, 80))
            elif isinstance(content, str):
                log.info("    content (str, len=%d, first=%.100s)", len(content), content[:100])

        # ── 1. Strip image file refs from body["files"] ─────────────────────
        if self.valves.strip_files_metadata:
            files: Optional[list] = body.get("files")
            if isinstance(files, list) and any(_is_image_file(f) for f in files):
                non_image_files = [f for f in files if not _is_image_file(f)]
                if len(non_image_files) != len(files):
                    image_count = len(files) - len(non_image_files)
                    total_stripped += image_count
                    modified = True
                    stripped_files = [f for f in files if _is_image_file(f)]
                    log.info(
                        "Removed %d image(s) from body.files (kept %d non-image file(s))",
                        image_count, len(non_image_files),
                    )
                    body["files"] = non_image_files if non_image_files else None

        # ── 2. Strip image_url blocks from user message content ─────────────
        if self.valves.strip_image_url_context and messages:
            for message in messages:
                if message.get("role") != "user":
                    continue

                content = message.get("content")
                if not isinstance(content, list):
                    log.info("  SKIP (content is not list, type=%s)", type(content).__name__)
                    continue

                # Collect URLs before stripping
                for item in content:
                    if _is_image_url_block(item):
                        url = _get_url(item)
                        if url:
                            stripped_urls.append(url)

                new_content = [item for item in content if not _is_image_url_block(item)]
                base64_count = sum(1 for item in content if _is_base64_data_uri(item))

                if len(new_content) != len(content):
                    removed = len(content) - len(new_content)
                    total_stripped += removed
                    modified = True
                    log.info(
                        "Stripped %d image_url block(s) from user message (%d were base64)",
                        removed, base64_count,
                    )
                else:
                    log.info("  No image_url blocks found in this user message")

                message["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        # ── 3. Inject <attached_files> block ────────────────────────────────
        if modified and total_stripped > 0 and (stripped_files or stripped_urls):
            ref_block = _build_attached_files(stripped_files, stripped_urls)
            if ref_block and messages:
                _prepend_to_user_message(messages, ref_block)
                log.info("Injected <attached_files> into user message (%d image(s))", total_stripped)

        # ── 4. Notify the user ──────────────────────────────────────────────
        if modified and total_stripped > 0 and __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"Removed {total_stripped} image(s) from LLM context "
                                "(files remain stored — accessible via file URL)"
                            ),
                            "done": True,
                        },
                    }
                )
            except Exception:
                log.debug("status event failed (non-fatal)")

        log.info("=== ImageFilter OUTLET: total_stripped=%d, modified=%s ===", total_stripped, modified)
        return body
