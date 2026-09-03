"""Citation source emission — Open WebUI native fetch_url parity.

Covers the fix for the "sources have ids but no content" failure mode
(ISSUES.md #2):
- ``_source_document`` mirrors core's ``get_citation_source_from_tool_result``:
  ``document`` holds the first ``SOURCE_DOCUMENT_SNIPPET_CHARS`` chars of the
  fetched content (native fetch_url truncates to 500);
- ``_batch_result_content`` extracts citeable content out of batch result
  bodies (metadata block stripped) and yields "" for error/empty results;
- ``_emit_sources`` emits one source event per URL that has readable content
  — with that content in ``document`` — and emits NO source for URLs whose
  content is empty (errors, empty extractions, dropped results).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_fetch_url import SOURCE_DOCUMENT_SNIPPET_CHARS, Tools, FetchResult


# ── _source_document (document array = real snippet, native parity) ──


def test_source_document_short_content_untouched():
    tools = Tools()
    assert tools._source_document("hello world") == ["hello world"]


def test_source_document_truncates_at_500_like_core_fetch_url():
    tools = Tools()
    long = "x" * (SOURCE_DOCUMENT_SNIPPET_CHARS + 50)
    assert tools._source_document(long) == ["x" * SOURCE_DOCUMENT_SNIPPET_CHARS + "..."]


def test_source_document_empty_is_well_formed():
    tools = Tools()
    assert tools._source_document("") == [""]
    assert tools._source_document(None) == [""]


# ── _batch_result_content (strip metadata block) ─────────────────────


def test_batch_content_strips_metadata_block():
    tools = Tools()
    body = "> Status: HTTP 200 OK\n> URL: https://example.com\n\nReal content here."
    assert tools._batch_result_content(body) == "Real content here."


def test_batch_content_keeps_json_whole():
    tools = Tools()
    body = '{"Status": "HTTP 200 OK", "content": "hi"}'
    assert tools._batch_result_content(body) == body


def test_batch_content_error_body_is_empty():
    tools = Tools()
    body = "> Status: ❌ Timeout after 15s\n> URL: https://example.com\n"
    assert tools._batch_result_content(body) == ""


def test_batch_content_binary_placeholder_kept():
    tools = Tools()
    body = (
        "> Status: HTTP 200 OK\n\n"
        "[Non-text content (image/png). Content not displayed to avoid context pollution.]"
    )
    assert tools._batch_result_content(body) != ""


# ── _emit_sources: one source per URL WITH readable content ──────────


class _FakeEmitter:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def _emit(tools, urls, documents=None):
    async def scenario():
        emitter = _FakeEmitter()
        await tools._emit_sources(emitter, urls, documents)
        return emitter.events

    return asyncio.run(scenario())


def test_emit_sources_sends_real_content_in_document():
    tools = Tools()
    events = _emit(tools, ["https://a.com"], ["This is the fetched content."])
    assert len(events) == 1
    data = events[0]["data"]
    assert data["source"] == {"name": "https://a.com", "id": "https://a.com"}
    assert data["document"] == ["This is the fetched content."]
    assert data["metadata"] == [
        {"source": "https://a.com", "name": "https://a.com", "url": "https://a.com"}
    ]


def test_emit_sources_skips_urls_without_content():
    tools = Tools()
    events = _emit(
        tools,
        ["https://a.com", "https://b.com", "https://c.com"],
        ["has content", "", "   "],
    )
    assert [e["data"]["source"]["id"] for e in events] == ["https://a.com"]


def test_emit_sources_no_documents_emits_nothing():
    tools = Tools()
    assert _emit(tools, ["https://a.com"]) == []
    assert _emit(tools, ["https://a.com"], []) == []


def test_emit_sources_short_documents_list_treated_as_empty_tail():
    tools = Tools()
    events = _emit(tools, ["https://a.com", "https://b.com"], ["only a"])
    assert [e["data"]["source"]["id"] for e in events] == ["https://a.com"]


def test_emit_sources_none_emitter_is_noop():
    async def scenario():
        await Tools()._emit_sources(None, ["https://a.com"], ["x"])

    asyncio.run(scenario())  # must not raise


# ── end-to-end shape: batch body → citation document ─────────────────


def test_citation_document_derived_from_batch_body():
    """The document OWUI receives mirrors the actual fetched content."""
    tools = Tools()
    body = (
        "> Status: HTTP 200 OK\n> URL: https://docs.example.com/x\n\n"
        "First line of real content.\nMore content."
    )
    content = tools._batch_result_content(body)
    events = _emit(tools, ["https://docs.example.com/x"], [content])
    assert events[0]["data"]["document"] == [content]
    assert "First line of real content." in events[0]["data"]["document"][0]


# ── _execute_fetch binary branch (unbound-content regression) ─────────


def test_binary_fetch_emits_source_with_explainer_not_empty():
    """The binary branch previously passed an unbound `content` local to
    _emit_sources (NameError at runtime). The explainer text must be used
    both as the output body and as the citation document."""
    html = "<html><body>irrelevant</body></html>"

    class _FakeEmitter:
        def __init__(self):
            self.events = []

        async def __call__(self, event):
            self.events.append(event)

    class _BinaryTools(Tools):
        def __init__(self):
            super().__init__()
            self._fr = FetchResult(
                raw_html=html,
                final_url="https://x.test/img.png",
                status_code=200,
                content_type="image/png",
                resp_headers={},
                raw_bytes=b"\x89PNG",
            )

        async def _fetch_with_fingerprint(self, **kwargs):
            return self._fr

    async def scenario():
        emitter = _FakeEmitter()
        t = _BinaryTools()
        out = await t._execute_fetch(
            url="https://x.test/img.png",
            browser="firefox",
            timeout_ms=8000,
            format="markdown",
            max_chars=0,
            include_replies=False,
            verbose=False,
            __event_emitter__=emitter,
            _start_time=0.0,
            cache_cfg=None,
        )
        return out, emitter.events

    out, events = asyncio.run(scenario())
    assert "Non-text content (image/png)" in out
    sources = [e for e in events if e.get("type") == "source"]
    assert len(sources) == 1
    doc = sources[0]["data"]["document"][0]
    assert "Non-text content (image/png)" in doc
