"""
title: Agent Loop Guard
author: open-webui-tools
author_url: https://github.com/your-org/open-webui-tools
version: 1.1.0
required_open_webui_version: 0.5.0
requirements: httpx, pydantic
"""

from pydantic import BaseModel, Field, model_validator
from typing import AsyncGenerator, Awaitable, Callable, Literal, Optional
import httpx
import json
import logging
import re

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Guard message helpers
# --------------------------------------------------------------------------

def _warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x with same args. Change approach or summarize."


def _final_warning_msg(tool_name: str, total: int) -> str:
    return f"{tool_name} called {total}x. Still repeating. Stop now and summarize."


def _runaway_instruction(total: int, max_calls: int) -> str:
    return (
        f"TOOL LIMIT: {total}/{max_calls} used. "
        f"No more tools this turn. Summarize now."
    )


def _loop_blocked_tool_instruction(tool_name: str, total: int) -> str:
    return (
        f"TOOL REMOVED: {tool_name} blocked after {total} identical calls. "
        f"Other tools still available. Summarize or continue."
    )


def _build_guard_status_message(state: dict) -> str:
    """Build a human-readable message string for the _guard_status tool result.

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
            f"Tool call limit reached: {total}/{max_calls}. "
            f"No more tool calls this turn. Summarise now."
        )
    return ""


def _build_guard_status_content(state: dict) -> str:
    """Build the JSON content for the _guard_status tool result.

    Uses Option B from GUARD_STATUS.md: status + message fields only.

    Args:
        state: dict with keys:
            - status: str ('ok'|'warning'|'final_warning'|'blocked_tool'|'runaway')
            - tool: str | None
            - consecutive: int
            - total: int
            - max_calls: int
            - remaining_calls: int

    Returns:
        JSON string with 'status' and 'message' fields.
    """
    return json.dumps(
        {
            "status": state["status"],
            "message": _build_guard_status_message(state),
        }
    )


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

        INJECTION_POSITION: Literal["append_user", "merge_last_tool"] = Field(
            default="append_user",
            description="Where to inject guard messages: 'append_user' (before last user msg) or 'merge_last_tool' (append to last tool result).",
        )
        SHOW_TOOL_COUNTER: bool = Field(
            default=True,
            description="Append 'remaining tool calls: N' to the last tool result in each turn.",
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
        self._merge_injected = False

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
        return {
            t.strip() for t in re.split(r"[,\n\r]+", raw)
            if t.strip()
        }

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

        body["tools"] = [
            t for t in tools
            if t.get("function", {}).get("name") not in blocked
        ]

        # If tool_choice targets a blocked tool, reset it so the LLM can choose freely
        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in blocked:
            body.pop("tool_choice", None)
            log.info("tool_choice '%s' targets a blocked tool — reset", tool_choice)

    @staticmethod
    def _add_guard_status_tool(body: dict) -> None:
        """Add the _guard_status dummy tool to body['tools'].

        This tool is never registered in Open WebUI's tool callable registry;
        it is managed entirely by the pipe. The LLM sees it as available but
        any attempt to call it is silently skipped by the middleware.
        """
        guard_tool = {
            "type": "function",
            "function": {
                "name": "_guard_status",
                "description": (
                    "Internal read-only tool. Returns the current state of the "
                    "agent loop guard: number of tool calls in the turn, "
                    "consecutive identical calls, block status, and remaining "
                    "budget."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        }
        tools = body.get("tools", [])
        # Avoid duplicates in case this method is called more than once
        if not any(
            t.get("function", {}).get("name") == "_guard_status"
            for t in tools
        ):
            body["tools"] = [guard_tool] + tools

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
                    # Skip auto-injected _guard_status pairs — they are not real tool calls
                    if tc["function"]["name"] == "_guard_status":
                        continue
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        args = {}
                    history.append({"name": tc["function"]["name"], "args": args})
        history.reverse()
        return history

    def _count_consecutive_duplicates(
        self, history: list[dict]
    ) -> tuple[int, str | None, dict | None]:
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
    # Tool counter
    # ------------------------------------------------------------------

    def _append_tool_counter(self, messages: list[dict], total: int, max_calls: int) -> None:
        """Append a descending counter to the last tool result in the current turn.
        The counter tells the agent how many tool calls it has left.

        If _merge_injected is True (meaning _inject() already appended a guard
        message to the last tool result), the counter is appended without adding
        a second separator."""
        if max_calls <= 0:
            return
        remaining = max(0, max_calls - total)
        # Scan backwards to find the last tool message in the current turn
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                break
            if messages[i].get("role") == "tool":
                original = messages[i].get("content", "")
                if self._merge_injected:
                    # Separator already added by _inject() — reuse it
                    messages[i]["content"] = f"{original}\nremaining tool calls: {remaining}"
                else:
                    messages[i]["content"] = f"{original}\n---\nremaining tool calls: {remaining}"
                return

    # ------------------------------------------------------------------
    # Injection helper
    # ------------------------------------------------------------------

    def _inject(self, messages: list[dict], message: dict) -> None:
        """Insert a guard message into the conversation at the configured position.

        Supports two positions:
          - append_user:      insert before the last user message (default)
          - merge_last_tool:  append the message content to the end of the last
                              tool result of the current turn, after a clear
                              separator (no new message added).
        """
        pos = getattr(self.valves, "INJECTION_POSITION", "append_user")
        if pos == "merge_last_tool":
            # Append the guard message to the last tool result of the current turn.
            # Scan backwards until we hit a user message (start of turn).
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    break
                if messages[i].get("role") == "tool":
                    original = messages[i].get("content", "")
                    suffix = message.get("content", "")
                    if suffix:
                        messages[i]["content"] = f"{original}\n\n---\n{suffix}"
                    self._merge_injected = True
                    return
            # Fallback: no tool message found, append as system message
            messages.append({"role": "system", "content": message.get("content", "")})
        else:  # append_user (default)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    messages.insert(i, message)
                    return
            messages.append(message)

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
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        messages = body.get("messages", [])
        if not messages:
            return ""

        # Strip pipe prefix from model ID.
        # "pipe-uuid.deepseek/deepseek-v4-flash" → "deepseek/deepseek-v4-flash"
        real_model = body["model"].split(".", 1)[-1]

        # Build headers and URL for the gateway.
        headers = {"Content-Type": "application/json", **self._build_gateway_headers(user=__user__, metadata=__metadata__)}

        url = f"{self.valves.GATEWAY_BASE_URL.rstrip('/')}/chat/completions"

        log.info(
            "Agent Loop Guard → %s (model=%s, auth_header=%s, has_auth=%s, custom_headers=%s)",
            url,
            real_model,
            self.valves.GATEWAY_AUTH_HEADER,
            bool(self.valves.GATEWAY_AUTH_VALUE),
            bool(self.valves.GATEWAY_CUSTOM_HEADERS),
        )

        # --- Analyse tool calls in current turn -----------------------
        history = self._extract_tool_calls_in_turn(messages)
        total = len(history)

        # --- Debug: summary of received request -----------------------
        log.debug(
            "pipe() called | model=%s | stream=%s | tools=%s | tool_choice=%s | messages=%d",
            real_model,
            body.get("stream", False),
            [t.get("function", {}).get("name") for t in body.get("tools", [])],
            body.get("tool_choice", "auto"),
            len(messages),
        )
        log.debug(
            "Gateway headers: %s",
            {k: ("<redacted>" if k == self.valves.GATEWAY_AUTH_HEADER else v) for k, v in headers.items()},
        )

        # Reset per-turn state at the start of a new user turn
        if total == 0:
            self._merge_injected = False

        # --- Debug: tool calls extracted -------------------------------
        log.debug(
            "Tool calls in turn: %d | history: %s",
            total,
            json.dumps(history, indent=2),
        )

        # --- Helper: soft-block (remove tools + inject + forward) -----
        async def _soft_block(
            reason: str,
            instruction: str,
            blocked_tool: str | None = None,
        ):
            """Remove tools from body so the LLM cannot call more, inject
            a system message instructing it to summarise, then forward to
            the gateway. All tool results already in messages are preserved.

            If blocked_tool is set, only that tool is removed (loop case).
            If None, all tools are removed (runaway case).
            """
            # Loop soft-block is error (same severity as runaway) so the user
            # sees a clear progression: info → warning → error
            notification_type = "error"
            log.warning("Soft-block: %s (%d tool calls in turn)", reason, total)

            if __event_emitter__:
                try:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": notification_type,
                                "content": (
                                    f"🛡️ Agent Loop Guard: {reason}"
                                ),
                            },
                        }
                    )
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"🛡️ {reason}",
                                "done": True,
                                "hidden": False,
                            },
                        }
                    )
                except Exception:
                    log.warning("Failed to emit event (non-fatal)", exc_info=True)

            if blocked_tool:
                # Loop case: remove only the offending tool
                body["tools"] = [
                    t for t in body.get("tools", [])
                    if t.get("function", {}).get("name") != blocked_tool
                ]
                # If tool_choice targets the blocked tool, reset it
                if isinstance(body.get("tool_choice"), str) and blocked_tool in body["tool_choice"]:
                    body.pop("tool_choice", None)
                log.debug(
                    "Soft-block (loop) | tool removed: %s | remaining tools: %s",
                    blocked_tool,
                    [t.get("function", {}).get("name") for t in body.get("tools", [])],
                )
            else:
                # Runaway case: remove all tools
                body.pop("tools", None)
                body.pop("tool_choice", None)
                log.debug("Soft-block (runaway) | all tools removed")

            log.debug(
                "Soft-block | instruction: %s | injection_pos: %s",
                instruction,
                getattr(self.valves, "INJECTION_POSITION", "append_user"),
            )

            # Ensure _guard_status is always available, even after tool removal
            self._add_guard_status_tool(body)

            # Inject instruction to summarise
            self._inject(messages, {
                "role": "system",
                "content": instruction,
            })

            # Forward to gateway — LLM sees all tool results but has no tools.
            # Streaming: return the async generator; non-streaming: return dict.
            payload = {**body, "model": real_model, "messages": messages}
            if body.get("stream", False):
                return self._stream(payload, headers, url)
            else:
                return await self._call(payload, headers, url)

        # --- Runaway soft-block ---------------------------------------
        runaway = (
            self.valves.MAX_TOOL_CALLS_PER_TURN > 0
            and total >= self.valves.MAX_TOOL_CALLS_PER_TURN
        )
        if runaway:
            return await _soft_block(
                reason=(
                    f"Tool call limit reached: {total} calls in this turn "
                    f"(max {self.valves.MAX_TOOL_CALLS_PER_TURN})"
                ),
                instruction=_runaway_instruction(total, self.valves.MAX_TOOL_CALLS_PER_TURN),
            )

        # --- Loop detection (consecutive identical tool calls) ---------
        consecutive, bad_tool, _ = self._count_consecutive_duplicates(history)
        block_threshold = self.valves.MAX_CONSECUTIVE_BEFORE_BLOCK

        # --- Debug: loop analysis -------------------------------------
        if total > 0:
            log.debug(
                "Loop analysis | consecutive=%s | bad_tool=%s | block_threshold=%s | final_pos=%s | total=%s",
                consecutive,
                bad_tool,
                block_threshold,
                2 + (block_threshold - 2) * 3 // 5 if block_threshold >= 2 else None,
                total,
            )

        if consecutive >= 2:
            # Formula-based escalation: each level fires exactly once.
            #   consecutive == 2                                  → WARNING
            #   consecutive == final_pos (if N > 3)                → FINAL WARNING
            #   consecutive >= N                                   → soft-block
            #   otherwise                                         → silent
            #
            # final_pos is placed at ~60% of the range [2, N) so FINAL WARNING
            # has separation from the block when N is large enough.
            final_pos = 2 + (block_threshold - 2) * 3 // 5

            if consecutive >= block_threshold:
                # Soft-block: remove only the looping tool
                return await _soft_block(
                    reason=(
                        f"Repeated tool call detected: {total} calls in this turn"
                    ),
                    instruction=_loop_blocked_tool_instruction(bad_tool, total),
                    blocked_tool=bad_tool,
                )
            elif block_threshold > 3 and consecutive == final_pos:
                # Final warning (tools still available)
                self._inject(messages, {
                    "role": "system",
                    "content": _final_warning_msg(bad_tool, total),
                })
                if __event_emitter__:
                    try:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "warning",
                                    "content": (
                                        f"🛡️ Agent Loop Guard: {bad_tool} called {total}x. "
                                        f"Still repeating. Final warning."
                                    ),
                                },
                            }
                        )
                    except Exception:
                        log.warning("Failed to emit FINAL WARNING event (non-fatal)", exc_info=True)
            elif consecutive == 2:
                # First warning (tools still available)
                self._inject(messages, {
                    "role": "system",
                    "content": _warning_msg(bad_tool, total),
                })
                if __event_emitter__:
                    try:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "info",
                                    "content": (
                                        f"🛡️ Agent Loop Guard: {bad_tool} called {total}x "
                                        f"with same args."
                                    ),
                                },
                            }
                        )
                    except Exception:
                        log.warning("Failed to emit WARNING event (non-fatal)", exc_info=True)

        # --- Tool blocklist ---------------------------------------------
        before_blocklist = [t.get("function", {}).get("name") for t in body.get("tools", [])]
        self._apply_tool_blocklist(body)
        if self.valves.TOOL_BLOCKLIST.strip():
            after_blocklist = [t.get("function", {}).get("name") for t in body.get("tools", [])]
            removed = set(before_blocklist) - set(after_blocklist)
            if removed:
                log.debug("Tool blocklist | removed: %s", sorted(removed))

        # --- Tool counter ----------------------------------------------
        if (
            self.valves.SHOW_TOOL_COUNTER
            and total > 0
            and self.valves.MAX_TOOL_CALLS_PER_TURN > 0
        ):
            self._append_tool_counter(messages, total, self.valves.MAX_TOOL_CALLS_PER_TURN)
            remaining = max(0, self.valves.MAX_TOOL_CALLS_PER_TURN - total)
            log.debug(
                "Tool counter appended | total=%d | max=%d | remaining=%d | merge_injected=%s",
                total,
                self.valves.MAX_TOOL_CALLS_PER_TURN,
                remaining,
                self._merge_injected,
            )

            if __event_emitter__:
                remaining = max(0, self.valves.MAX_TOOL_CALLS_PER_TURN - total)
                try:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"🛡️ Remaining tool calls: {remaining}/{self.valves.MAX_TOOL_CALLS_PER_TURN}",
                                "done": True,
                                "hidden": False,
                            },
                        }
                    )
                except Exception:
                    log.warning("Failed to emit counter status (non-fatal)", exc_info=True)

        # --- Ensure _guard_status is available to the LLM ---------------
        self._add_guard_status_tool(body)

        # --- Debug: final payload before forwarding --------------------
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
        log.debug(
            "Forwarding to gateway | payload summary: %s",
            json.dumps(payload_preview, indent=2),
        )

        # --- Forward to gateway ---------------------------------------
        payload = {**body, "model": real_model, "messages": messages}

        try:
            if body.get("stream", False):
                log.debug("Streaming request started")
                return self._stream(payload, headers, url)
            else:
                response = await self._call(payload, headers, url)
                # Only log first 500 chars of response to avoid flooding
                response_preview = json.dumps(response, indent=2)[:500]
                log.debug("Non-streaming response (truncated): %s", response_preview)
                return response
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
