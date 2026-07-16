"""
title: DeepSeek Reasoning Effort Selector
author: pi-agent
description: Toggleable filter that lets users select "high" or "max" reasoning effort for DeepSeek models. Shows a chip in the chat input bar; click to open the effort selector.
required_open_webui_version: 0.9.0
version: 1.0.6
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional


class Filter:
    # Admin Valves (configured by admins in Functions management)
    class Valves(BaseModel):
        priority: int = Field(
            default=1,
            description="Filter execution order. Run after Thinking Default Off (priority 0).",
        )
        default_effort: Literal["high", "max"] = Field(
            default="high",
            description="Default reasoning effort when the user hasn't picked one yet.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "high", "label": "high"},
                        {"value": "max", "label": "max"},
                    ],
                }
            },
        )
        model_pattern: str = Field(
            default="deepseek",
            description=(
                "Case-insensitive model name filter. "
                "Only matching models get reasoning params. Default: 'deepseek'."
            ),
        )

    # User Valves (per-chat configurable by any user)
    class UserValves(BaseModel):
        reasoning_effort: Literal["high", "max"] = Field(
            default="high",
            description="Reasoning depth for this chat.",
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

        # Resolve reasoning effort
        effort: str = self.valves.default_effort

        if __user__ and __user__.get("valves"):
            uv = __user__["valves"]
            # Prefer the user's per-chat choice when available
            effort = getattr(uv, "reasoning_effort", effort)

        # Strip any pre-existing values (e.g. from DeepSeek Thinking Default
        # Off filter, workspace params, or Open WebUI) so this filter's values
        # always take precedence.  At the DeepSeek API level, "thinking" is a
        # top-level parameter, not nested inside extra_body.
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
                        "description": f"🤔 Reasoning effort ({effort})",
                        "done": True,
                        "hidden": False,
                    },
                }
            )

        return body
