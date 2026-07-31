"""
title: Image to File Storage
author: pi-agent
description: Strips images from LLM context to prevent RAG and base64 bloat. Persists pasted images with content-hash dedup and injects a deduplicated <attached_files> block with absolute file URLs so tools (e.g. ComfyUI URL-loading nodes) can fetch the stored images.
required_open_webui_version: 0.5.0
version: 2.11.0
"""

import base64
import hashlib
import logging
import re
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


def _decode_base64_uri(url: str) -> Optional[tuple[bytes, str]]:
    """Decode a `data:image/*;base64,...` URI into (raw bytes, content_type).

    The hash that Open WebUI stores in `files.meta["file_hash"]` is computed
    over the decoded bytes (what actually lands on disk), not over the base64
    string — so dedup must decode first, exactly once.
    """
    if not (url.startswith("data:image/") and ";base64," in url):
        return None
    header, b64 = url.split(",", 1)
    content_type = header.split(";", 1)[0].removeprefix("data:")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    return raw, content_type


_FILES_URL_RE = re.compile(r"/api/v1/files/([^/]+)/content")


def _file_id_from_url(url: str) -> Optional[str]:
    """Extract the file id from a (relative or absolute) file content URL
    (`/api/v1/files/{id}/content`), or None."""
    m = _FILES_URL_RE.search(url or "")
    return m.group(1) if m else None


def _file_dedup_key(file: dict) -> str:
    """Canonical key for deduplicating <file> tags within one request.

    The same underlying file can be referenced from different sources in
    one payload: `body["files"]` (by id), a `/api/v1/files/{id}/content`
    URL (relative or absolute), or an external URL. The key collapses all
    of those to one tag. Returns "" for synthetic tags (e.g. the
    "(base64 stripped)" placeholder), which must never be deduplicated.
    """
    file_id = file.get("id")
    if file_id:
        return f"id:{file_id}"
    url = file.get("url", "")
    if not url or url == "(base64 stripped)":
        return ""
    from_url = _file_id_from_url(url)
    if from_url:
        return f"id:{from_url}"
    return f"url:{url}"


def _append_file_tag(file_tags: list[str], seen: set[str], file: dict, base_url: str = "") -> None:
    """Append a <file> tag, skipping it when the same file (by id or file
    URL) was already tagged in this request."""
    key = _file_dedup_key(file)
    if key:
        if key in seen:
            return
        seen.add(key)
    file_tags.append(_format_file_tag(file, base_url))


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
        seen: set[str] = set()
        base_url = await _get_public_base_url(__request__) if __request__ is not None else ""

        # ── Step 1: handle body["files"] ────────────────────────────────────
        files: Optional[list] = body.get("files")
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    _append_file_tag(file_tags, seen, f, base_url)
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
                    if _is_base64_uri(url):
                        persisted = await self._persist_base64(url, __request__, __metadata__, __user__)
                        if persisted:
                            purl, fid = persisted
                            _append_file_tag(file_tags, seen, {"id": fid, "url": purl, "type": "image"}, base_url)
                        else:
                            file_tags.append(_format_file_tag({"url": "(base64 stripped)", "type": "image"}, base_url))
                    else:
                        _append_file_tag(file_tags, seen, {"url": url, "type": "image"}, base_url)
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
                    "data": {"description": f"Prepared {len(file_tags)} image(s). Removed from context.", "done": True},
                })
            except Exception:
                pass

        return body

    # ------------------------------------------------------------------
    async def _persist_base64(self, url: str, request, metadata, user) -> Optional[tuple[str, str]]:
        """Persist a pasted image, reusing an existing file when the same
        content is already stored for this user.

        The `data:` URI is decoded exactly once. The raw bytes are hashed
        with sha256 — the same digest `upload_file_handler` stores in
        `files.meta["file_hash"]` — so an existing file can be looked up
        and reused instead of writing a new one. On a miss, the decoded
        bytes go straight to `upload_image()` (no second decode). Returns
        a `(relative file URL, file id)` pair, or None on failure.
        """
        decoded = _decode_base64_uri(url)
        if decoded is None:
            return None
        raw, content_type = decoded
        file_hash = hashlib.sha256(raw).hexdigest()

        user_model = _ensure_user_model(user)
        if user_model is None:
            return None

        try:
            existing = await self._find_file_by_hash(user_model.id, file_hash)
            if existing and existing.get("id"):
                await self._link_file_to_message(existing["id"], metadata, user_model)
                return (
                    request.app.url_path_for("get_file_content_by_id", id=existing["id"]),
                    existing["id"],
                )

            from open_webui.routers.images import upload_image

            meta = {
                "chat_id": (metadata or {}).get("chat_id"),
                "message_id": (metadata or {}).get("message_id"),
                "session_id": (metadata or {}).get("session_id"),
            }
            file_item, image_url = await upload_image(request, raw, content_type, meta, user_model)
            return image_url, file_item.id
        except Exception as exc:
            log.warning("_persist_base64 failed: %s", exc)
            return None

    async def _find_file_by_hash(self, user_id: str, file_hash: str) -> Optional[dict]:
        """Return the first file owned by `user_id` whose
        `meta["file_hash"]` matches the given digest.

        Best-effort: on any error it returns None and the caller falls
        back to persisting a new file. No index exists on
        `meta["file_hash"]`, so concurrent requests can still double-
        insert (TOCTOU) — worst case equals today's behavior (one extra
        file row), it never reuses another user's file because the
        lookup is scoped by `user_id`.
        """
        try:
            from open_webui.models.files import Files

            files = await Files.get_files_by_user_id(user_id)
            for f in files:
                if (f.meta or {}).get("file_hash") == file_hash:
                    return {"id": f.id}
        except Exception as exc:
            log.warning("_find_file_by_hash failed: %s", exc)
        return None

    async def _link_file_to_message(self, file_id: str, metadata, user_model) -> None:
        """Mirror `upload_image()`: link the (possibly reused) file to the
        chat message so the association matches what a fresh persist
        would have created. Best-effort — a linking failure must not
        prevent the URL from being returned.
        """
        chat_id = (metadata or {}).get("chat_id")
        message_id = (metadata or {}).get("message_id")
        if not chat_id or not message_id:
            return
        try:
            from open_webui.models.chats import Chats

            await Chats.insert_chat_files(
                chat_id=chat_id,
                message_id=message_id,
                file_ids=[file_id],
                user_id=user_model.id,
            )
        except Exception as exc:
            log.warning("_link_file_to_message failed: %s", exc)
