"""
title: DeepSeek Reasoning Effort Selector
author: pi-agent
description: Toggleable filter that lets users select "low", "high" or "max" reasoning effort for DeepSeek models. Admin defines a per-model default via the model_effort_map valve (JSON: model pattern -> effort); models not listed fall back to default_effort ("low"). User chip choice wins; otherwise the per-model default applies. No monkey-patching required.
required_open_webui_version: 0.9.0
version: 1.3.0
"""

import json
import logging
from pydantic import BaseModel, Field
from typing import Optional

log = logging.getLogger(__name__)

ALLOWED = ("low", "high", "max")


def _parse_effort_map(raw: str) -> list[tuple[str, str]]:
    """Parse JSON map into (pattern, effort) pairs, sorted by specificity (longest first)."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("model_effort_map: invalid JSON: %s", raw[:200])
        return []
    if not isinstance(data, dict):
        return []
    pairs = []
    for pattern, effort in data.items():
        if isinstance(effort, str) and effort in ALLOWED:
            pairs.append((str(pattern).strip().lower(), effort))
    # El patrón más específico (más largo) gana sobre subcadenas genéricas
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _resolve_model_effort(model_id: str, pairs: list[tuple[str, str]]) -> Optional[str]:
    model_lower = model_id.lower()
    for pattern, effort in pairs:
        if pattern and pattern in model_lower:
            return effort
    return None


class Filter:
    # Admin Valves (configured by admins in Functions management)
    class Valves(BaseModel):
        priority: int = Field(
            default=1,
            description="Filter execution order. Run after Thinking Default Off (priority 0).",
        )
        model_pattern: str = Field(
            default="deepseek",
            description=(
                "Case-insensitive model name filter. "
                "Only matching models get reasoning params. Default: 'deepseek'."
            ),
        )
        model_effort_map: str = Field(
            default='{"deepseek-v4-pro": "high", "deepseek-v4-flash": "low"}',
            description=(
                'JSON map: model ID or substring pattern -> default reasoning effort (low/high/max). '
                'The most specific (longest) matching pattern wins.'
            ),
        )
        default_effort: str = Field(
            default="low",
            description=(
                "Fallback reasoning effort for models NOT matched by model_effort_map. "
                "low is safe since DeepSeek 0731 exposes all three levels (low/high/max)."
            ),
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "low", "label": "low"},
                        {"value": "high", "label": "high"},
                        {"value": "max", "label": "max"},
                    ],
                }
            },
        )

    # User Valves (per-chat configurable by any user)
    class UserValves(BaseModel):
        # NOTE: this is a plain str with a select input, NOT a Literal.
        # The default "" is the "unset" state: Open WebUI only persists
        # valves the user explicitly set to Custom (the modal shows unset
        # fields as "Default" and omits them from the saved dict), so the
        # pydantic default is what unset users get. "" must therefore be
        # a legal value (a Literal would reject it and break the filter).
        reasoning_effort: str = Field(
            default="",
            description=(
                "Reasoning depth for this chat. Leave unset (Default) to "
                "follow the model's configured default (model_effort_map)."
            ),
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "low", "label": "low"},
                        {"value": "high", "label": "high"},
                        {"value": "max", "label": "max"},
                    ],
                }
            },
        )

    def __init__(self):
        self.valves = self.Valves()
        # Make the filter toggleable so users can enable/disable it per chat.
        # A chip appears in the chat input bar; clicking it opens the
        # UserValves modal to select the reasoning effort.
        self.toggle = True
        self.icon = "https://icons.getbootstrap.com/assets/icons/lightbulb.svg"

    # Inlet: modify the request body BEFORE it reaches the LLM API
    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> dict:
        model: str = body.get("model", "")

        # Only apply to models matching the configured pattern (e.g. "deepseek")
        if self.valves.model_pattern.lower() not in model.lower():
            return body

        pairs = _parse_effort_map(self.valves.model_effort_map)

        # 1. Elección explícita del usuario (chip) gana
        effort: Optional[str] = None
        source = "fallback"
        if __user__ and __user__.get("valves"):
            uv = __user__["valves"]
            if isinstance(uv, dict):
                user_effort = uv.get("reasoning_effort", "")
            else:
                user_effort = getattr(uv, "reasoning_effort", "")
            if user_effort in ALLOWED:
                effort = user_effort
                source = "usuario"

        # 2. Default del modelo (mapa por patrón)
        if effort is None:
            effort = _resolve_model_effort(model, pairs)
            if effort is not None:
                source = "modelo (map)"

        # 3. Fallback global
        if effort is None:
            effort = self.valves.default_effort

        # Strip any pre-existing values so this filter's values always take
        # precedence. At the DeepSeek API level, "thinking" is a top-level
        # parameter, not nested inside extra_body.
        body.pop("reasoning_effort", None)
        body.pop("thinking", None)

        # Inject the resolved values fresh.
        body["reasoning_effort"] = effort
        body["thinking"] = {"type": "enabled"}

        # Show a brief status notification in the chat UI
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"🧠 Reasoning effort ({effort}, {source})",
                        "done": True,
                        "hidden": False,
                    },
                }
            )

        return body
