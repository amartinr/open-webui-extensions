"""
title: DeepSeek Thinking Default Off
author: pi-agent
description: Global filter that explicitly sets thinking mode to "disabled" for DeepSeek models by default. Prevents DeepSeek from using its default thinking mode (which is ON). When the toggleable "DeepSeek Reasoning Effort Selector" filter is activated by the user, it overrides this to enable thinking + reasoning effort. This filter is always-on (no user chip) and runs first (priority 0).
required_open_webui_version: 0.9.0
version: 1.0.0
"""

from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    # Admin Valves (configured by admins in Functions management)
    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Execution order. Lower values run first. Should run before the Reasoning Effort Selector (priority 1).",
        )
        model_pattern: str = Field(
            default="deepseek",
            description=(
                "Case-insensitive substring to match against the model name. "
                "Only requests to models whose name contains this pattern will "
                "have thinking disabled. Default: 'deepseek'."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        # This filter is NOT toggleable — it runs as a global always-on filter
        # so DeepSeek thinking is disabled by default for every request.
        # self.toggle is NOT set, meaning the filter is always active
        # when assigned to a model or as a global filter.
        self.icon = "🤫"

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

        # Strip any pre-existing "thinking" so this filter's value always
        # takes precedence.  At the DeepSeek API level, "thinking" is a
        # top-level parameter, not nested inside extra_body.
        body.pop("thinking", None)
        body["thinking"] = {"type": "disabled"}

        # Do NOT set reasoning_effort — that is handled by the
        # toggleable Reasoning Effort Selector filter (if enabled).

        return body
