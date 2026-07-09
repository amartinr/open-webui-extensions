"""
title: Agent Loop Guard
author: open-webui-tools
author_url: https://github.com/your-org/open-webui-tools
version: 1.0.0
required_open_webui_version: 0.5.0
requirements: httpx, pydantic
"""

from pydantic import BaseModel, Field
from typing import AsyncGenerator, Awaitable, Callable, Optional
import httpx
import json
import logging

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Guard messages (structured with metadata, composed at injection time)
# --------------------------------------------------------------------------
# Each entry has:
#   marker — stable token for detection (_escalation_level, etc.)
#   text   — human-readable message sent to the LLM
#   level  — 0=reminder, 1=warning, 2=final
#
# The composed string sent to the LLM is: marker + " " + text

_GUARD_MESSAGES = {
    "warning": {
        "marker": "[GUARD_WARN]",
        "level": 1,
        "text": (
            "You are repeating the same tool call without making progress. "
            "If you are stuck, stop calling tools and summarise what you"
            " have so far."
        ),
    },
    "final": {
        "marker": "[GUARD_FINAL]",
        "level": 2,
        "text": (
            "You are still repeating tool calls after the previous warning. "
            "Stop calling tools now and provide a summary of everything you"
            " have gathered."
        ),
    },
    "reminder": {
        "marker": "[GUARD_REMIND]",
        "level": 0,
        "text": (
            "Periodically check whether your tool calls are producing new"
            " results. If you detect repetition, stop and provide a"
            " summary."
        ),
    },
}


def _compose(msg: dict) -> str:
    """Compose a guard message string for the LLM: marker + text."""
    return f"{msg['marker']} {msg['text']}"


def _guard_by_marker(marker: str) -> dict | None:
    """Look up a guard message entry by its marker string."""
    for entry in _GUARD_MESSAGES.values():
        if entry["marker"] == marker:
            return entry
    return None


def _runaway_instruction(total: int, max_calls: int) -> str:
    return (
        f"TOOL CALL LIMIT REACHED: You have used {total} tool calls "
        f"(max {max_calls}). You cannot call more tools this turn. "
        f"Provide your final answer using the information you have gathered."
    )


def _loop_soft_block_instruction(total: int) -> str:
    return (
        f"TOOL ACCESS REVOKED: You made {total} identical tool calls without "
        f"making progress. All tool access has been removed for this turn. "
        f"Provide your final answer now."
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
        MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING: int = Field(
            default=2,
            description="Consecutive identical tool calls before first warning. Set to 0 to disable.",
        )
        MAX_WARNINGS_BEFORE_TERMINATE: int = Field(
            default=2,
            description="Warnings before tools are removed (soft-block). Set to 0 to soft-block immediately on first loop detection.",
        )
        ENABLE_PREVENTIVE_REMINDER: bool = Field(
            default=True,
            description="Inject periodic self-evaluation reminders.",
        )
        REMINDER_INTERVAL: int = Field(
            default=3,
            description="Inject preventive reminder every N user messages.",
        )
        INJECTION_POSITION: str = Field(
            default="append_system",
            description="Where to inject warning messages: 'prepend', 'append_system', or 'append_user'.",
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

    def _has_consecutive_duplicates(
        self, history: list[dict], threshold: int
    ) -> bool:
        """Check if the last N tool calls are identical (same name + same args)."""
        if len(history) < threshold:
            return False
        recent = history[-threshold:]
        first = recent[0]
        return all(
            c["name"] == first["name"] and c["args"] == first["args"]
            for c in recent
        )

    def _escalation_level(self, messages: list[dict]) -> int:
        """Deduce escalation level from injected guard markers.
        0 = clean, 1 = warning, 2 = final warning."""
        msg_final = _GUARD_MESSAGES["final"]
        msg_warn = _GUARD_MESSAGES["warning"]
        for m in reversed(messages):
            if m.get("role") != "system":
                continue
            content = m.get("content", "")
            if msg_final["marker"] in content:
                return 2
            if msg_warn["marker"] in content:
                return 1
        return 0

    def _last_system_contains(self, messages: list[dict], text: str) -> bool:
        """Check if any system message contains the given text (case-insensitive)."""
        return any(
            msg.get("role") == "system"
            and text.lower() in msg.get("content", "").lower()
            for msg in messages
        )

    # ------------------------------------------------------------------
    # Injection helper
    # ------------------------------------------------------------------

    def _inject(self, messages: list[dict], message: dict) -> None:
        """Insert a message into the conversation at the configured position.

        Supports three positions:
          - prepend:      insert at index 0
          - append_user:  insert before the last user message
          - append_system: insert after the last system message
        Defaults to 'prepend' when valve is not set or unrecognised.
        """
        pos = getattr(self.valves, "INJECTION_POSITION", "append_system")
        if pos == "append_user":
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    messages.insert(i, message)
                    return
            messages.append(message)
        elif pos == "append_system":
            idx = -1
            for i, m in enumerate(messages):
                if m.get("role") == "system":
                    idx = i
            messages.insert(idx + 1, message)
        else:  # prepend (default)
            messages.insert(0, message)

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
        escalation = self._escalation_level(messages)
        max_esc = self.valves.MAX_WARNINGS_BEFORE_TERMINATE

        # --- Helper: soft-block (remove tools + inject + forward) -----
        async def _soft_block(
            reason: str,
            instruction: str,
        ):
            """Remove tools from body so the LLM cannot call more, inject
            a system message instructing it to summarise, then forward to
            the gateway. All tool results already in messages are preserved."""
            log.warning("Soft-block: %s (%d tool calls in turn)", reason, total)

            if __event_emitter__:
                try:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
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

            # Remove tools so the LLM cannot call more
            body.pop("tools", None)
            body.pop("tool_choice", None)

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
        loop_detected = (
            history
            and self.valves.MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING > 0
            and self._has_consecutive_duplicates(
                history,
                self.valves.MAX_CONSECUTIVE_SAME_TOOL_BEFORE_WARNING,
            )
        )

        if loop_detected:
            if escalation >= max_esc:
                # Already warned enough → soft-block (remove tools)
                return await _soft_block(
                    reason=(
                        f"Repeated tool call detected: {total} calls in this turn"
                    ),
                    instruction=_loop_soft_block_instruction(total),
                )
            elif escalation == 1:
                # Final warning — inject and forward normally (tools still available)
                self._inject(messages, {
                    "role": "system",
                    "content": _compose(_GUARD_MESSAGES["final"]),
                })
            else:
                # First warning — inject and forward normally
                self._inject(messages, {
                    "role": "system",
                    "content": _compose(_GUARD_MESSAGES["warning"]),
                })

        # --- Preventive reminder --------------------------------------
        if self.valves.ENABLE_PREVENTIVE_REMINDER and not loop_detected and not runaway:
            user_count = sum(1 for m in messages if m.get("role") == "user")
            if user_count > 0 and user_count % self.valves.REMINDER_INTERVAL == 0:
                if not self._last_system_contains(messages, _GUARD_MESSAGES["reminder"]["marker"]):
                    self._inject(messages, {
                        "role": "system",
                        "content": _compose(_GUARD_MESSAGES["reminder"]),
                    })

        # --- Forward to gateway ---------------------------------------
        payload = {**body, "model": real_model, "messages": messages}

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
