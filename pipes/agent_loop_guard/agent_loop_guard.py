"""
title: Agent Loop Guard
author: open-webui-tools
author_url: https://github.com/your-org/open-webui-tools
version: 1.0.0
required_open_webui_version: 0.5.0
requirements: httpx, pydantic
"""

from pydantic import BaseModel, Field
from typing import AsyncGenerator, Optional
import httpx
import json
import logging

log = logging.getLogger(__name__)


class Pipe:
    class Valves(BaseModel):
        GATEWAY_BASE_URL: str = Field(
            default="",
            description="Base URL for the OpenAI-compatible gateway (e.g. Bifrost).",
        )
        GATEWAY_AUTH_HEADER: str = Field(
            default="x-bf-vk",
            description="HTTP header name for the API key (e.g. 'Authorization', 'x-bf-vk', 'x-api-key').",
        )
        GATEWAY_AUTH_VALUE: str = Field(
            default="",
            description="Credential value sent in the configured auth header (e.g. 'Bearer sk-...', 'bf-vk-...').",
            json_schema_extra={"input": {"type": "password"}},
        )
        GATEWAY_HOST_HEADER: str = Field(
            default="x-bf-dim-host",
            description="HTTP header name for the host routing value (e.g. 'x-bf-dim-host').",
        )
        GATEWAY_HOST_VALUE: str = Field(
            default="",
            description="Value sent in the host routing header (e.g. Bifrost dimension). Leave empty if not needed.",
        )
        MAX_TOOL_CALLS_PER_TURN: int = Field(
            default=15,
            description="Max tool calls in a turn before force-termination. Set to 0 to disable.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._models_cache: list[dict] = []

    # ------------------------------------------------------------------
    # Model discovery (manifold)
    # ------------------------------------------------------------------

    async def pipes(self) -> list[dict]:
        """Query gateway for available models. Cache on success, fallback on failure."""
        if not self.valves.GATEWAY_BASE_URL:
            return [{"id": "config", "name": "⚠️ Configure gateway URL"}]

        headers = self._build_gateway_headers()
        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/models"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, headers=headers, timeout=10)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("Gateway unreachable during model discovery: %s", e)
            return self._models_cache or [
                {"id": "error", "name": "⚠️ Gateway unreachable"}
            ]

        self._models_cache = [
            {"id": m["id"], "name": f"🛡️ {m.get('name', m['id'])}"}
            for m in data.get("data", [])
        ]
        log.info("Model discovery: %d models cached", len(self._models_cache))
        return self._models_cache

    # ------------------------------------------------------------------
    # Gateway helpers
    # ------------------------------------------------------------------

    def _build_gateway_headers(self) -> dict:
        """Build the headers dict for gateway requests."""
        headers = {}
        if self.valves.GATEWAY_AUTH_VALUE:
            headers[self.valves.GATEWAY_AUTH_HEADER] = self.valves.GATEWAY_AUTH_VALUE
            log.debug(
                "Auth header: %s = <redacted %d chars>",
                self.valves.GATEWAY_AUTH_HEADER,
                len(self.valves.GATEWAY_AUTH_VALUE),
            )
        else:
            log.warning("GATEWAY_AUTH_VALUE is empty — gateway requests will have no auth header.")
        if self.valves.GATEWAY_HOST_VALUE:
            headers[self.valves.GATEWAY_HOST_HEADER] = self.valves.GATEWAY_HOST_VALUE

        log.debug("Gateway headers: %s", {k: ("<redacted>" if k == self.valves.GATEWAY_AUTH_HEADER else v) for k, v in headers.items()})
        return headers

    # ------------------------------------------------------------------
    # Tool-call analysis
    # ------------------------------------------------------------------

    def _extract_tool_calls_in_turn(self, messages: list[dict]) -> list[dict]:
        """Scan backwards from the end until the last user message.
        Collect every assistant tool_call in the current turn."""
        history: list[dict] = []
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}
                    history.append({"name": tc["function"]["name"], "args": args})
        history.reverse()
        return history

    # ------------------------------------------------------------------
    # Gateway proxy
    # ------------------------------------------------------------------

    async def _stream(
        self, payload: dict, headers: dict, url: str
    ) -> AsyncGenerator[str, None]:
        """Stream SSE lines from the gateway back to Open WebUI."""
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line:
                        yield line

    async def _call(self, payload: dict, headers: dict, url: str) -> dict:
        """Non-streaming call to the gateway."""
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ):
        messages = body.get("messages", [])
        if not messages:
            return ""

        # Strip pipe prefix from model ID.
        # "pipe-uuid.deepseek/deepseek-v4-flash" → "deepseek/deepseek-v4-flash"
        real_model = body["model"].split(".", 1)[-1]

        # Build headers and URL for the gateway.
        headers = {"Content-Type": "application/json", **self._build_gateway_headers()}

        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

        log.info(
            "Agent Loop Guard → %s (model=%s, auth_header=%s, has_auth=%s, has_host=%s)",
            url,
            real_model,
            self.valves.GATEWAY_AUTH_HEADER,
            bool(self.valves.GATEWAY_AUTH_VALUE),
            bool(self.valves.GATEWAY_HOST_VALUE),
        )

        # --- Runaway prevention ---------------------------------------
        max_tool_calls = self.valves.MAX_TOOL_CALLS_PER_TURN
        if max_tool_calls > 0:
            history = self._extract_tool_calls_in_turn(messages)
            if len(history) >= max_tool_calls:
                log.warning(
                    "Force-terminate: %d tool calls in turn (limit=%d)",
                    len(history),
                    max_tool_calls,
                )
                return (
                    f"I've stopped because this turn reached the limit of "
                    f"{max_tool_calls} tool calls without producing a final "
                    f"answer.\n\n"
                    f"Please try a more specific query or reduce the scope "
                    f"of your request."
                )

        # Forward to gateway with the real model ID.
        payload = {**body, "model": real_model}

        try:
            if body.get("stream", False):
                return self._stream(payload, headers, url)
            else:
                return await self._call(payload, headers, url)
        except httpx.HTTPStatusError as e:
            log.error("Gateway returned HTTP %d: %s", e.response.status_code, e)
            return (
                f"Gateway error: HTTP {e.response.status_code}. "
                f"Please check the gateway configuration."
            )
        except httpx.RequestError as e:
            log.error("Gateway unreachable: %s", e)
            return (
                "Gateway unreachable. Please check that the gateway is running "
                "and GATEWAY_BASE_URL is correct."
            )
        except Exception as e:
            log.error("Unexpected error calling gateway: %s", e)
            return f"Error calling gateway: {e}"
