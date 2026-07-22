"""
title: Bifrost reasoning_content fix
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: >
  Fixes Bifrost's non-standard response format by converting
  'reasoning' + 'reasoning_details' back to proper 'reasoning_content'.
  Also cleans up historical messages on the way IN to prevent
  stale non-standard fields from being re-sent.
required_open_webui_version: 0.9.0
version: 2.0.0
"""

import json
import logging
from typing import Optional, Union
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  HELPERS (apply to both inlet and outlet)
# ──────────────────────────────────────────────

import re

_REASONING_TAG_RE = re.compile(
    r"<reasoning\b[^>]*>(.*?)</reasoning\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _get_content_text(content) -> str:
    """Extract plain text from content regardless of type.

    OpenAI content can be a plain string, a list of multimodal parts,
    or (in some providers) a dict.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", content.get("content", "")))
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item.get("content", ""))))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _has_bifrost_residue(msg: dict) -> bool:
    """Check if a message still carries non-standard Bifrost fields."""
    return bool(msg.get("reasoning") or msg.get("reasoning_details"))


def _extract_reasoning_from_content(content: str) -> tuple[str, str]:
    """Extract <reasoning>...</reasoning> XML tags from content text.

    Workaround for Bifrost #974 where reasoning deltas are merged into
    delta.content as XML tags instead of being placed in a dedicated field.

    Returns (cleaned_content, reasoning_text).
    """
    if not content:
        return content, ""

    reasoning_parts = []

    def _capture(m: re.Match) -> str:
        reasoning_parts.append(m.group(1))
        return ""

    cleaned = _REASONING_TAG_RE.sub(_capture, content)
    return cleaned, "".join(reasoning_parts)


def _normalize_assistant_message(msg: dict) -> dict:
    """
    Normalize an assistant message to remove any Bifrost residue:
      - reasoning_details → reconstruct reasoning_content from its text blocks
      - reasoning          → reasoning_content
    """
    msg = dict(msg)  # shallow copy to avoid mutating the original

    # 1. Reconstruct reasoning_content from reasoning_details if present
    reasoning_details = msg.pop("reasoning_details", None)
    if reasoning_details and isinstance(reasoning_details, list):
        texts = []
        for item in reasoning_details:
            if isinstance(item, dict) and item.get("type") == "reasoning.text":
                texts.append(item.get("text", ""))
        if texts and not msg.get("reasoning_content"):
            msg["reasoning_content"] = "".join(texts)

    # 2. If Bifrost put the field 'reasoning' instead of 'reasoning_content'
    if "reasoning" in msg and "reasoning_content" not in msg:
        msg["reasoning_content"] = msg.pop("reasoning")
    elif "reasoning" in msg:
        # Both coexist → keep reasoning_content and remove reasoning
        msg.pop("reasoning")

    return msg


def _fix_chunk(data: dict) -> dict:
    """Fix a response chunk (streaming or non-streaming)."""
    choices = data.get("choices")
    if not choices or not isinstance(choices, list):
        return data

    for choice in choices:
        # --- Streaming: delta ---
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if "reasoning" in delta:
                delta["reasoning_content"] = delta.pop("reasoning")
            delta.pop("reasoning_details", None)

            # Workaround for Bifrost #974: reasoning merged into
            # delta.content wrapped in <reasoning> tags.
            d_content = delta.get("content", "")
            if isinstance(d_content, str) and "<reasoning" in d_content.lower():
                cleaned, extracted = _extract_reasoning_from_content(d_content)
                if extracted:
                    delta["content"] = cleaned
                    if not delta.get("reasoning_content"):
                        delta["reasoning_content"] = extracted

        # --- Non-streaming: message ---
        msg = choice.get("message")
        if isinstance(msg, dict):
            _normalize_assistant_message(msg)

    return data


def _fix_sse_line(payload: str) -> str:
    payload = payload.strip()
    if not payload or payload == "[DONE]":
        return payload
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse SSE payload: %s — %s", exc, payload[:200])
        return payload
    data = _fix_chunk(data)
    return json.dumps(data, ensure_ascii=False)


def _clean_messages(body: dict) -> dict:
    """
    Walk the body 'messages' array and normalize any assistant
    message carrying non-standard Bifrost fields from previous turns.
    """
    body = dict(body)
    messages = body.get("messages", [])
    if not messages:
        return body

    cleaned = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            if _has_bifrost_residue(msg):
                msg = _normalize_assistant_message(msg)
        cleaned.append(msg)

    body["messages"] = cleaned
    return body


# ──────────────────────────────────────────────
#  FILTER
# ──────────────────────────────────────────────


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Lower runs first.")
        model_prefixes: str = Field(
            default="deepseek",
            description="Comma-separated model ID prefixes that route through Bifrost.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __model__: Optional[dict] = None) -> dict:
        """
        On the way in (Open WebUI → provider): clean historical
        messages so stale reasoning_details / broken Bifrost fields
        are not re-sent to the upstream API.
        """
        model = __model__ or {}
        model_id = model.get("id", "")

        prefixes = {
            p.strip() for p in self.valves.model_prefixes.split(",") if p.strip()
        }
        if not any(model_id.startswith(p) for p in prefixes):
            return body

        return _clean_messages(body)

    async def outlet(
        self, body, __model__: Optional[dict] = None, **kwargs
    ) -> Union[dict, "StreamingResponse"]:
        """
        On the way out (provider → Open WebUI): convert non-standard
        Bifrost fields back to the standard format.
        """
        from starlette.responses import StreamingResponse

        model = __model__ or {}
        model_id = model.get("id", "")

        prefixes = {
            p.strip() for p in self.valves.model_prefixes.split(",") if p.strip()
        }
        if not any(model_id.startswith(p) for p in prefixes):
            return body

        # --- Streaming ---
        if isinstance(body, StreamingResponse):
            return self._wrap_stream(body)

        # --- Non-streaming (dict) ---
        if isinstance(body, dict):
            return _fix_non_streaming(body)

        return body

    def _wrap_stream(self, response: StreamingResponse) -> StreamingResponse:
        from starlette.responses import StreamingResponse

        async def patched_generator():
            try:
                async for raw_chunk in response.body_iterator:
                    chunk = (
                        raw_chunk.decode("utf-8", errors="replace")
                        if isinstance(raw_chunk, bytes)
                        else raw_chunk
                    )

                    lines = chunk.split("\n")
                    out_lines = []
                    for line in lines:
                        if line.startswith("data: "):
                            payload = line[6:]
                            fixed = _fix_sse_line(payload)
                            out_lines.append(f"data: {fixed}")
                        else:
                            out_lines.append(line)

                    yield "".join(out_lines).encode("utf-8")
            except Exception:
                logger.exception("Unhandled error in Bifrost reasoning filter stream — pasando chunk original")
                yield raw_chunk if isinstance(raw_chunk, bytes) else str(raw_chunk).encode("utf-8")

        return StreamingResponse(
            patched_generator(),
            media_type=response.media_type,
            headers=dict(response.headers),
            status_code=response.status_code,
        )


def _strip_reasoning_tokens(usage: dict) -> dict:
    """Remove Bifrost-injected reasoning_tokens from usage statistics.

    These are not part of the standard OpenAI Chat Completion schema.
    """
    if not isinstance(usage, dict):
        return usage
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        details.pop("reasoning_tokens", None)
        if not details:
            usage.pop("completion_tokens_details", None)
    audio_details = usage.get("audio_tokens_details")
    if isinstance(audio_details, dict):
        audio_details.pop("reasoning_tokens", None)
    return usage


def _fix_non_streaming(body: dict) -> dict:
    """Fix a complete non-streaming response."""
    body = _fix_chunk(body)
    usage = body.get("usage")
    if usage is not None:
        body["usage"] = _strip_reasoning_tokens(usage)
    return body
