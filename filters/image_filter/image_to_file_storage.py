"""
title: Image to File Storage
author: pi-agent
description: Strips images from LLM context to prevent RAG and base64 bloat. Persists pasted images with content-hash dedup and injects a deduplicated <attached_files> block with absolute file URLs so tools (e.g. ComfyUI URL-loading nodes) can fetch the stored images. Pasted images are announced only in the turn they are pasted — later turns strip the re-hydrated history without re-announcing it.
required_open_webui_version: 0.5.0
version: 2.12.2
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

        # The LAST user message is the current turn's message — the only one
        # that can carry a *new* attachment.
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        # ── Step 1: handle body["files"] ────────────────────────────────────
        files: Optional[list] = body.get("files")
        # hash -> file id of images uploaded via `+` THIS turn, so pasted /
        # re-encoded copies of the same image reuse that file instead of
        # minting a duplicate UUID (the "model saw 2 images" bug).
        turn_hash_to_id: dict[str, str] = {}
        if isinstance(files, list):
            non_images = []
            for f in files:
                if _is_image_file(f):
                    _append_file_tag(file_tags, seen, f, base_url)
                    if f.get("id"):
                        h = await self._file_hash_of(f["id"])
                        if h:
                            turn_hash_to_id[h] = f["id"]
                else:
                    non_images.append(f)
            body["files"] = non_images if non_images else None

        # Native function calling: a `+` upload does NOT arrive via
        # body["files"] — the core middleware pops `files` from the payload
        # message and injects image_url parts into content, which
        # convert_url_images_to_base64() has already turned into base64 by
        # the time the filter runs. The file refs still live on the STORED
        # current user message (DB) — the same list the core's
        # add_file_context() reads later. Seed the this-turn hash map from
        # those refs too, so the base64 copy reuses the CURRENT upload's
        # file id instead of an older identical file found by the user-wide
        # lookup. Otherwise the filter tags one UUID and the core tags
        # another for the same image, and the pipe's UUID dedup cannot
        # collapse them — the model sees "two images" for one upload
        # (observed 2026-08-01: filter reused old copy 79cb1456..., core
        # tagged the current upload 76680237...).
        if last_user_idx is not None:
            refs = messages[last_user_idx].get("files") or []
            if not refs:
                refs = await self._current_turn_file_refs(__metadata__)
            for f in refs:
                if _is_image_file(f) and f.get("id"):
                    h = await self._file_hash_of(f["id"])
                    if h:
                        turn_hash_to_id.setdefault(h, f["id"])

        # ── Step 2: strip image_url from message content ────────────────────
        announced = 0
        stripped_historical = 0
        if messages:
            # Only the LAST user message can carry a *new* attachment for
            # this turn. Re-hydrated history (stored data: URIs / image_url
            # from earlier turns) is stripped but NOT re-announced: those
            # images were already tagged in their own turn, and re-tagging
            # them every turn would re-inject an ever-staler image forever
            # (wasted tokens, breaks prefix caching). The model retains
            # conversational memory of earlier attachments; if the user
            # needs the file again they re-attach it.
            for idx, msg in enumerate(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                announce = idx == last_user_idx
                new_content = []
                for item in content:
                    if not _is_image_url_block(item):
                        new_content.append(item)
                        continue
                    url = item.get("image_url", {}).get("url", "")
                    if announce:
                        announced += 1
                        if _is_base64_uri(url):
                            persisted = await self._persist_base64(
                                url, __request__, __metadata__, __user__,
                                known_hashes=turn_hash_to_id,
                            )
                            if persisted:
                                purl, fid = persisted
                                _append_file_tag(file_tags, seen, {"id": fid, "url": purl, "type": "image"}, base_url)
                            else:
                                file_tags.append(_format_file_tag({"url": "(base64 stripped)", "type": "image"}, base_url))
                        else:
                            _append_file_tag(file_tags, seen, {"url": url, "type": "image"}, base_url)
                    else:
                        # Historical re-hydration — stripped (keeps base64
                        # out of the LLM context) but not re-announced.
                        stripped_historical += 1
                msg["content"] = new_content if new_content else [{"type": "text", "text": ""}]

        log.info(
            "image_filter: announced %d image(s) from the current turn; "
            "stripped %d historical image block(s) without re-announcing "
            "(last user message idx=%s, user messages=%d)",
            announced,
            stripped_historical,
            last_user_idx,
            sum(1 for m in messages if m.get("role") == "user"),
        )

        # ── Step 3: inject <attached_files> ─────────────────────────────────
        if file_tags and messages:
            ref = _build_attached_files(file_tags)
            if ref:
                _prepend_to_user_message(messages, ref)
                log.info("image_filter: injected <attached_files> with %d tag(s)", len(file_tags))

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

    async def _file_hash_of(self, file_id: str) -> Optional[str]:
        """Best-effort: return the stored sha256 (`meta["file_hash"]`) of a
        file id, or None. Used to match this turn's `+` uploads against
        the `image_url` copies so the same file is reused instead of
        persisting a duplicate."""
        try:
            from open_webui.models.files import Files

            fobj = await Files.get_file_by_id(file_id)
            if fobj is not None:
                return (getattr(fobj, "meta", None) or {}).get("file_hash")
            # Fallback for versions where get_file_by_id is unavailable or
            # returns None: the metadata endpoint exposes the same row.
            meta = await Files.get_file_metadata_by_id(file_id)
            if meta is not None:
                return (getattr(meta, "meta", None) or {}).get("file_hash")
        except Exception as exc:
            log.warning("_file_hash_of failed for %s: %s", file_id, exc)
        return None

    async def _current_turn_file_refs(self, metadata) -> list:
        """Recover the current user message's file refs from the STORED
        chat message (native FC path).

        The core middleware pops `files` off the payload message before
        filter inlets run, but the DB row still carries them — the same
        list the core's `add_file_context()` reads later (which is why the
        core tags the current upload's file while the filter would tag an
        older identical copy). Returns [] when unavailable (fail-soft: the
        caller falls back to the user-wide content-hash lookup).
        """
        meta = metadata or {}
        chat_id = meta.get("chat_id")
        message_id = meta.get("message_id") or meta.get("user_message_id")
        if not chat_id or not message_id:
            return []
        try:
            from open_webui.models.chats import Chats

            msg = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            return (msg or {}).get("files", []) or []
        except Exception as exc:
            log.warning("_current_turn_file_refs failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    async def _persist_base64(self, url: str, request, metadata, user, known_hashes: Optional[dict] = None) -> Optional[tuple[str, str]]:
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
            # 1) Reuse a file uploaded via `+` THIS TURN with the same
            #    content (body["files"] refs) — avoids the duplicate-UUID
            #    bug that made the model see two images for one upload.
            if known_hashes and file_hash in known_hashes:
                fid = known_hashes[file_hash]
                await self._link_file_to_message(fid, metadata, user_model)
                log.info(
                    "image_filter: reused this-turn upload %s (content hash match)",
                    fid,
                )
                return (
                    request.app.url_path_for("get_file_content_by_id", id=fid),
                    fid,
                )

            # 2) Reuse any earlier file owned by the user with the same hash.
            existing = await self._find_file_by_hash(user_model.id, file_hash)
            if existing and existing.get("id"):
                await self._link_file_to_message(existing["id"], metadata, user_model)
                return (
                    request.app.url_path_for("get_file_content_by_id", id=existing["id"]),
                    existing["id"],
                )

            # 3) Miss: persist a new file.
            log.info(
                "image_filter: no content-hash match; persisting new file "
                "(sha256=%s...)",
                file_hash[:12],
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
                    log.info(
                        "image_filter: content-hash dedup hit for user %s -> file %s",
                        user_id,
                        f.id,
                    )
                    return {"id": f.id}
            log.info(
                "image_filter: content-hash dedup miss for user %s (sha256=%s...) — "
                "no stored meta['file_hash'] matched; persisting duplicate",
                user_id,
                file_hash[:12],
            )
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
