"""
title: Image to File Storage
author: pi-agent
description: Strips images from the LLM payload to prevent RAG on pixel data and base64 bloat. Injects <attached_files> with <file> tags so the model can reference images by ID/URL.
required_open_webui_version: 0.5.0
version: 2.4.0
"""

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_image_file(file_ref: Any) -> bool:
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
    if not isinstance(item, dict):
        return False
    if item.get("type") != "image_url":
        return False
    return bool(item.get("image_url", {}).get("url"))


def _is_base64_uri(url: str) -> bool:
    return url.startswith("data:image/") and ";base64," in url


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


def _build_attached_files(tags: list[str]) -> str:
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


def _ensure_user_model(user: Any) -> Any:
    if isinstance(user, dict):
        try:
            from open_webui.models.users import UserModel
            return UserModel(**user)
        except Exception as exc:
            log.warning("Failed to convert user dict to UserModel: %s", exc)
            return None
    return user


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
        file_tags: list[str] = []

        # Resolve the server base URL so file paths become absolute URLs
        # that downstream tools (e.g. ComfyUI) can fetch.
        base_url = ""
        if __request__ is not None:
            try:
                base_url = str(__request__.base_url).rstrip("/")
            except Exception:
                pass

        def _abs(url: str) -> str:
            """Prepend base_url if *url* is a relative path."""
            if base_url and url and url.startswith("/"):
                return base_url + url
            return url

        # ── Step 1: handle body["files"] (uploaded via "+" button) ──────────
        # These images are already persisted by the upload API.  The base64
        # blocks in the message content (added by convert_url_images_to_base64)
        # are just reconstructions of these same files — do NOT persist them
        # again.  We track this via has_uploaded_images.
        has_uploaded_images = False
        files: Optional[list] = body.get("files")
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    has_uploaded_images = True
                    tag = dict(f)
                    tag["url"] = _abs(tag.get("url", ""))
                    file_tags.append(_format_file_tag(tag))
                else:
                    non_images.append(f)
            if non_images:
                body["files"] = non_images
            else:
                body["files"] = None

        # ── Step 2: handle base64 images in message content ─────────────────
        # convert_url_images_to_base64() runs before this filter and turns any
        # file URL into a data: URI inside image_url blocks.  If the image was
        # already handled via body["files"], just strip the block without
        # persisting.  Otherwise the user pasted an image (Ctrl+V) and we must
        # persist it to get a real file URL.
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

                    if has_uploaded_images:
                        # Already handled via body["files"] — just strip.
                        # The <file> tag from step 1 already has the real URL.
                        pass
                    elif _is_base64_uri(url):
                        # Truly pasted image (no body["files"] entry).
                        # Persist it to get a real file URL.
                        persisted = await self._persist_base64_image(
                            __request__, url, __metadata__, __user__
                        )
                        if persisted:
                            file_tags.append(
                                _format_file_tag({"url": _abs(persisted), "type": "image"})
                            )
                            log.info("Persisted pasted image to %s", persisted)
                        else:
                            # Strip anyway — can't let base64 reach the LLM.
                            file_tags.append(_format_file_tag({"url": "(base64 stripped)", "type": "image"}))
                    else:
                        # Already a file URL — keep the reference.
                        file_tags.append(_format_file_tag({"url": _abs(url), "type": "image"}))

                message["content"] = new_content if new_content else [
                    {"type": "text", "text": ""}
                ]

        # ── Step 3: inject <attached_files> into last user message ──────────
        if file_tags and messages:
            ref_block = _build_attached_files(file_tags)
            if ref_block:
                _prepend_to_user_message(messages, ref_block)
                log.info("Injected <attached_files> into user message (%d image(s))", len(file_tags))

        # ── Step 4: notify user ─────────────────────────────────────────────
        if file_tags and __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"Removed {len(file_tags)} image(s) from LLM context. "
                                f"Files stored — accessible via file URL."
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
        user: Any,
    ) -> str | None:
        """Persist a base64 image and return its file URL."""
        user_model = _ensure_user_model(user)
        if user_model is None:
            log.warning("Cannot persist base64 image: no valid user object")
            return None

        try:
            from open_webui.utils.files import get_image_url_from_base64

            meta = {
                "chat_id": (metadata or {}).get("chat_id"),
                "message_id": (metadata or {}).get("message_id"),
                "session_id": (metadata or {}).get("session_id"),
            }
            return await get_image_url_from_base64(request, base64_url, meta, user_model)
        except Exception as exc:
            log.warning("Failed to persist base64 image: %s", exc)
            return None
