"""Unit tests for the cache-safe <attached_files> cleanup in agent_loop_guard.

The cleanup is a pure function of the payload: it must be deterministic,
idempotent, and keep the history prefix byte-stable between consecutive
turns (that is what lets LLM prefix caches hit). These tests run against
the module without Open WebUI (open_webui imports are lazy inside the
pipe class only).
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_loop_guard import (
    _build_block,
    _cleanup_attached_files,
    _dedupe_tags,
    _file_dedup_key,
    _normalize_tag,
    _parse_file_tags,
)

BASE = "http://open-webui.private"
ABS = f"{BASE}/api/v1/files"


def _core_block(file_id: str, name: str = "") -> str:
    """The core's add_file_context() format: relative URL, own attrs."""
    extra = f' content_type="image/png" name="{name}"' if name else ""
    return (
        "<attached_files>\n"
        f'<file type="image" id="{file_id}" url="/api/v1/files/{file_id}/content"{extra}/>\n'
        "</attached_files>\n\n"
    )


def _union_block(*file_ids: str) -> str:
    """The image_filter's union block: absolute URLs, one tag per file."""
    lines = "\n".join(
        f'<file type="image" id="{fid}" url="{ABS}/{fid}/content"/>' for fid in file_ids
    )
    return f"<attached_files>\n{lines}\n</attached_files>\n\n"


def _user(parts: list[str]) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": p} for p in parts]}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_single_core_block():
    tags = _parse_file_tags(_core_block("f1", "one.png") + "Hi")
    assert len(tags) == 1
    assert dict(tags[0]["attrs"]) == {
        "type": "image",
        "id": "f1",
        "url": "/api/v1/files/f1/content",
        "content_type": "image/png",
        "name": "one.png",
    }


def test_parse_two_blocks_in_one_text():
    text = _core_block("f1") + _union_block("f1", "f2") + "Hi"
    tags = _parse_file_tags(text)
    assert [dict(t["attrs"])["id"] for t in tags] == ["f1", "f1", "f2"]


def test_parse_ignores_block_without_file_tags():
    assert _parse_file_tags("<attached_files>\n</attached_files>\n\nHi") == []


def test_parse_mixed_part_block_plus_text():
    tags = _parse_file_tags(_union_block("f1", "f2") + "User text here")
    assert len(tags) == 2


# ---------------------------------------------------------------------------
# Dedup keys
# ---------------------------------------------------------------------------


def _tag(attrs):
    return {"raw": "<file/>", "attrs": list(attrs.items())}


def test_dedup_key_absolute_and_relative_same_path():
    assert _file_dedup_key(_tag({"url": "/api/v1/files/abc/content"})) == _file_dedup_key(
        _tag({"url": "http://host/api/v1/files/abc/content"})
    )


def test_dedup_key_id_only():
    assert _file_dedup_key(_tag({"id": "abc"})) == "id:abc"
    assert _file_dedup_key(_tag({"id": "abc"})) == _file_dedup_key(
        _tag({"url": "/api/v1/files/abc/content"})
    )


def test_dedup_key_bare_uuid_url():
    assert _file_dedup_key(_tag({"url": "d264f103-0fb6-480e-a310-dcb62f10ef30"})) == (
        "id:d264f103-0fb6-480e-a310-dcb62f10ef30"
    )


def test_dedup_key_external_url():
    assert _file_dedup_key(_tag({"url": "https://cdn.example.com/img.png"})) == (
        "url:https://cdn.example.com/img.png"
    )


def test_dedup_key_placeholder_never_deduped():
    assert _file_dedup_key(_tag({"url": "(base64 stripped)"})) == ""
    assert _file_dedup_key(_tag({"id": "(base64 stripped)"})) == ""


def test_dedupe_tags_keeps_placeholders():
    tags = [
        _tag({"url": "/api/v1/files/abc/content"}),
        _tag({"url": "http://h/api/v1/files/abc/content"}),
        _tag({"url": "(base64 stripped)"}),
    ]
    kept = _dedupe_tags(tags, set())
    assert [dict(t["attrs"])["url"] for t in kept] == ["/api/v1/files/abc/content", "(base64 stripped)"]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_relative_url():
    tag = _tag({"type": "image", "id": "f1", "url": "/api/v1/files/f1/content"})
    assert _normalize_tag(tag, BASE) == (
        '<file type="image" id="f1" url="http://open-webui.private/api/v1/files/f1/content"/>'
    )


