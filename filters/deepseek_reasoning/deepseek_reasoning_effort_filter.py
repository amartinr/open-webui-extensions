"""
title: DeepSeek Reasoning Effort Selector
author: pi-agent
description: Toggleable filter that lets users select "low", "high" or "max" reasoning effort for DeepSeek models. Shows a chip in the chat input bar; click to open the effort selector. When the user leaves the effort unset, the admin's default applies.
required_open_webui_version: 0.9.0
version: 1.2.0
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
        default_effort: Literal["low", "high", "max"] = Field(
            default="low",
            description="Default reasoning effort when the user hasn't picked one yet.",
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
        model_pattern: str = Field(
            default="deepseek",
            description=(
                "Case-insensitive model name filter. "
                "Only matching models get reasoning params. Default: 'deepseek'."
            ),
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
                "follow the admin's default_effort."
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

        # Resolve reasoning effort. The admin's default_effort is the
        # baseline; the user's per-chat choice only overrides it when set
        # explicitly (Open WebUI stores user valves as a partial dict, so an
        # unset field materializes to the UserValves default "").
        effort: str = self.valves.default_effort

        if __user__ and __user__.get("valves"):
            uv = __user__["valves"]
            if isinstance(uv, dict):
                user_effort = uv.get("reasoning_effort", "")
            else:
                user_effort = getattr(uv, "reasoning_effort", "")
            if user_effort in ("low", "high", "max"):
                effort = user_effort

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
