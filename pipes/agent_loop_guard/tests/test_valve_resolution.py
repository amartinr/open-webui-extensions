"""Unit tests for valve resolution in agent_loop_guard.

Covers the fix for the "UserValves is inert" discrepancy: Open WebUI
delivers per-user pipe valves under `__user__["valves"]` (verified in
open_webui/functions.py, v0.11.x and main), and `self.valves` is the
function's stored ADMIN configuration (Open WebUI overwrites it on every
request). The effective limit is the user override when non-zero, else the
admin value; an admin MAX_TOOL_CALLS_PER_TURN of 0 disables the runaway
guard (a non-zero user override re-enables it for that user only).

These tests run against the module without Open WebUI (the pipe class is
instantiated directly; admin "config" is simulated by overwriting
`self.valves`, exactly as Open WebUI does at request time).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_loop_guard import Pipe  # noqa: E402


def _pipe(admin_max: int = 15, admin_loop: int = 4) -> Pipe:
    """Fresh Pipe whose admin valves are (admin_max, admin_loop).

    Mirrors Open WebUI: `instance.valves = Valves(**stored_admin_config)`.
    """
    p = Pipe()
    p.valves = p.Valves(
        MAX_TOOL_CALLS_PER_TURN=admin_max,
        MAX_CONSECUTIVE_TOOL_CALLS=admin_loop,
    )
    return p


def _turn(n_calls: int, identical: bool = True) -> dict:
    """A single-turn tool-calling history with `n_calls` real calls.

    `identical=True` → every call has the same name AND arguments (loop
    detector material); `identical=False` → arguments vary (loop detector
    inert, only runaway can fire).
    """
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(n_calls):
        cid = f"call-{i}"
        args = "{}" if identical else json.dumps({"q": f"query-{i}"})
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": "web_search", "arguments": args},
                    }
                ],
            }
        )
        msgs.append(
            {"role": "tool", "tool_call_id": cid, "content": f"result {i}"}
        )
    return {"model": "pipe.deepseek/deepseek-v4-flash", "messages": msgs}


# --- _extract_user_valves ---------------------------------------------------


def test_no_user_dict_means_no_override():
    p = _pipe()
    assert p._extract_user_valves(None) is None
    assert p._extract_user_valves({}) is None
    assert p._extract_user_valves({"id": "u1", "role": "user"}) is None


def test_user_valves_instance_passthrough():
    p = _pipe()
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=5)
    assert p._extract_user_valves({"id": "u1", "valves": uv}) is uv


def test_user_valves_as_plain_dict():
    p = _pipe()
    uv = p._extract_user_valves({"valves": {"MAX_TOOL_CALLS_PER_TURN": 5}})
    assert uv is not None
    assert uv.MAX_TOOL_CALLS_PER_TURN == 5
    assert uv.MAX_CONSECUTIVE_TOOL_CALLS == 0


def test_user_valves_unknown_keys_dropped():
    p = _pipe()
    uv = p._extract_user_valves(
        {"valves": {"MAX_TOOL_CALLS_PER_TURN": 5, "NOT_A_VALVE": "x"}}
    )
    assert uv is not None
    assert uv.MAX_TOOL_CALLS_PER_TURN == 5


def test_user_valves_garbage_fails_open():
    p = _pipe()
    assert p._extract_user_valves({"valves": 42}) is None
    assert p._extract_user_valves({"valves": "MAX_TOOL_CALLS_PER_TURN=5"}) is None


# --- Effective limits through _analyse --------------------------------------


def test_admin_defaults_loop_fires_at_four_identical():
    p = _pipe()
    body = _turn(n_calls=4, identical=True)
    should_block, tool, kind, total, max_calls = p._analyse(body)
    assert should_block is True
    assert kind == "loop"
    assert tool == "web_search"
    assert total == 4
    assert max_calls == 15  # the admin runaway value is reported


def test_admin_defaults_runaway_fires_at_fifteen():
    p = _pipe()
    body = _turn(n_calls=15, identical=False)
    should_block, _, kind, total, max_calls = p._analyse(body)
    assert should_block is True
    assert kind == "runaway"
    assert total == 15
    assert max_calls == 15


def test_user_override_lowers_runaway_threshold():
    p = _pipe(admin_max=15)
    body = _turn(n_calls=2, identical=False)
    # Without an override: 2 calls are nowhere near the admin runaway (15).
    assert p._analyse(body)[0] is False
    # With a per-user override of 2 the same history trips runaway.
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=2)
    should_block, _, kind, total, max_calls = p._analyse(body, uv)
    assert should_block is True
    assert kind == "runaway"
    assert max_calls == 2


def test_user_zero_defers_to_admin():
    p = _pipe(admin_max=15)
    body = _turn(n_calls=4, identical=False)
    uv = p.UserValves()  # 0 / 0 → defer to admin
    assert p._analyse(body, uv)[0] is False


def test_admin_disabled_runaway_does_not_fire():
    p = _pipe(admin_max=0)
    body = _turn(n_calls=20, identical=False)
    assert p._analyse(body)[0] is False  # runaway off, loop inert (distinct args)


def test_admin_disabled_but_user_override_reenables():
    p = _pipe(admin_max=0)
    body = _turn(n_calls=3, identical=False)
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=3)
    should_block, _, kind, total, max_calls = p._analyse(body, uv)
    assert should_block is True
    assert kind == "runaway"
    assert max_calls == 3


def test_user_loop_override_tightens_loop_threshold():
    p = _pipe(admin_loop=4)
    body = _turn(n_calls=2, identical=True)
    # Admin loop = 4: two identical calls are safe.
    assert p._analyse(body)[0] is False
    uv = p.UserValves(MAX_CONSECUTIVE_TOOL_CALLS=2)
    should_block, _, kind, total, max_calls = p._analyse(body, uv)
    assert should_block is True
    assert kind == "loop"
    assert total == 2


def test_full_request_shape_end_to_end():
    """The shape Open WebUI really sends: __user__ dict with 'valves'."""
    p = _pipe(admin_max=15)
    body = _turn(n_calls=2, identical=False)
    __user__ = {"id": "u1", "name": "Ana", "valves": {"MAX_TOOL_CALLS_PER_TURN": 2}}
    uv = p._extract_user_valves(__user__)
    assert uv is not None
    should_block, _, kind, _, _ = p._analyse(body, uv)
    assert should_block is True
    assert kind == "runaway"
