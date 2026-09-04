"""Unit tests for the system-prompt budget templating in agent_loop_guard.

Covers the feature that substitutes {{MAX_TOOL_CALLS_PER_TURN}} /
{{MAX_CONSECUTIVE_TOOL_CALLS}} in the model's system prompt with the
EFFECTIVE guard limits (admin valve, overridden per user via
Pipe.UserValves) before the payload is forwarded to the gateway. The
substitution uses the same `_effective_limits()` source as `_analyse()`, so
the numbers the model sees can never disagree with the thresholds the guard
enforces.

These tests run against the module without Open WebUI (pure functions).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_loop_guard import (  # noqa: E402
    _SYSTEM_TOKEN_MAX_CALLS,
    _SYSTEM_TOKEN_MAX_CONSECUTIVE,
    _resolve_budget_tokens_in_system_prompt,
    Pipe,
)


def _pipe(admin_max: int = 15, admin_loop: int = 4) -> Pipe:
    """Fresh Pipe whose admin valves are (admin_max, admin_loop)."""
    p = Pipe()
    p.valves = p.Valves(
        MAX_TOOL_CALLS_PER_TURN=admin_max,
        MAX_CONSECUTIVE_TOOL_CALLS=admin_loop,
    )
    return p


def _system(content: str) -> dict:
    return {"role": "system", "content": content}


# --- Pure helper: _resolve_budget_tokens_in_system_prompt --------------------


def test_budget_templating_replaces_tokens():
    msgs = [
        _system(
            "Budget:\n"
            f"- At most {_SYSTEM_TOKEN_MAX_CALLS} tool calls per turn.\n"
            f"- At most {_SYSTEM_TOKEN_MAX_CONSECUTIVE} consecutive identical calls."
        )
    ]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, 15, 4)
    assert replaced == 2
    text = msgs[0]["content"]
    assert _SYSTEM_TOKEN_MAX_CALLS not in text
    assert _SYSTEM_TOKEN_MAX_CONSECUTIVE not in text
    assert "At most 15 tool calls" in text
    assert "At most 4 consecutive" in text


def test_budget_templating_uses_passed_values_not_defaults():
    msgs = [_system(f"max {_SYSTEM_TOKEN_MAX_CALLS}")]
    _resolve_budget_tokens_in_system_prompt(msgs, 8, 6)
    assert msgs[0]["content"] == "max 8"


def test_budget_templating_disabled_renders_unlimited():
    # Runaway disabled (0) → "unlimited" — never tell the model to make 0 calls.
    msgs = [_system(f"max {_SYSTEM_TOKEN_MAX_CALLS}")]
    _resolve_budget_tokens_in_system_prompt(msgs, 0, 4)
    assert msgs[0]["content"] == "max unlimited"
    # Same rendering for a 0 loop (defensive; effective loop is never 0 today).
    msgs2 = [_system(f"loop {_SYSTEM_TOKEN_MAX_CONSECUTIVE}")]
    _resolve_budget_tokens_in_system_prompt(msgs2, 15, 0)
    assert msgs2[0]["content"] == "loop unlimited"


def test_budget_templating_no_system_message_noop():
    msgs = [
        {"role": "user", "content": f"hi {_SYSTEM_TOKEN_MAX_CALLS}"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "tool_call_id": "c1", "content": "res"},
    ]
    original = [dict(m) for m in msgs]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, 15, 4)
    assert replaced == 0
    assert msgs == original  # untouched, incl. the token in the user message


def test_budget_templating_no_token_noop():
    msgs = [_system("no placeholders here")]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, 15, 4)
    assert replaced == 0
    assert msgs[0]["content"] == "no placeholders here"


def test_budget_templating_list_content_skipped():
    msgs = [{"role": "system", "content": [{"type": "text", "text": "x"}]}]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, 15, 4)
    assert replaced == 0
    assert isinstance(msgs[0]["content"], list)


def test_budget_templating_only_touches_system_messages():
    user = {"role": "user", "content": f"max {_SYSTEM_TOKEN_MAX_CALLS}"}
    system = _system(f"max {_SYSTEM_TOKEN_MAX_CALLS}")
    msgs = [system, user]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, 15, 4)
    assert replaced == 1
    assert system["content"] == "max 15"
    assert user["content"] == f"max {_SYSTEM_TOKEN_MAX_CALLS}"  # untouched


def test_budget_templating_deterministic():
    template = _system(
        f"max {_SYSTEM_TOKEN_MAX_CALLS} / loop {_SYSTEM_TOKEN_MAX_CONSECUTIVE}"
    )["content"]
    a = [_system(template)]
    b = [_system(template)]
    _resolve_budget_tokens_in_system_prompt(a, 15, 4)
    _resolve_budget_tokens_in_system_prompt(b, 15, 4)
    assert a[0]["content"] == b[0]["content"]


# --- Effective limits: single source of truth with the guard -----------------


def test_effective_limits_admin_defaults():
    p = _pipe()
    assert p._effective_limits() == (15, 4)


def test_effective_limits_user_override_wins():
    p = _pipe(admin_max=15, admin_loop=4)
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=3)
    assert p._effective_limits(uv) == (3, 4)


def test_effective_limits_user_zero_defers():
    p = _pipe(admin_max=8, admin_loop=4)
    uv = p.UserValves()
    assert p._effective_limits(uv) == (8, 4)


def test_effective_limits_admin_disabled():
    p = _pipe(admin_max=0)
    assert p._effective_limits() == (0, 4)


def test_analyse_reports_same_runaway_limit_as_templating():
    """_analyse's max_calls must equal _effective_limits()[0] (criterion 2)."""
    p = _pipe(admin_max=15)
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=3)
    body = {
        "model": "pipe.deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    *_rest, max_calls_from_analyse = p._analyse(body, uv)
    assert max_calls_from_analyse == p._effective_limits(uv)[0] == 3


def test_prompt_numbers_match_guard_limits():
    """End-to-end: substituted numbers == the limits _analyse will enforce."""
    p = _pipe(admin_max=7, admin_loop=3)
    uv = p.UserValves(MAX_TOOL_CALLS_PER_TURN=5)  # effective (5, 3)
    msgs = [
        _system(
            f"max {_SYSTEM_TOKEN_MAX_CALLS}; loop {_SYSTEM_TOKEN_MAX_CONSECUTIVE}"
        )
    ]
    replaced = _resolve_budget_tokens_in_system_prompt(msgs, *p._effective_limits(uv))
    assert replaced == 2
    assert msgs[0]["content"] == "max 5; loop 3"
