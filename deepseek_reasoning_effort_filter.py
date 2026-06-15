"""
title: DeepSeek Reasoning Effort Selector
author: pi-agent
description: Lets users choose between "high" and "max" reasoning effort when chatting with DeepSeek models. Injects reasoning_effort and thinking parameters into the request body before it reaches the DeepSeek API. Toggleable via a chip in the chat input bar; clicking the chip opens a modal to select the effort level.
required_open_webui_version: 0.9.0
version: 1.0.0
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional


class Filter:
    # Admin Valves (configured by admins in Functions management)
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Execution order of this filter. Lower values run first.",
        )
        default_effort: Literal["high", "max"] = Field(
            default="high",
            description="Default reasoning effort when the user hasn't picked one yet.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "high", "label": "High — default, faster responses"},
                        {"value": "max", "label": "Max  — deepest reasoning, slower"},
                    ],
                }
            },
        )
        model_pattern: str = Field(
            default="deepseek",
            description=(
                "Case-insensitive substring to match against the model name. "
                "Only requests to models whose name contains this pattern will "
                "have the parameters injected. Default: 'deepseek'."
            ),
        )

    # User Valves (per-chat configurable by any user)
    class UserValves(BaseModel):
        reasoning_effort: Literal["high", "max"] = Field(
            default="high",
            description="Reasoning depth for DeepSeek models in this chat.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "high", "label": "High — default, faster"},
                        {"value": "max", "label": "Max  — deepest reasoning"},
                    ],
                }
            },
        )
        enable_thinking: bool = Field(
            default=True,
            description=(
                "Enable thinking mode (required for reasoning_effort to take effect). "
                "Turn off to use the non-reasoning variant of the model."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        # Make the filter toggleable so users can enable/disable it per chat.
        # A chip appears in the chat input bar; clicking it opens the
        # UserValves modal (effort + thinking toggle).
        self.toggle = True
        self.icon = "🧠"

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
        enable_thinking: bool = True

        if __user__ and __user__.get("valves"):
            uv = __user__["valves"]
            # Prefer the user's per-chat choice when available
            effort = getattr(uv, "reasoning_effort", effort)
            enable_thinking = getattr(uv, "enable_thinking", True)

        # Strip any pre-existing values (e.g. from Open WebUI or other
        # filters) so this filter's values always take precedence.
        body.pop("reasoning_effort", None)
        extra_body: dict = body.get("extra_body", {})
        if isinstance(extra_body, dict):
            extra_body.pop("thinking", None)

        # Inject the resolved values fresh
        body["reasoning_effort"] = effort
        body["extra_body"] = extra_body
        body["extra_body"]["thinking"] = {
            "type": "enabled" if enable_thinking else "disabled"
        }

        # Show a brief status notification in the chat UI
        if __event_emitter__:
            mode = f"{effort.upper()}" if enable_thinking else "off"
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": f"🧠 DeepSeek reasoning: {mode}",
                    "done": True,
                    "hidden": False,
                },
            })

        return body