def test_normalize_leaves_absolute_and_data_uris():
    assert _normalize_tag(_tag({"url": "http://h/api/v1/files/a/content"}), BASE).endswith(
        'url="http://h/api/v1/files/a/content"/>'
    )
    assert _normalize_tag(_tag({"url": "data:image/png;base64,AAA"}), BASE).endswith(
        'url="data:image/png;base64,AAA"/>'
    )


def test_normalize_without_base_url_returns_raw():
    tag = _tag({"url": "/api/v1/files/f1/content"})
    assert _normalize_tag(tag, "") == tag["raw"]


def test_build_block_core_format():
    block = _build_block([_tag({"type": "image", "id": "f1", "url": "/api/v1/files/f1/content"})], BASE)
    assert block == (
        "<attached_files>\n"
        '<file type="image" id="f1" url="http://open-webui.private/api/v1/files/f1/content"/>\n'
        "</attached_files>\n\n"
    )
    assert _build_block([], BASE) == ""


# ---------------------------------------------------------------------------
# Cleanup semantics
# ---------------------------------------------------------------------------


def test_historical_core_block_normalized_when_alone():
    messages = [_user([_core_block("f1"), "First?"])]
    _cleanup_attached_files(messages, BASE)
    assert messages[0]["content"][0]["text"] == _core_block("f1").replace(
        "/api/v1/files/f1/content", f"{ABS}/f1/content"
    )
    assert messages[0]["content"][1]["text"] == "First?"


def test_last_message_collapses_blocks_and_drops_old_files():
    messages = [
        _user([_core_block("f1"), "First?"]),
        {"role": "assistant", "content": "Answer 1"},
        _user([_core_block("f2"), _union_block("f1", "f2") + "Second?"]),
    ]
    _cleanup_attached_files(messages, BASE)

    u2_text = "\n".join(p["text"] for p in messages[2]["content"] if p.get("type") == "text")
    assert "id=\"f1\"" not in u2_text
    assert "id=\"f2\"" in u2_text
    assert u2_text.count("<attached_files>") == 1  # blocks collapsed into one
    assert u2_text.endswith("Second?")


def test_cross_message_dedup_str_content():
    messages = [
        {"role": "user", "content": _core_block("f1") + "First?"},
        {"role": "user", "content": _core_block("f2") + _union_block("f1", "f2") + "Second?"},
    ]
    _cleanup_attached_files(messages, BASE)
    u2 = messages[1]["content"]
    assert "id=\"f1\"" not in u2
    assert "id=\"f2\"" in u2
    assert u2.endswith("Second?")


def test_re_attached_file_tagged_only_at_first_occurrence():
    messages = [
        {"role": "user", "content": _core_block("f1") + "First?"},
        {"role": "user", "content": _core_block("f1") + "Re-attach?"},
    ]
    _cleanup_attached_files(messages, BASE)
    assert "id=\"f1\"" in messages[0]["content"]
    assert "id=\"f1\"" not in messages[1]["content"]


def test_placeholder_preserved_and_not_deduped():
    placeholder_block = "<attached_files>\n<file type=\"image\" url=\"(base64 stripped)\"/>\n</attached_files>\n\n"
    messages = [
        _user([_core_block("f1"), "a"]),
        _user([_union_block("f1", "f2"), placeholder_block + "b"]),
    ]
    _cleanup_attached_files(messages, BASE)
    u2 = "\n".join(p["text"] for p in messages[1]["content"] if p.get("type") == "text")
    assert u2.count("(base64 stripped)") == 1
    assert "id=\"f1\"" not in u2  # f1 already tagged in u1


def test_empty_content_after_strip_gets_empty_text_part():
    messages = [{"role": "user", "content": [{"type": "text", "text": _core_block("f1")}]}]
    _cleanup_attached_files(messages, BASE)
    assert messages[0]["content"][0]["text"].startswith("<attached_files>")  # f1 kept (earliest)


def test_non_user_messages_untouched():
    messages = [
        {"role": "system", "content": "sys"},
        _user([_core_block("f1"), "a"]),
        {"role": "assistant", "content": "ans"},
        {"role": "tool", "content": _core_block("f1")},  # never injected into tool msgs, but must not be touched
    ]
    _cleanup_attached_files(messages, BASE)
    assert messages[3]["content"] == _core_block("f1")


