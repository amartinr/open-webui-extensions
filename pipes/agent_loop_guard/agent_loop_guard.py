"""
title: Agent Loop Guard
author: open-webui-tools
author_url: https://github.com/your-org/open-webui-tools
version: 2.1.0
required_open_webui_version: 0.5.0
requirements: httpx, pydantic
"""

from pydantic import BaseModel, Field, model_validator
from typing import AsyncGenerator, Awaitable, Callable, Optional
import httpx
import json
import logging
import re

log = logging.getLogger(__name__)


GUARD_MARKER = "[Guard Budget Exhausted]"


def _build_guard_message(status: str, tool: str | None, total: int, max_calls: int) -> str:
    """Build the text that replaces the tool result when budget is exhausted."""
    if status == "loop":
        return (
            f"{GUARD_MARKER}\n"
            f"The tool {tool} has been called too many times with the same arguments "
            f"({total} calls this turn).\n"
            f"Other tools are still available. Summarise what you have."
        )
    elif status == "runaway":
        return (
            f"{GUARD_MARKER}\n"
            f"Maximum tool calls reached ({total}/{max_calls}). "
            f"No further tool calls are possible this turn.\n"
            f"Summarise the information you have collected so far."
        )
    return ""


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
        GATEWAY_CUSTOM_HEADERS: str = Field(
            default="",
            description="JSON object of extra HTTP headers to send with every gateway request. "
            'Example: {"x-bf-dim-host": "myhost", "x-trace-id": "abc"}. '
            "Leave empty if not needed.",
        )
        MAX_TOOL_CALLS_PER_TURN: int = Field(
            default=15,
            description="Max tool calls in a turn before the guard fires. Set to 0 to disable.",
        )
        MAX_CONSECUTIVE_TOOL_CALLS: int = Field(
            default=4,
            ge=3,
            description="Max consecutive identical tool calls before budget is exhausted (min 3).",
        )
        TOOL_BLOCKLIST: str = Field(
            default="",
            description="Comma-separated (or newline-separated) tool names to REMOVE from the agent's tool list. "
            'Example: "delete_file, terminal_execute".',
        )

        @model_validator(mode="after")
        def _check_runaway_gt_loop(self):
            """Ensure MAX_TOOL_CALLS_PER_TURN > MAX_CONSECUTIVE_TOOL_CALLS
            when both are enabled."""
            runaway = self.MAX_TOOL_CALLS_PER_TURN
            loop = self.MAX_CONSECUTIVE_TOOL_CALLS
            if runaway > 0 and loop >= runaway:
                raise ValueError(
                    f"MAX_TOOL_CALLS_PER_TURN ({runaway}) must be greater than "
                    f"MAX_CONSECUTIVE_TOOL_CALLS ({loop})."
                )
            return self

    class UserValves(BaseModel):
        MAX_TOOL_CALLS_PER_TURN: int = Field(
            default=0,
            ge=0,
            description="Max tool calls in a turn. 0 = use admin default.",
        )
        MAX_CONSECUTIVE_TOOL_CALLS: int = Field(
            default=0,
            ge=0,
            description="Max consecutive identical tool calls. 0 = use admin default.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._admin_valves = self.Valves()
        self._models_cache: list[dict] = []

    # ------------------------------------------------------------------
    # Model discovery (manifold)
    # ------------------------------------------------------------------

    async def pipes(self) -> list[dict]:
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
            {"id": m["id"], "name": f"🔧 {m.get('name', m['id'])}"}
            for m in data.get("data", [])
        ]
        log.info("Model discovery: %d models cached", len(self._models_cache))
        return self._models_cache

    # ------------------------------------------------------------------
    # Gateway helpers
    # ------------------------------------------------------------------

    def _build_gateway_headers(
        self,
        user: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        headers = {}
        if self.valves.GATEWAY_AUTH_VALUE:
            headers[self.valves.GATEWAY_AUTH_HEADER] = self.valves.GATEWAY_AUTH_VALUE
        else:
            log.warning("GATEWAY_AUTH_VALUE is empty.")

        if self.valves.GATEWAY_CUSTOM_HEADERS:
            try:
                raw_headers = json.loads(self.valves.GATEWAY_CUSTOM_HEADERS)
                if isinstance(raw_headers, dict):
                    user = user or {}
                    meta = metadata or {}
                    template_vars = {
                        "{{USER_ID}}": str(user.get("id", "") or ""),
                        "{{USER_NAME}}": str(user.get("name", "") or ""),
                        "{{USER_EMAIL}}": str(user.get("email", "") or ""),
                        "{{USER_ROLE}}": str(user.get("role", "") or ""),
                        "{{CHAT_ID}}": str(meta.get("chat_id", "") or ""),
                        "{{MESSAGE_ID}}": str(meta.get("message_id", "") or ""),
                    }
                    for k, v in raw_headers.items():
                        if not k:
                            continue
                        val = str(v) if v is not None else ""
                        for token, resolved in template_vars.items():
                            val = val.replace(token, resolved)
                        headers[k] = val
                else:
                    log.warning("GATEWAY_CUSTOM_HEADERS is not a JSON object")
            except json.JSONDecodeError as e:
                log.warning("GATEWAY_CUSTOM_HEADERS is not valid JSON: %s", e)

        return headers

    # ------------------------------------------------------------------
    # Tool blocklist helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tool_list(raw: str) -> set[str]:
        if not raw or not raw.strip():
            return set()
        return {t.strip() for t in re.split(r"[,\n\r]+", raw) if t.strip()}

    def _apply_tool_blocklist(self, body: dict) -> None:
        raw = getattr(self.valves, "TOOL_BLOCKLIST", "")
        if not raw or not raw.strip():
            return
        tools = body.get("tools", [])
        if not tools:
            return
        blocked = self._parse_tool_list(raw)
        actual_names = {t.get("function", {}).get("name") for t in tools if t.get("function", {})}
        unknown = blocked - actual_names
        if unknown:
            log.warning("TOOL_BLOCKLIST contains unknown names: %s", sorted(unknown))
        body["tools"][:] = [t for t in tools if t.get("function", {}).get("name") not in blocked]
        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in blocked:
            body.pop("tool_choice", None)

    # ------------------------------------------------------------------
    # Valve resolution
    # ------------------------------------------------------------------

    def _resolve_limit(self, user_val: int, admin_val: int) -> int:
        return user_val if user_val > 0 else admin_val

    # ------------------------------------------------------------------
    # Tool-call analysis
    # ------------------------------------------------------------------

    def _analyse(self, body: dict) -> tuple[bool, str | None, str, int, int]:
        """Analyse tool calls and decide if the guard should fire.

        Returns (should_block, tool_to_blame, block_kind, total, max_calls).

        block_kind is 'loop' or 'runaway'.  When should_block is False
        the other return values are meaningless.
        """
        messages = body.get("messages", [])
        max_calls = self._resolve_limit(
            self.valves.MAX_TOOL_CALLS_PER_TURN,
            self._admin_valves.MAX_TOOL_CALLS_PER_TURN,
        )
        max_consecutive = self._resolve_limit(
            self.valves.MAX_CONSECUTIVE_TOOL_CALLS,
            self._admin_valves.MAX_CONSECUTIVE_TOOL_CALLS,
        )

        # Extract real tool calls (skip those whose result was replaced by the guard)
        history: list[dict] = []

        # First pass: identify guard-replaced results
        guarded_ids: set[str] = set()
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and GUARD_MARKER in content:
                    guarded_ids.add(msg.get("tool_call_id", ""))

        # Second pass: collect real calls, skipping guarded ones
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id", "") in guarded_ids:
                        continue
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}
                    history.append({"name": tc["function"]["name"], "args": args})
        history.reverse()

        total = len(history)

        # Count consecutive identical calls
        consecutive = 0
        bad_tool = None
        if history:
            last_call = history[-1]
            for tc in reversed(history):
                if tc["name"] == last_call["name"] and tc["args"] == last_call["args"]:
                    consecutive += 1
                else:
                    break
            if consecutive >= 2:
                bad_tool = last_call["name"]

        # Loop detection: consecutive > max_consecutive
        if max_consecutive > 0 and consecutive > max_consecutive and bad_tool:
            return True, bad_tool, "loop", total, max_calls

        # Runaway: total > max_calls (only if no loop)
        if max_calls > 0 and total > max_calls:
            return True, None, "runaway", total, max_calls

        return False, None, "", total, max_calls

    # ------------------------------------------------------------------
    # Gateway proxy
    # ------------------------------------------------------------------

    async def _stream(self, payload: dict, headers: dict, url: str) -> AsyncGenerator[str, None]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line:
                        yield line

    async def _call(self, payload: dict, headers: dict, url: str) -> dict:
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
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        messages = body.get("messages", [])
        if not messages:
            return ""

        real_model = body["model"].split(".", 1)[-1]
        headers = {"Content-Type": "application/json", **self._build_gateway_headers(user=__user__, metadata=__metadata__)}
        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

        # --- Analyse tool calls ---------------------------------------------
        should_block, bad_tool, kind, total, max_calls = self._analyse(body)

        log.info(
            "Agent Loop Guard → %s (block=%s, kind=%s, tool=%s, total=%s, max=%s)",
            url, should_block, kind, bad_tool, total, max_calls,
        )

        # --- Block: replace last tool result --------------------------------
        if should_block:
            guard_msg = _build_guard_message(kind, bad_tool, total, max_calls)
            if guard_msg:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "tool":
                        messages[i]["content"] = guard_msg
                        log.info(
                            "Tool result replaced with guard (kind=%s, tool=%s)",
                            kind, bad_tool,
                        )
                        break

            if __event_emitter__:
                try:
                    if kind == "loop":
                        await __event_emitter__({
                            "type": "notification",
                            "data": {"type": "error", "content": f"🔧 {bad_tool} budget exhausted after too many identical calls."},
                        })
                    elif kind == "runaway":
                        await __event_emitter__({
                            "type": "notification",
                            "data": {"type": "error", "content": f"🔧 Tool call budget exhausted ({total}/{max_calls})."},
                        })
                except Exception:
                    log.warning("Failed to emit event (non-fatal)", exc_info=True)

        # --- Always show counter pill if there are tool calls ---------------
        if __event_emitter__ and max_calls > 0 and total > 0:
            remaining = max(0, max_calls - total)
            try:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": f"🔧 Remaining: {remaining}/{max_calls}", "done": True, "hidden": False},
                })
            except Exception:
                pass

        # --- Apply blocklist and forward ------------------------------------
        self._apply_tool_blocklist(body)
        payload = {**body, "model": real_model, "messages": messages}

        try:
            if body.get("stream", False):
                return self._stream(payload, headers, url)
            else:
                return await self._call(payload, headers, url)
        except httpx.HTTPStatusError as e:
            log.error("Gateway returned HTTP %d: %s", e.response.status_code, e)
            return f"Gateway error: HTTP {e.response.status_code}."
        except httpx.RequestError as e:
            log.error("Gateway unreachable: %s", e)
            return "Gateway unreachable."
        except Exception as e:
            log.error("Unexpected error: %s", e)
            return f"Error: {e}"
