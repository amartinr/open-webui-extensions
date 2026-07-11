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


# --------------------------------------------------------------------------
# Guard message helpers
# --------------------------------------------------------------------------


def _build_guard_message(state: dict) -> str:
    """Build a human-readable guard status message.

    Args:
        state: dict with keys:
            - status: str ('ok'|'warning'|'final_warning'|'blocked_tool'|'runaway')
            - tool: str | None
            - consecutive: int
            - total: int
            - max_calls: int
            - remaining_calls: int

    Returns:
        A single, self-contained message string.
    """
    remaining = state.get("remaining_calls", 0)
    max_calls = state.get("max_calls", 0)
    tool = state.get("tool")
    consecutive = state.get("consecutive", 0)
    total = state.get("total", 0)
    status = state.get("status", "ok")

    if status == "ok":
        return f"{remaining}/{max_calls} tool calls remaining."
    elif status == "warning":
        return (
            f"{tool} called {consecutive}x with the same arguments. "
            f"{remaining}/{max_calls} tool calls remaining. "
            f"Change your approach or summarise."
        )
    elif status == "final_warning":
        return (
            f"{tool} called {consecutive}x with the same arguments. "
            f"{remaining}/{max_calls} tool calls remaining. "
            f"This is your final warning. Stop repeating and summarise."
        )
    elif status == "blocked_tool":
        return (
            f"TOOL REMOVED: {tool} blocked after {consecutive} identical calls. "
            f"{remaining}/{max_calls} tool calls remaining. "
            f"Other tools are still available or you may summarise now."
        )
    elif status == "runaway":
        return (
            f"TOOL LIMIT REACHED: {total}/{max_calls}. "
            f"Tools used in this turn have been removed. "
            f"Other tools are still available or you may summarise now."
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
            description="Max tool calls in a turn before tools are removed (soft-block). Set to 0 to disable.",
        )
        MAX_CONSECUTIVE_BEFORE_BLOCK: int = Field(
            default=4,
            ge=3,
            description="Consecutive identical tool calls before soft-block (min 3). "
            "Warnings are spaced automatically: WARNING at 50%, FINAL WARNING at 75%.",
        )
        TOOL_BLOCKLIST: str = Field(
            default="",
            description="Comma-separated (or newline-separated) tool names to REMOVE from the agent's tool list. "
            'Example: "delete_file, terminal_execute".',
        )

        @model_validator(mode="after")
        def _check_runaway_gt_loop(self):
            """Ensure MAX_TOOL_CALLS_PER_TURN > MAX_CONSECUTIVE_BEFORE_BLOCK
            when both are enabled, so runaway doesn't fire before loop detection."""
            runaway = self.MAX_TOOL_CALLS_PER_TURN
            loop = self.MAX_CONSECUTIVE_BEFORE_BLOCK
            if runaway > 0 and loop >= runaway:
                raise ValueError(
                    f"MAX_TOOL_CALLS_PER_TURN ({runaway}) must be greater than "
                    f"MAX_CONSECUTIVE_BEFORE_BLOCK ({loop}), otherwise runaway "
                    f"triggers before loop detection."
                )
            return self

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

    def _build_gateway_headers(
        self,
        user: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Build the headers dict for gateway requests.

        Resolves template variables in header values:
          {{USER_NAME}}, {{USER_ID}}, {{USER_EMAIL}}, {{USER_ROLE}},
          {{CHAT_ID}}, {{MESSAGE_ID}}.
        """
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

        # Parse custom headers from JSON valve and resolve template vars
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
                    log.warning("GATEWAY_CUSTOM_HEADERS is not a JSON object — ignoring")
            except json.JSONDecodeError as e:
                log.warning("GATEWAY_CUSTOM_HEADERS is not valid JSON: %s", e)

        log.debug(
            "Gateway headers: %s",
            {k: ("<redacted>" if k == self.valves.GATEWAY_AUTH_HEADER else v) for k, v in headers.items()},
        )
        return headers

    # ------------------------------------------------------------------
    # Tool blocklist helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tool_list(raw: str) -> set[str]:
        """Parse a string that may be comma-separated and/or newline-separated
        into a set of non-empty, stripped tool names.

        Accepts any combination:
          "search_web, calculator"
          "search_web\ncalculator"
          "search_web, calculator\ndelete_file"
        """
        if not raw or not raw.strip():
            return set()
        return {t.strip() for t in re.split(r"[,\n\r]+", raw) if t.strip()}

    def _apply_tool_blocklist(self, body: dict) -> None:
        """Remove tools from body['tools'] whose names appear in TOOL_BLOCKLIST.

        Mutates body['tools'] in-place.
        - Returns immediately if TOOL_BLOCKLIST is empty.
        - If tool_choice targets a blocked tool, it is reset so the LLM can choose freely.
        """
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
            log.warning(
                "TOOL_BLOCKLIST contains names not found among available tools: %s",
                sorted(unknown),
            )

        body["tools"][:] = [
            t for t in tools
            if t.get("function", {}).get("name") not in blocked
        ]

        # If tool_choice targets a blocked tool, reset it so the LLM can choose freely.
        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in blocked:
            body.pop("tool_choice", None)
            log.info("tool_choice '%s' targets a blocked tool — reset", tool_choice)

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

    def _count_consecutive_duplicates(self, history: list[dict]) -> tuple[int, str | None, dict | None]:
        """Count consecutive identical tool calls from the end of history.

        Returns (count, name, args) of the repeated tool.
        count=1 means the last call is unique (no consecutive duplicate).
        """
        if not history:
            return 0, None, None
        last = history[-1]
        count = 0
        for tc in reversed(history):
            if tc["name"] == last["name"] and tc["args"] == last["args"]:
                count += 1
            else:
                break
        return count, last["name"], last["args"]

    # ------------------------------------------------------------------
    # Gateway proxy
    # ------------------------------------------------------------------

    async def _stream(self, payload: dict, headers: dict, url: str) -> AsyncGenerator[str, None]:
        """Stream SSE lines from the gateway back to Open WebUI."""
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
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
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        messages = body.get("messages", [])
        if not messages:
            return ""

        real_model = body["model"].split(".", 1)[-1]
        headers = {"Content-Type": "application/json", **self._build_gateway_headers(user=__user__, metadata=__metadata__)}
        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

        log.info(
            "Agent Loop Guard → %s (model=%s, auth_header=%s, has_auth=%s, custom_headers=%s)",
            url, real_model, self.valves.GATEWAY_AUTH_HEADER,
            bool(self.valves.GATEWAY_AUTH_VALUE), bool(self.valves.GATEWAY_CUSTOM_HEADERS),
        )

        # --- Analyse tool calls ------------------------------------------
        history = self._extract_tool_calls_in_turn(messages)
        total = len(history)

        log.debug(
            "pipe() called | model=%s | stream=%s | tools=%s | tool_choice=%s | messages=%d",
            real_model, body.get("stream", False),
            [t.get("function", {}).get("name") for t in body.get("tools", [])],
            body.get("tool_choice", "auto"), len(messages),
        )

        log.debug("Tool calls in turn: %d | history: %s", total, json.dumps(history, indent=2))

        # --- Loop detection -----------------------------------------------
        consecutive, bad_tool, _ = self._count_consecutive_duplicates(history)
        block_threshold = self.valves.MAX_CONSECUTIVE_BEFORE_BLOCK
        max_calls = self.valves.MAX_TOOL_CALLS_PER_TURN
        remaining_calls = max(0, max_calls - total)

        if total > 0:
            final_pos = 2 + (block_threshold - 2) * 3 // 5 if block_threshold >= 2 else None
            log.debug(
                "Loop analysis | consecutive=%s | bad_tool=%s | block_threshold=%s | final_pos=%s | total=%s",
                consecutive, bad_tool, block_threshold, final_pos, total,
            )

        # --- Build guard state dict ---------------------------------------
        state = {
            "status": "ok",
            "tool": None,
            "consecutive": consecutive,
            "total": total,
            "max_calls": max_calls,
            "remaining_calls": remaining_calls,
        }

        # --- Escalation ladder --------------------------------------------
        # Loop detection takes precedence over runaway.  If the agent has
        # exceeded the consecutive threshold, it's in a loop — report that
        # even if it also hit the total-turn limit.  This way the agent
        # keeps other tools available and can still be productive.
        # Runaway only fires when consecutive is below the loop threshold.
        runaway = max_calls > 0 and total >= max_calls and consecutive < block_threshold

        if consecutive >= 2:
            final_pos = 2 + (block_threshold - 2) * 3 // 5

            if consecutive >= block_threshold:
                state["status"] = "blocked_tool"
                state["tool"] = bad_tool
                log.warning("Soft-block (loop): %s blocked after %d calls", bad_tool, total)
                tools_list = body.get("tools", [])
                tools_list[:] = [
                    t for t in tools_list
                    if t.get("function", {}).get("name") != bad_tool
                ]
                # Also remove from metadata so the middleware can't execute it
                meta_tools = __metadata__.get("tools", {}) if __metadata__ else {}
                meta_tools.pop(bad_tool, None)
                if isinstance(body.get("tool_choice"), str) and bad_tool in body["tool_choice"]:
                    body.pop("tool_choice", None)

            elif block_threshold > 3 and consecutive == final_pos:
                state["status"] = "final_warning"
                state["tool"] = bad_tool
                log.debug("Final warning: %s consecutive=%d", bad_tool, consecutive)

            elif consecutive == 2:
                state["status"] = "warning"
                state["tool"] = bad_tool
                log.debug("Warning: %s consecutive=%d", bad_tool, consecutive)

        if state["status"] == "ok" and runaway:
            state["status"] = "runaway"
            log.warning("Soft-block (runaway): %d tool calls in turn", total)
            # Remove only the tools that were used in this turn,
            # same pattern as loop block (blocked_tool removes just the offender).
            # Unused tools stay available so the agent can still be productive.
            used_tools = {tc["name"] for tc in history}
            tools_list = body.get("tools", [])
            tools_list[:] = [
                t for t in tools_list
                if t.get("function", {}).get("name") not in used_tools
            ]
            if __metadata__:
                meta_tools = __metadata__.get("tools", {})
                if meta_tools:
                    for name in used_tools:
                        meta_tools.pop(name, None)
            body.pop("tool_choice", None)

        # --- Soft-block: early return (same pattern as master) -------------
        if state["status"] in ("runaway", "blocked_tool"):
            if __event_emitter__:
                try:
                    if state["status"] == "runaway":
                        await __event_emitter__({
                            "type": "notification",
                            "data": {"type": "error", "content": f"🛡️ Agent Loop Guard: Tool call limit reached ({total}/{max_calls})."},
                        })
                        await __event_emitter__({
                            "type": "status",
                            "data": {"description": f"🛡️ Tool call limit reached: {total}/{max_calls}", "done": True, "hidden": False},
                        })
                    elif state["status"] == "blocked_tool":
                        await __event_emitter__({
                            "type": "notification",
                            "data": {"type": "error", "content": f"🛡️ Agent Loop Guard: {bad_tool} blocked after {consecutive} identical calls."},
                        })
                        await __event_emitter__({
                            "type": "status",
                            "data": {"description": f"🛡️ {bad_tool} blocked", "done": True, "hidden": False},
                        })
                except Exception:
                    log.warning("Failed to emit event (non-fatal)", exc_info=True)

            # Inject system message instructing the agent to stop
            messages.append({"role": "system", "content": _build_guard_message(state)})

            # Fall through to the normal forward path.  Tools were already
            # cleared in-place above (tools[:] = ...) so no further action
            # is needed here.

        # --- Non-soft-block: event emission --------------------------------
        if __event_emitter__:
            try:
                if state["status"] == "final_warning":
                    await __event_emitter__({
                        "type": "notification",
                        "data": {"type": "warning", "content": f"🛡️ Agent Loop Guard: {bad_tool} called {consecutive}x. Final warning."},
                    })
                elif state["status"] == "warning":
                    await __event_emitter__({
                        "type": "notification",
                        "data": {"type": "info", "content": f"🛡️ Agent Loop Guard: {bad_tool} called {consecutive}x with same args."},
                    })

                if max_calls > 0 and total > 0:
                    await __event_emitter__({
                        "type": "status",
                        "data": {"description": f"🛡️ Remaining tool calls: {remaining_calls}/{max_calls}", "done": True, "hidden": False},
                    })
            except Exception:
                log.warning("Failed to emit event (non-fatal)", exc_info=True)

        # --- Inject system message for warning/final_warning -----------------
        # System messages give the agent real feedback so it can correct
        # itself before the soft-block.
        if state["status"] in ("warning", "final_warning"):
            messages.append({"role": "system", "content": _build_guard_message(state)})

        # --- Apply tool blocklist --------------------------------------------
        self._apply_tool_blocklist(body)

        # --- Debug: final payload before forwarding --------------------------
        payload_preview = {
            "model": real_model,
            "stream": body.get("stream", False),
            "tools": [t.get("function", {}).get("name") for t in body.get("tools", [])],
            "tool_choice": body.get("tool_choice", "auto"),
            "messages": [
                {
                    "role": m.get("role"),
                    "content": (m.get("content", "")[:200] + "...") if len(m.get("content", "")) > 200 else m.get("content", ""),
                    "tool_calls": bool(m.get("tool_calls")),
                }
                for m in messages
            ],
        }
        log.debug("Forwarding to gateway | payload summary: %s", json.dumps(payload_preview, indent=2))

        # --- Forward to gateway ---------------------------------------------
        payload = {**body, "model": real_model, "messages": messages}

        try:
            if body.get("stream", False):
                log.debug("Streaming request started")
                return self._stream(payload, headers, url)
            else:
                response = await self._call(payload, headers, url)
                log.debug("Non-streaming response (truncated): %s", json.dumps(response, indent=2)[:500])
                return response
        except httpx.HTTPStatusError as e:
            log.error("Gateway returned HTTP %d: %s", e.response.status_code, e)
            return f"Gateway error: HTTP {e.response.status_code}. Please check the gateway configuration."
        except httpx.RequestError as e:
            log.error("Gateway unreachable: %s", e)
            return "Gateway unreachable. Please check that the gateway is running and GATEWAY_BASE_URL is correct."
        except Exception as e:
            log.error("Unexpected error calling gateway: %s", e)
            return f"Error calling gateway: {e}"
