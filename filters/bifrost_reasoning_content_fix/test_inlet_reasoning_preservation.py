"""Unit tests for the inlet reasoning-preservation logic (v3.2.0).

Ports the verified DeepSeek-behind-Bifrost fix from
pi-bifrost-reasoning-fix: once a history contains an assistant tool call
(or the request carries tools), every assistant message must carry
`reasoning_content` — otherwise DeepSeek silently drops reasoning on the
next turn. Empirically validated against a live Bifrost endpoint:

  - tool-call history + assistant WITHOUT reasoning_content  -> reasoning lost
  - tool-call history + assistant WITH reasoning_content (even "") -> reasoning kept
  - tool-call history + assistant with reasoning_details (Bifrost dialect)
    -> reasoning lost (Bifrost does not translate the field)

Runs standalone (no Open WebUI import needed for the helpers).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bifrost_reasoning_content_fix import (
    _force_reasoning_content_on_tools,
    _has_bifrost_residue,
    _history_has_tool_calls,
    _normalize_assistant_message,
)

MODEL = {"id": "deepseek/deepseek-v4-flash"}


def _assistant(**kw):
    msg = {"role": "assistant", "content": ""}
    msg.update(kw)
    return msg


def test_residue_detection_matches_pi():
    """reasoning counts even empty; reasoning_details counts as a list."""
    assert _has_bifrost_residue({"role": "assistant", "reasoning": "thought"})
    assert _has_bifrost_residue({"role": "assistant", "reasoning": ""})  # pi: typeof string
    assert _has_bifrost_residue(
        {"role": "assistant", "reasoning_details": [{"type": "reasoning.text", "text": "x"}]}
    )
    assert _has_bifrost_residue({"role": "assistant", "reasoning_details": []})  # pi: isArray
    assert not _has_bifrost_residue({"role": "assistant", "content": "hi"})
    assert not _has_bifrost_residue({"role": "assistant", "reasoning_content": "ok"})


def test_normalize_assistant_converts_both_dialects():
    msg = _assistant(
        reasoning="plain",
        reasoning_details=[{"type": "reasoning.text", "text": "block"}],
    )
    out = _normalize_assistant_message(msg)
    assert out["reasoning_content"] == "plain"
    assert "reasoning" not in out
    assert "reasoning_details" not in out

    # details are the only source -> concatenated
    out = _normalize_assistant_message(
        _assistant(reasoning_details=[{"type": "reasoning.text", "text": "a"}, {"type": "reasoning.text", "text": "b"}])
    )
    assert out["reasoning_content"] == "ab"

    # empty reasoning string becomes empty reasoning_content (not dropped)
    out = _normalize_assistant_message(_assistant(reasoning=""))
    assert out["reasoning_content"] == ""
    assert "reasoning" not in out


def test_history_has_tool_calls():
    assert not _history_has_tool_calls([_assistant(content="hi"), {"role": "user", "content": "q"}])
    assert _history_has_tool_calls(
        [_assistant(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}])]
    )
    assert not _history_has_tool_calls([_assistant(tool_calls=[])])
    assert not _history_has_tool_calls([{"role": "user", "content": "q", "tool_calls": [{}]}])


def test_force_reasoning_content_on_tools():
    msgs = [_assistant(content="a"), _assistant(content="b", reasoning_content="rc"), {"role": "user", "content": "q"}]
    _force_reasoning_content_on_tools(msgs)
    assert msgs[0]["reasoning_content"] == ""
    assert msgs[1]["reasoning_content"] == "rc"  # untouched
    assert "reasoning_content" not in msgs[2]

    # non-string reasoning_content is replaced with an empty string
    msgs = [_assistant(reasoning_content=["odd"])]
    _force_reasoning_content_on_tools(msgs)
    assert msgs[0]["reasoning_content"] == ""


def _run_inlet(body, model=MODEL):
    from bifrost_reasoning_content_fix import Filter

    filt = Filter()
    return asyncio.run(filt.inlet(body, model))


def test_inlet_renames_residue_and_forces_on_tool_history():
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
    out = _run_inlet(body)
    msgs = out["messages"]
    assert msgs[1]["reasoning_content"] == "t1"
    assert "reasoning_details" not in msgs[1]
    assert msgs[3]["reasoning_content"] == ""  # tool-calling assistant forced
    assert msgs[2].get("role") == "user" and "reasoning_content" not in msgs[2]


def test_inlet_forces_when_tools_present_but_no_calls_yet():
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1"),
            {"role": "user", "content": "q2"},
        ],
    }
    out = _run_inlet(body)
    assert out["messages"][1]["reasoning_content"] == ""


def test_inlet_leaves_history_untouched_without_tools_or_calls():
    body = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "q1"},
            _assistant(content="r1", reasoning_content="rc"),
            {"role": "user", "content": "q2"},
        ],
    }
    out = _run_inlet(body)
    assert out["messages"][1] == {"role": "assistant", "content": "r1", "reasoning_content": "rc"}


def test_inlet_ignores_non_target_models():
    body = {
        "model": "other/model",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [{"role": "user", "content": "q1"}, _assistant(content="r1")],
    }
    out = _run_inlet(body, {"id": "other/model"})
    assert out["messages"][1] == {"role": "assistant", "content": "r1"}


def test_fix_delta_converts_and_strips_details():
    """v3.5.0: reasoning_details are stripped from the delta so Open
    WebUI keeps emitting response.reasoning_text.delta events (its handler
    suppresses them when details are present). Text lands in
    reasoning_content; the empty-string fallback covers Bifrost #974."""
    from bifrost_reasoning_content_fix import _fix_delta

    # normal fragment: reasoning + duplicated details -> converted, details stripped
    delta = {
        "reasoning": "El usuario",
        "reasoning_details": [{"index": 0, "type": "reasoning.text", "text": "El usuario"}],
    }
    _fix_delta(delta)
    assert delta["reasoning_content"] == "El usuario"
    assert "reasoning" not in delta
    assert "reasoning_details" not in delta  # stripped: keeps SSE streaming alive

    # empty reasoning + details with text (Bifrost #974 case) -> details used
    delta = {
        "reasoning": "",
        "reasoning_details": [{"index": 0, "type": "reasoning.text", "text": "primera parte"}],
    }
    _fix_delta(delta)
    assert delta["reasoning_content"] == "primera parte"
    assert "reasoning" not in delta and "reasoning_details" not in delta

    # details absent -> nothing
    delta = {"reasoning": "solo texto"}
    _fix_delta(delta)
    assert delta["reasoning_content"] == "solo texto"
    assert "reasoning" not in delta and "reasoning_details" not in delta


def test_fix_delta_no_duplication_bifrost2():
    """Bifrost core >= 1.8.0 emits each reasoning fragment in THREE fields at
    once (reasoning, reasoning_content, reasoning_details — same text).
    _fix_delta must keep reasoning_content as-is and strip the redundant
    fields, never re-append (which doubled the fragment and quadrupled it
    across pipe + filter).
    """
    from bifrost_reasoning_content_fix import _fix_delta

    delta = {
        "reasoning": "Let",
        "reasoning_content": "Let",
        "reasoning_details": [{"type": "reasoning.text", "text": "Let"}],
    }
    out = _fix_delta(dict(delta))
    assert out == {"reasoning_content": "Let"}

    delta = {"reasoning": "Legacy", "reasoning_details": [{"type": "reasoning.text", "text": "Legacy"}]}
    out = _fix_delta(dict(delta))
    assert out == {"reasoning_content": "Legacy"}

    delta = {"reasoning": "", "reasoning_content": "", "reasoning_details": [{"type": "reasoning.text", "text": ""}]}
    out = _fix_delta(dict(delta))
    assert out == {"reasoning_content": ""}
