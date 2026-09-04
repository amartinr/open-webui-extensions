"""
title: Payload Inspector
id: payload_inspector
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: Debug-only filter that dumps the raw gateway request payload as pretty JSON to the server console and posts a truncated copy to the chat. System messages are always printed in full; user/assistant/tool contents are truncated to preview_chars. The outlet is a passthrough, so the request is never modified. Logs go through the standard stdlib logger, so they follow Open WebUI's GLOBAL_LOG_LEVEL (INFO or DEBUG required).
required_open_webui_version: 0.5.0
version: 0.1.0
licence: MIT
"""

import copy
import json
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Cap for the JSON preview posted to the chat status event. The full
# payload is always available in the console log.
MAX_CHAT_STATUS_CHARS = 4000


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=999)
        preview_chars: int = Field(
            default=80,
            description=(
                "Max characters of the content shown per user/assistant/tool "
                "message. System messages are always printed in full."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def _truncate_body(self, body: dict, preview_len: int) -> dict:
        """Return a copy of the body with long contents truncated; system messages untouched."""
        b = copy.deepcopy(body)

        for msg in b.get("messages", []):
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                continue  # system messages are always printed in full

            if len(content) > preview_len:
                msg["content"] = (
                    content[:preview_len] + f"... [{len(content)} chars total]"
                )

        return b

    async def inlet(self, body: dict, __event_emitter__=None, __user__=None) -> dict:
        # 1. Copy with long contents truncated
        body_light = self._truncate_body(body, self.valves.preview_chars)

        # 2. Serialize to raw JSON
        payload_json = json.dumps(body_light, indent=2, default=str, ensure_ascii=False)

        # 3. Dump to the log (console)
        log.info(payload_json)

        # 4. Post to the chat (truncated if too large)
        if __event_emitter__:
            display = payload_json[:MAX_CHAT_STATUS_CHARS]
            if len(payload_json) > MAX_CHAT_STATUS_CHARS:
                display += "\n\n... (truncated, see the console for the full JSON)"
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": f"```json\n{display}\n```", "done": True},
                }
            )

        return body

    async def outlet(self, body: dict, *args, **kwargs) -> dict:
        return body
