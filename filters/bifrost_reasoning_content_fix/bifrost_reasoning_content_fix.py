"""
title: Bifrost reasoning_content fix
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: >
  Fixes Bifrost's non-standard response format by converting
  'reasoning' + 'reasoning_details' back to proper 'reasoning_content'
  using the Open WebUI >= 0.11 per-event stream() filter API.
  Also cleans up historical messages on the way IN to prevent
  stale non-standard fields from being re-sent.
required_open_webui_version: 0.11.0
version: 3.0.0
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  HELPERS (apply to inlet, outlet and stream)
# ──────────────────────────────────────────────


def _extract_reasoning_text(details) -> str:
    """reasoning_details is a list of blocks {type: 'reasoning.text', text: ...}."""
    if not isinstance(details, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in details
        if isinstance(item, dict) and item.get("type") == "reasoning.text"
    )


def _has_bifrost_residue(msg: dict) -> bool:
    """Check if a message still carries non-standard Bifrost fields."""
    return bool(msg.get("reasoning") or msg.get("reasoning_details"))


def _normalize_assistant_message(msg: dict) -> dict:
    """Normalize an assistant message to remove any Bifrost residue."""
    msg = dict(msg)  # shallow copy to avoid mutating the original

    reasoning_details = msg.pop("reasoning_details", None)
    if reasoning_details:
        text = _extract_reasoning_text(reasoning_details)
        if text and not msg.get("reasoning_content"):
            msg["reasoning_content"] = text

    if "reasoning" in msg and "reasoning_content" not in msg:
        msg["reasoning_content"] = msg.pop("reasoning")
    elif "reasoning" in msg:
        msg.pop("reasoning")

    return msg


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


# ──────────────────────────────────────────────
#  STREAM HELPERS (Open WebUI >= 0.11 event contract)
# ──────────────────────────────────────────────


def _fix_delta(delta: dict) -> dict:
    """Normalize a streaming delta in place.

    Bifrost sends each reasoning fragment duplicated in BOTH
    delta.reasoning (incremental plain text) and delta.reasoning_details
    (list of blocks). To avoid double-appending we use delta.reasoning as
    the primary source and only fall back to reasoning_details when
    reasoning carried no text (Bifrost #974 drops delta.reasoning for some
    providers). Keep the text even when it only arrives via details — this
    is the piece that made the model 'stop reasoning' when the old filter
    discarded it.
    """
    if not isinstance(delta, dict):
        return delta

    used = False

    # Variant A: delta.reasoning (incremental plain text)
    if "reasoning" in delta:
        reasoning = delta.pop("reasoning")
        used = True  # field present; we consume it as the source of truth
        if isinstance(reasoning, str) and reasoning:
            existing = delta.get("reasoning_content", "")
            existing = existing if isinstance(existing, str) else ""
            delta["reasoning_content"] = existing + reasoning

    # Variant B: delta.reasoning_details (list of blocks) -> fallback only.
    if not used:
        details = delta.pop("reasoning_details", None)
        if details:
            text = _extract_reasoning_text(details)
            if text:
                existing = delta.get("reasoning_content", "")
                existing = existing if isinstance(existing, str) else ""
                delta["reasoning_content"] = existing + text
    else:
        # reasoning already consumed; drop the redundant details payload.
        delta.pop("reasoning_details", None)

    return delta


def _fix_event(event: dict) -> dict:
    """Normalize a full stream event (OpenAI shape)."""
    choices = event.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    _fix_delta(delta)
    # The final streaming chunk carries top-level usage with reasoning_tokens.
    if event.get("usage"):
        event["usage"] = _strip_reasoning_tokens(event["usage"])
    return event


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

    def _targets(self, model_id: str) -> bool:
        prefixes = {
            p.strip() for p in self.valves.model_prefixes.split(",") if p.strip()
        }
        return any(model_id.startswith(p) for p in prefixes)

    async def inlet(self, body: dict, __model__: Optional[dict] = None) -> dict:
        """
        On the way in (Open WebUI → provider): clean historical
        messages so stale reasoning_details / broken Bifrost fields
        are not re-sent to the upstream API.
        """
        model = __model__ or {}
        if not self._targets(model.get("id", "")):
            return body

        messages = body.get("messages", [])
        cleaned = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                if _has_bifrost_residue(msg):
                    msg = _normalize_assistant_message(msg)
            cleaned.append(msg)
        body["messages"] = cleaned
        return body

    async def stream(self, event: dict) -> dict:
        """Per-stream-event fix (Open WebUI >= 0.11 stream contract).

        event arrives already parsed as a dict; mutate and return it.
        """
        try:
            # Prefer the event's own model id; fall back to the valve prefixes.
            if not self._targets(event.get("model", "")):
                return event
            return _fix_event(event)
        except Exception:
            logger.exception("Error fixing Bifrost event - passing through unchanged")
            return event

    async def outlet(
        self, body, __model__: Optional[dict] = None, **kwargs
    ) -> dict:
        """
        Only NON-streaming responses here (dict). Streaming is handled
        by stream() in Open WebUI >= 0.11.
        """
        model = __model__ or {}
        if not self._targets(model.get("id", "")):
            return body

        if isinstance(body, dict):
            choices = body.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
                        choice["message"] = _normalize_assistant_message(choice["message"])
            usage = body.get("usage")
            if usage is not None:
                body["usage"] = _strip_reasoning_tokens(usage)
        return body