def test_idempotent():
    messages = [
        _user([_core_block("f1"), "First?"]),
        _user([_core_block("f2"), _union_block("f1", "f2") + "Second?"]),
    ]
    _cleanup_attached_files(messages, BASE)
    once = copy.deepcopy(messages)
    _cleanup_attached_files(messages, BASE)
    assert messages == once


def test_no_blocks_noop():
    messages = [{"role": "user", "content": [{"type": "text", "text": "plain"}]}]
    original = copy.deepcopy(messages)
    _cleanup_attached_files(messages, BASE)
    assert messages == original


def test_fail_open_on_malformed_input():
    messages = [
        {"role": "user", "content": None},
        {"role": "user", "content": 42},
        {"role": "user"},
        "not-a-dict",
    ]
    _cleanup_attached_files(messages, BASE)  # must not raise


# ---------------------------------------------------------------------------
# Cache-safety regression: the history prefix must be byte-stable
# ---------------------------------------------------------------------------
#
# Real conversation, one new file per turn. The image_filter's union block
# moves to the last user message every turn; the core's per-message blocks
# stay put. Cleanup must leave every shared history message byte-identical
# between turn N and turn N+1.


def _turn_payload(include_next_turn: bool) -> list[dict]:
    """Payload as seen in turn 3 (history up to u3) and in turn 4 (same
    history + a3 + u4). In turn 3, u2/u3 carry their own core block PLUS
    the filter's union block; in turn 4 the union block has moved to u4."""
    messages = [
        _user([_core_block("f1"), "First?"]),
        {"role": "assistant", "content": "Answer 1"},
        _user([_core_block("f2"), "Second?"]),
        {"role": "assistant", "content": "Answer 2"},
    ]
    if include_next_turn:
        # turn 4: history u1..u3 unchanged (core blocks only), union moved to u4
        messages.append(_user([_core_block("f3"), "Third?"]))
        messages.append({"role": "assistant", "content": "Answer 3"})
        messages.append(_user([_core_block("f4"), _union_block("f1", "f2", "f3", "f4") + "Fourth?"]))
    else:
        # turn 3: u2/u3 carry the union block too
        messages[2]["content"][1]["text"] = _union_block("f1", "f2") + "Second?"
        messages.append(_user([_core_block("f3"), _union_block("f1", "f2", "f3") + "Third?"]))
    return messages


def test_history_prefix_byte_stable_between_turns():
    """The core cache-safety guarantee.

    Turn 3 and turn 4 share the history u1, a1, u2, a2, u3. After cleanup
    every shared message must be byte-identical — otherwise the provider's
    prefix cache misses on the whole conversation.
    """
    turn3 = _turn_payload(include_next_turn=False)
    turn4 = _turn_payload(include_next_turn=True)

    _cleanup_attached_files(turn3, BASE)
    _cleanup_attached_files(turn4, BASE)

    assert turn3[0] == turn4[0]  # u1
    assert turn3[1] == turn4[1]  # a1
    assert turn3[2] == turn4[2]  # u2 — lost the union block between turns
    assert turn3[3] == turn4[3]  # a2
    assert turn3[4] == turn4[4]  # u3 — lost the union block between turns

    # Sanity: u2/u3 must still show their OWN file (f2/f3), now deduplicated.
    u2 = "\n".join(p["text"] for p in turn3[2]["content"] if p.get("type") == "text")
    u3 = "\n".join(p["text"] for p in turn3[4]["content"] if p.get("type") == "text")
    assert "id=\"f2\"" in u2 and "id=\"f1\"" not in u2
    assert "id=\"f3\"" in u3 and "id=\"f1\"" not in u3
    # The last message of turn 4 keeps only its genuinely new file.
    u4 = "\n".join(p["text"] for p in turn4[6]["content"] if p.get("type") == "text")
    assert "id=\"f4\"" in u4 and "id=\"f1\"" not in u4 and "id=\"f2\"" not in u4 and "id=\"f3\"" not in u4


def test_history_prefix_stable_without_base_url():
    turn3 = _turn_payload(include_next_turn=False)
    turn4 = _turn_payload(include_next_turn=True)
    _cleanup_attached_files(turn3, "")
    _cleanup_attached_files(turn4, "")
    assert turn3[2] == turn4[2]
    assert turn3[4] == turn4[4]
