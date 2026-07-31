"""
title: Image to File Storage
author: pi-agent
description: Strips images from LLM context to prevent RAG and base64 bloat. Injects <attached_files> with absolute file URLs so tools (e.g. ComfyUI URL-loading nodes) can fetch the stored images.
required_open_webui_version: 0.5.0
version: 2.9.0
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


def _format_file_tag(file: dict, base_url: str = "") -> str:
    file_id = file.get("id") or file.get("url", "")
    attrs = f'type="{file.get("type", "file")}"'
    if file_id:
        attrs += f' id="{file_id}"'
    url = file.get("url", "")
    if url:
        if base_url and url.startswith("/"):
            url = f"{base_url}{url}"
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


async def _get_public_base_url(request) -> str:
    """Return the public base URL of this Open WebUI instance.

    Prefers the admin-configured "WebUI URL" setting (webui.url), falling
    back to the request's base URL when unset. Lets the injected <file>
    tags carry absolute URLs that external tools can fetch.
    """
    try:
        from open_webui.models.config import Config

        webui_url = await Config.get("webui.url")
        if webui_url:
            return str(webui_url).rstrip("/")
    except Exception as exc:
        log.warning("Failed to read webui.url config: %s", exc)

    base = getattr(request, "base_url", None)
    return str(base).rstrip("/") if base else ""


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
        has_uploaded = False
        base_url = await _get_public_base_url(__request__) if __request__ is not None else ""

        # ── Step 1: handle body["files"] ────────────────────────────────────
        files: Optional[list] = body.get("files")
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    has_uploaded = True
                    file_tags.append(_format_file_tag(f, base_url))
                else:
                    non_images.append(f)
            body["files"] = non_images if non_images else None

        # ── Step 2: strip image_url from message content ────────────────────
        if messages:
            for msg in messages:
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                new_content = []
                for item in content:
                    if not _is_image_url_block(item):
                        new_content.append(item)
                        continue
                    url = item.get("image_url", {}).get("url", "")
                    if has_uploaded:
                        pass  # handled by step 1
                    elif _is_base64_uri(url):
                        purl = await self._persist_base64(url, __request__, __metadata__, __user__)
                        if purl:
                            file_tags.append(_format_file_tag({"url": purl, "type": "image"}, base_url))
                        else:
                            file_tags.append(_format_file_tag({"url": "(base64 stripped)", "type": "image"}, base_url))
                    else:
                        file_tags.append(_format_file_tag({"url": url, "type": "image"}, base_url))
                msg["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        # ── Step 3: inject <attached_files> ─────────────────────────────────
        if file_tags and messages:
            ref = _build_attached_files(file_tags)
            if ref:
                _prepend_to_user_message(messages, ref)

        # ── Step 4: notify ─────────────────────────────────────────────────
        if file_tags and __event_emitter__:
            try:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": f"Stored {len(file_tags)} image(s). Removed from context.", "done": True},
                })
            except Exception:
                pass

        return body

    # ------------------------------------------------------------------
    async def _persist_base64(self, url: str, request, metadata, user) -> str | None:
        user_model = _ensure_user_model(user)
        if user_model is None:
            return None
        try:
            from open_webui.utils.files import get_image_url_from_base64
            meta = {
                "chat_id": (metadata or {}).get("chat_id"),
                "message_id": (metadata or {}).get("message_id"),
                "session_id": (metadata or {}).get("session_id"),
            }
            return await get_image_url_from_base64(request, url, meta, user_model)
        except Exception as exc:
            log.warning("_persist_base64 failed: %s", exc)
            return None
