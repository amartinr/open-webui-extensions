"""
title: Bifrost reasoning_content fix
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: >
  Fixes Bifrost's non-standard response format by converting
  'reasoning' + 'reasoning_details' back to proper 'reasoning_content'
  using the Open WebUI >= 0.11 per-event stream() filter API.
  Detection in stream() is content-driven (auto-selective) so it works
  regardless of the event model id.
  Also cleans up historical messages on the way IN to prevent
  stale non-standard fields from being re-sent, and (v3.2.0) forces
  'reasoning_content' on every assistant message once the history
  contains a tool call or the request carries tools — DeepSeek drops
  reasoning on the next turn otherwise (same fix as the
  pi-bifrost-reasoning-fix pi extension). Since v3.4.0 the stream()
  conversion leaves reasoning_details in place so Open WebUI can store
  and replay the real reasoning text.
required_open_webui_version: 0.11.0
version: 3.5.1
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
    """Check if a message still carries non-standard Bifrost fields.

    Mirrors pi-bifrost-reasoning-fix: a `reasoning` field counts even when
    empty (it must be replayed as reasoning_content, and an empty string
    is what DeepSeek accepts), and `reasoning_details` counts whenever it
    is a list (even an empty one).
    """
    return "reasoning" in msg and isinstance(msg["reasoning"], str) or isinstance(
        msg.get("reasoning_details"), list
    )


def _normalize_assistant_message(msg: dict) -> dict:
    """Normalize an assistant message to remove any Bifrost residue.

    Mirrors pi-bifrost-reasoning-fix's normalizeAssistant(): plain-text
    `reasoning` is the source of truth (both fields carry the same
    incremental text upstream), and `reasoning_details` is only used as a
    fallback when no text landed in reasoning_content. Both non-standard
    fields are always removed.
    """
    msg = dict(msg)  # shallow copy to avoid mutating the original

    reasoning = msg.pop("reasoning", None)
    if isinstance(reasoning, str) and "reasoning_content" not in msg:
        msg["reasoning_content"] = reasoning

    reasoning_details = msg.pop("reasoning_details", None)
    if reasoning_details and (
        not isinstance(msg.get("reasoning_content"), str)
        or msg["reasoning_content"] == ""
    ):
        text = _extract_reasoning_text(reasoning_details)
        if text:
            msg["reasoning_content"] = text

    return msg


def _history_has_tool_calls(messages: list) -> bool:
    """True when the history contains an assistant message with tool calls.

    Once a tool call has happened, DeepSeek requires `reasoning_content` on
    every assistant message of every subsequent request — regardless of
    whether that request still ships `tools` (Open WebUI re-sends history
    after a tool call without the tool definitions).
    """
    return any(
        isinstance(msg, dict)
        and msg.get("role") == "assistant"
        and isinstance(msg.get("tool_calls"), list)
        and len(msg["tool_calls"]) > 0
        for msg in messages
    )


def _force_reasoning_content_on_tools(messages: list) -> None:
    """Guarantee every assistant message carries `reasoning_content`.

    DeepSeek stops reasoning (silently, no error) when a tool-calling
    history replays an assistant message without `reasoning_content`. An
    empty string is enough — the field just has to be present. This is the
    same forcing step as pi-bifrost-reasoning-fix's normalizePayload().
    It is deterministic and monotonic (a tool call once present stays in
    the history), so the provider prefix cache is not invalidated.
    """
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            if not isinstance(msg.get("reasoning_content"), str):
                msg["reasoning_content"] = ""


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

    `reasoning_details` are ALWAYS removed from the delta (v3.5.0):
    Open WebUI's stream handler suppresses the frontend
    `response.reasoning_text.delta` event whenever a delta carries
    reasoning_details (it sets data=None and only saves to DB), so
    keeping them broke the streaming display. The display text is already
    in `reasoning_content`; the history-replay of the real text is not
    possible from the stream side without breaking SSE, so the inlet/pipe
    forcing (v3.2.0+) covers the replay requirement instead.
    """
    if not isinstance(delta, dict):
        return delta

    # Bifrost core >= 1.8.0 emits each reasoning fragment in THREE fields at
    # once: delta.reasoning, delta.reasoning_content and
    # delta.reasoning_details (all the same text). When reasoning_content is
    # already populated by the gateway, appending would double the fragment
    # (and across pipe + filter it quadruples). Only synthesize the field
    # when the gateway did not provide it (Bifrost < 1.8.0), and always strip
    # the redundant fields.
    existing = delta.get("reasoning_content", "")
    existing = existing if isinstance(existing, str) else ""

    # Variant A: delta.reasoning (incremental plain text). Only consume it
    # as the source of truth when it carries text — an empty-string opening
    # event must still fall back to the details.
    if "reasoning" in delta:
        reasoning = delta.pop("reasoning")
        if isinstance(reasoning, str) and reasoning and not existing:
            delta["reasoning_content"] = reasoning

    # Variant B: delta.reasoning_details (list of blocks) -> fallback only.
    if not existing and not delta.get("reasoning_content"):
        text = _extract_reasoning_text(delta.get("reasoning_details"))
        if text:
            delta["reasoning_content"] = text

    # Never let Bifrost's non-standard fields reach Open WebUI: the handler
    # uses their presence to decide display events (see docstring).
    delta.pop("reasoning", None)
    delta.pop("reasoning_details", None)

    return delta


