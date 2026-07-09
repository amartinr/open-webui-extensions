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
import logging

log = logging.getLogger(__name__)


class Pipe:
    class Valves(BaseModel):
        GATEWAY_BASE_URL: str = Field(
            default="",
            description="Base URL for the OpenAI-compatible gateway (e.g. Bifrost).",
        )
        GATEWAY_API_KEY: str = Field(
            default="",
            description="API key for gateway authentication.",
            json_schema_extra={"input": {"type": "password"}},
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

        headers = {}
        if self.valves.GATEWAY_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.GATEWAY_API_KEY}"

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
        headers = {"Content-Type": "application/json"}
        if self.valves.GATEWAY_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.GATEWAY_API_KEY}"

        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

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
