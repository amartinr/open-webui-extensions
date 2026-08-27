"""Unit tests for the Bifrost reasoning normalization in agent_loop_guard.

The pipe is the single choke point that sees every outbound request to the
gateway — including Open WebUI tool-call continuations, which bypass filter
inlets. It must therefore apply the same normalization as
pi-bifrost-reasoning-fix / the bifrost_reasoning_content_fix filter inlet:

  - rename assistant `reasoning` / `reasoning_details` -> `reasoning_content`
  - once tool-calling is in scope (request tools or tool-call history),
    force `reasoning_content` (even "") on every assistant message

DeepSeek drops reasoning on the next turn when a tool-calling history
replays an assistant without `reasoning_content` (validated against a live
Bifrost endpoint: tool_calls + reasoning_details -> lost; tool_calls +
reasoning_content "" -> kept).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_loop_guard import (
    _force_reasoning_content_on_tools,
    _has_bifrost_residue,
    _history_has_tool_calls,
    _normalize_reasoning_for_gateway,
    _normalize_reasoning_message,
)


def _assistant(**kw):
    msg = {"role": "assistant", "content": ""}
    msg.update(kw)
    return msg


def test_residue_detection():
    assert _has_bifrost_residue({"role": "assistant", "reasoning": "t"})
    assert _has_bifrost_residue({"role": "assistant", "reasoning": ""})
    assert _has_bifrost_residue(
        {"role": "assistant", "reasoning_details": [{"type": "reasoning.text", "text": "x"}]}
    )
    assert _has_bifrost_residue({"role": "assistant", "reasoning_details": []})
    assert not _has_bifrost_residue({"role": "assistant", "content": "hi"})
    assert not _has_bifrost_residue({"role": "assistant", "reasoning_content": "ok"})


def test_normalize_reasoning_message():
    out = _normalize_reasoning_message(
        _assistant(reasoning="plain", reasoning_details=[{"type": "reasoning.text", "text": "block"}])
    )
    assert out["reasoning_content"] == "plain"
    assert "reasoning" not in out and "reasoning_details" not in out

    out = _normalize_reasoning_message(
        _assistant(reasoning_details=[{"type": "reasoning.text", "text": "a"}, {"type": "reasoning.text", "text": "b"}])
    )
    assert out["reasoning_content"] == "ab"

    out = _normalize_reasoning_message(_assistant(reasoning=""))
    assert out["reasoning_content"] == ""
    assert "reasoning" not in out


def test_history_has_tool_calls():
    assert not _history_has_tool_calls([_assistant(content="hi")])
    assert _history_has_tool_calls(
        [_assistant(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}])]
    )
    assert not _history_has_tool_calls([_assistant(tool_calls=[])])


def test_force_reasoning_content_on_tools():
    msgs = [_assistant(content="a"), _assistant(content="b", reasoning_content="rc"), {"role": "user", "content": "q"}]
    _force_reasoning_content_on_tools(msgs)
    assert msgs[0]["reasoning_content"] == ""
    assert msgs[1]["reasoning_content"] == "rc"
    assert "reasoning_content" not in msgs[2]


def test_gateway_normalization_tool_call_continuation():
    """Open WebUI tool-call continuation: assistant tool_calls WITHOUT
    reasoning, plus a previous assistant carrying Bifrost reasoning_details."""
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1", reasoning_details=[{"type": "reasoning.text", "text": "t1"}]),
            {"role": "user", "content": "q2"},
            _assistant(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "q3"},
        ],
    }
    _normalize_reasoning_for_gateway(body)
    msgs = body["messages"]
    assert msgs[1]["reasoning_content"] == "t1"
    assert "reasoning_details" not in msgs[1]
    assert msgs[3]["reasoning_content"] == ""  # tool-calling assistant forced
    assert msgs[4] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}


def test_gateway_normalization_tools_present_no_calls():
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [{"role": "user", "content": "q1"}, _assistant(content="r1"), {"role": "user", "content": "q2"}],
    }
    _normalize_reasoning_for_gateway(body)
    assert body["messages"][1]["reasoning_content"] == ""


def test_gateway_normalization_untouched_without_tools_or_calls():
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1", reasoning_content="rc"),
            {"role": "user", "content": "q2"},
        ],
    }
    _normalize_reasoning_for_gateway(body)
    assert body["messages"][1] == {"role": "assistant", "content": "r1", "reasoning_content": "rc"}


def test_gateway_normalization_is_deterministic():
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1"),
            {"role": "user", "content": "q2"},
        ],
    }
    _normalize_reasoning_for_gateway(body)
    first = body["messages"][1]
    body2 = {
        "model": "deepseek/deepseek-v4-flash",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1"),
            {"role": "user", "content": "q2"},
        ],
    }
    _normalize_reasoning_for_gateway(body2)
    assert body["messages"] == body2["messages"]


def test_reasoning_effort_upgrade_logic():
    """v2.11.0: low/absent effort for deepseek models is upgraded to the
    valve target; high/max and non-deepseek models are untouched."""
    from agent_loop_guard import Pipe

    p = Pipe()
    p.valves = Pipe.Valves(REASONING_EFFORT="high")

    # low -> upgraded
    body = {"model": "deepseek/deepseek-v4-flash", "reasoning_effort": "low"}
    assert p._upgrade_reasoning_effort(body) is True
    assert body["reasoning_effort"] == "high"

    # absent -> set
    body = {"model": "deepseek/deepseek-v4-flash"}
    assert p._upgrade_reasoning_effort(body) is True
    assert body["reasoning_effort"] == "high"

    # high -> untouched
    body = {"model": "deepseek/deepseek-v4-flash", "reasoning_effort": "high"}
    assert p._upgrade_reasoning_effort(body) is False
    assert body["reasoning_effort"] == "high"

    # max -> untouched
    body = {"model": "deepseek/deepseek-v4-flash", "reasoning_effort": "max"}
    assert p._upgrade_reasoning_effort(body) is False
    assert body["reasoning_effort"] == "max"

    # non-deepseek model -> untouched
    body = {"model": "anthropic/claude-haiku-4-5", "reasoning_effort": "low"}
    assert p._upgrade_reasoning_effort(body) is False
    assert body["reasoning_effort"] == "low"

    # valve disabled (empty) -> untouched
    p2 = Pipe()
    p2.valves = Pipe.Valves(REASONING_EFFORT="")
    body = {"model": "deepseek/deepseek-v4-flash", "reasoning_effort": "low"}
    assert p2._upgrade_reasoning_effort(body) is False
    assert body["reasoning_effort"] == "low"
