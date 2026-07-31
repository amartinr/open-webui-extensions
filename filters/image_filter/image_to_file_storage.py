"""
title: Image to File Storage
author: pi-agent
description: Strips images from LLM context to prevent RAG and base64 bloat. Injects <attached_files> with file references. When ComfyUI valves are set, uploads images to ComfyUI so workflows can reference them by local filename without auth.
required_open_webui_version: 0.5.0
version: 2.7.0
"""

import asyncio
import base64
import io
import logging
import mimetypes
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


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


def _decode_base64_uri(url: str) -> tuple[bytes, str]:
    """Return (image_bytes, mime_type) from a data: URI."""
    header, encoded = url.split(",", 1)
    mime = header.split(";")[0].lstrip("data:") or "image/png"
    return base64.b64decode(encoded), mime


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


async def _upload_to_comfyui(base_url: str, api_key: str | None, img_bytes: bytes, mime: str) -> str | None:
    """Upload image bytes to ComfyUI; return the local filename."""
    import aiohttp
    from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
    from open_webui.utils.session_pool import get_session

    ext = _IMAGE_MIME_EXT.get(mime.lower()) or mimetypes.guess_extension(mime) or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    form = aiohttp.FormData()
    form.add_field("image", io.BytesIO(img_bytes), filename=filename, content_type=mime)
    form.add_field("type", "input")

    session = await get_session()
    url = f"{base_url.rstrip('/')}/api/upload/image"
    async with session.post(url, data=form, headers=headers, ssl=AIOHTTP_CLIENT_SESSION_SSL) as resp:
        resp.raise_for_status()
        result = await resp.json()
        return result.get("name") or filename


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Execution order. Lower values run first.")
        comfyui_base_url: Optional[str] = Field(
            default=None,
            description=(
                "ComfyUI base URL, e.g. http://akari:8188. When set, the "
                "filter uploads images to ComfyUI's /upload/image endpoint "
                "so workflows can find them by local filename."
            ),
        )
        comfyui_api_key: Optional[str] = Field(
            default=None,
            description="ComfyUI API key (Bearer token) if required.",
        )

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

        # Track file IDs from body["files"] so we can read them from disk
        # later for ComfyUI upload.
        uploaded_ids: list[str] = []
        has_uploaded = False

        # ── Step 1: handle body["files"] ────────────────────────────────────
        files: Optional[list] = body.get("files")
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    has_uploaded = True
                    fid = f.get("id", "")
                    if fid:
                        uploaded_ids.append(fid)
                    file_tags.append(_format_file_tag(f))
                else:
                    non_images.append(f)
            body["files"] = non_images if non_images else None

        # ── Step 2: strip image_url from message content ────────────────────
        pasted_bytes: list[tuple[bytes, str]] = []
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
                            file_tags.append(_format_file_tag({"url": purl, "type": "image"}))
                            try:
                                pasted_bytes.append(_decode_base64_uri(url))
                            except Exception:
                                pass
                        else:
                            file_tags.append(_format_file_tag({"url": "(base64 stripped)", "type": "image"}))
                    else:
                        file_tags.append(_format_file_tag({"url": url, "type": "image"}))
                msg["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        # ── Step 3: upload to ComfyUI if configured ─────────────────────────
        comfy_ok = bool(self.valves.comfyui_base_url)
        if comfy_ok:
            # 3a: pasted images
            for img_bytes, mime in pasted_bytes:
                try:
                    fname = await _upload_to_comfyui(
                        self.valves.comfyui_base_url, self.valves.comfyui_api_key, img_bytes, mime
                    )
                    if fname:
                        file_tags.append(_format_file_tag({"url": fname, "type": "comfyui", "name": fname}))
                except Exception as exc:
                    log.warning("ComfyUI upload (pasted) failed: %s", exc)

            # 3b: uploaded files — read from Open WebUI disk and re-upload
            if uploaded_ids:
                from open_webui.models.files import Files
                from open_webui.storage.provider import Storage

                import aiofiles

                for fid in uploaded_ids:
                    try:
                        rec = await Files.get_file_by_id(fid)
                        if not rec:
                            continue
                        fpath = await asyncio.to_thread(Storage.get_file, rec.path)
                        async with aiofiles.open(fpath, "rb") as fh:
                            img_bytes = await fh.read()
                        mime = rec.meta.get("content_type", "image/png")
                        fname = await _upload_to_comfyui(
                            self.valves.comfyui_base_url, self.valves.comfyui_api_key, img_bytes, mime
                        )
                        if fname:
                            file_tags.append(_format_file_tag({"url": fname, "type": "comfyui", "name": fname}))
                    except Exception as exc:
                        log.warning("ComfyUI upload (file %s) failed: %s", fid, exc)

        # ── Step 4: inject <attached_files> ─────────────────────────────────
        if file_tags and messages:
            ref = _build_attached_files(file_tags)
            if ref:
                _prepend_to_user_message(messages, ref)

        # ── Step 5: notify ─────────────────────────────────────────────────
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