def _event_has_bifrost(event: dict) -> bool:
    """True if any delta still carries non-standard Bifrost fields."""
    choices = event.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict) and _has_bifrost_residue(delta):
                    return True
    return False


def _usage_has_reasoning_tokens(usage) -> bool:
    """True if usage (dict) still contains non-standard reasoning_tokens."""
    if not isinstance(usage, dict):
        return False
    for details_key in ("completion_tokens_details", "audio_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict) and "reasoning_tokens" in details:
            return True
    return False


def _fix_event(event: dict, strip_reasoning: bool = False) -> dict:
    """Normalize a full stream event (OpenAI shape).

    Content-driven (auto-selective): only touches an event when it actually
    carries Bifrost residue. This is what makes stream() work regardless of
    the event['model'] value, which is not reliably the model ID Open WebUI
    exposes (so a name/prefix gate would silently skip every chunk and the
    model would appear to 'stop reasoning').

    The final streaming chunk carries top-level usage with reasoning_tokens.
    By default these are preserved (Open WebUI and token-usage filters read
    them); only strip them when the caller opts in via strip_reasoning.
    """
    if _event_has_bifrost(event):
        choices = event.get("choices")
        for choice in choices:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    _fix_delta(delta)
    if strip_reasoning and _usage_has_reasoning_tokens(event.get("usage")):
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
        strip_reasoning_tokens: bool = Field(
            default=False,
            description=(
                "Remove non-standard 'reasoning_tokens' from usage. Default False: keep them "
                "so token-usage display filters (e.g. Token Usage Display) and Open WebUI can "
                "report reasoning tokens. Set True only when a strict OpenAI client rejects them."
            ),
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
        are not re-sent to the upstream API, and force
        `reasoning_content` on every assistant once tool-calling is in
        scope (see _force_reasoning_content_on_tools).

        Without the forcing step, DeepSeek silently drops reasoning on
        any turn whose replayed history contains an assistant tool call
        but an assistant message missing `reasoning_content` — that is
        the default Open WebUI replay shape (it rebuilds assistant
        messages from stored output items and omits the field for
        OpenAI-compatible providers). The loss looks spurious and
        self-heals only when history compaction removes the tool call.
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

        has_tools = isinstance(body.get("tools"), list) and len(body["tools"]) > 0
        if has_tools or _history_has_tool_calls(cleaned):
            _force_reasoning_content_on_tools(cleaned)
            logger.info(
                "bf-reasoning: inlet forced reasoning_content on tool-calling "
                "history (model=%s, tools=%s)",
                model.get("id", ""),
                has_tools,
            )
        else:
            logger.info(
                "bf-reasoning: inlet cleanup only (model=%s, no tool-calling "
                "scope)",
                model.get("id", ""),
            )

        return body

    async def stream(self, event: dict) -> dict:
        """Per-stream-event fix (Open WebUI >= 0.11 stream contract).

        Content-driven: _fix_event only touches chunks that actually carry
        Bifrost residue, so we don't gate on event['model'] (which is not
        reliably the Open WebUI model id) — that name/prefix gate was the
        reason the model appeared to 'stop reasoning' when the field value
        didn't match the valve prefixes.
        """
        try:
            return _fix_event(event, strip_reasoning=self.valves.strip_reasoning_tokens)
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
            if usage is not None and self.valves.strip_reasoning_tokens:
                body["usage"] = _strip_reasoning_tokens(usage)
        return body
