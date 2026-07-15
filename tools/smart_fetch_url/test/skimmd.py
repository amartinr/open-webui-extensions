#!/usr/bin/env python3
"""
skimmd — Skimmed Markdown converter.

A whitelist-based HTML-to-Markdown converter with zero external dependencies
(stdlib only: ``html.parser``, ``re``, ``urllib.parse``).

The name stands for **Skimmed Markdown** — like skimmed milk: remove the fat
(cruft, navigation, external promotions), keep the protein (content, links,
images, video).

When ``strip_external=True`` (the default), the parser operates at the block
level: any block container (``<p>``, ``<div>``, ``<li>``, …) that contains
*only* external links is discarded entirely.  Mixed blocks (internal + external)
keep internal content and drop external link anchor text.

Typical usage::

    >>> import skimmd
    >>> html = '<p>Visit <a href="http://redlib.private/page">us</a></p>'
    >>> skimmd.skimmd(html, base_url="http://redlib.private")
    'Visit [us](http://redlib.private/page)'
"""

from html.parser import HTMLParser
import re
from urllib.parse import urlparse


# ── Whitelist ────────────────────────────────────────────────────────────────
# Only these tags survive the filter.  Everything else is stripped (noisy tags
# like <script>/<style> are decomposed; everything else is unwrapped).

WHITELIST = frozenset({
    "a", "img", "video", "source", "picture",
    "p", "br", "hr", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "tr", "td", "th", "thead", "tbody", "caption",
    "strong", "em", "b", "i", "u", "span", "mark", "small",
    "div", "section", "article", "figure", "figcaption",
    "blockquote", "cite", "q",
})

# Tags whose entire content (including descendants) is removed.
NOISY_TAGS = frozenset({
    "script", "style", "nav", "footer", "header",
    "noscript", "iframe", "meta", "link", "svg", "form",
})

# HTML void elements that never have an end tag.
# When a void element is in NOISY_TAGS we must NOT increment skip_depth,
# because ``HTMLParser`` will never call ``handle_endtag`` for it.
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# Tags that delimit block-level boundaries.  Each time one starts or ends the
# internal buffer is flushed, making block-granularity decisions about external
# content possible.
BLOCK_CONTAINERS = frozenset({
    "p", "div", "section", "article", "figure", "figcaption",
    "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "td", "th",
})

# Tags that reset list nesting state.
LIST_TAGS = frozenset({"ul", "ol"})


# ── HTML entity map (common subset) ──────────────────────────────────────────

_HTML_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": "\u00a0",
    "bull": "\u2022",
    "hellip": "\u2026",
    "mdash": "\u2014",
    "ndash": "\u2013",
    "laquo": "\u00ab",
    "raquo": "\u00bb",
    "lsquo": "\u2018",
    "rsquo": "\u2019",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
}


# ═════════════════════════════════════════════════════════════════════════════
#  Parser
# ═════════════════════════════════════════════════════════════════════════════


class SkimmdParser(HTMLParser):
    """Whitelist-based HTML-to-Markdown converter.

    Parameters
    ----------
    base_url : str or None
        Base URL for resolving relative ``href``/``src`` paths.
    strip_external : bool
        When True (default), block containers that contain *only* external
        links are discarded.  Mixed blocks keep internal content and drop
        external link anchor text.
    """

    def __init__(self, base_url: str | None = None, *, strip_external: bool = True):
        super().__init__(convert_charrefs=False)
        # Output
        self._output: list[str] = []

        # Buffer for the current block container
        self._buf: list[str] = []
        self._buf_has_internal = False
        self._buf_has_external = False

        # State
        self._skip_depth = 0          # >0 → inside a NOISY_TAGS subtree
        self._in_pre = False
        self._list_stack: list[str] = []  # ["ul", "ol", …]
        self._list_counters: list[int] = []

        # Anchor state
        self._in_a = False
        self._a_href = ""
        self._a_is_external = False

        # Configuration
        self._base_url = base_url.rstrip("/") if base_url else None
        self._strip_external = strip_external
        self._origin: str | None = None
        if strip_external and base_url:
            self._origin = urlparse(base_url).hostname

    # ── helpers ──────────────────────────────────────────────────────────────

    def _is_external(self, href: str) -> bool:
        if not href or not self._origin:
            return False
        parsed = urlparse(href)
        if not parsed.hostname:
            return False
        return parsed.hostname != self._origin

    def _resolve(self, url: str) -> str:
        if self._base_url and url and url.startswith("/"):
            return self._base_url + url
        return url

    def _emit(self, text: str) -> None:
        """Append text to the current block buffer."""
        if text:
            self._buf.append(text)

    # ── block-flush logic ────────────────────────────────────────────────────

    def _flush_buf(self) -> None:
        """Decide whether the current block buffer is emitted or discarded.

        Decision matrix (only meaningful when ``strip_external=True``):

        * Only internal / no links → emit as-is.
        * Only external            → discard entirely.
        * Mixed (internal + external) → emit, but external anchor text was
          never written to the buffer (handled during ``handle_data`` /
          ``handle_endtag`` for ``<a>``), so only internal content survives.
        """
        if not self._buf:
            return

        if self._strip_external and self._buf_has_external and not self._buf_has_internal:
            # Block is entirely external — discard it.
            self._reset_buf()
            return

        text = "".join(self._buf)
        self._output.append(text)
        self._reset_buf()

    def _reset_buf(self) -> None:
        self._buf = []
        self._buf_has_internal = False
        self._buf_has_external = False

    # ── core handlers ────────────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        # Noisy subtree — push depth and bail (unless it's a void element
        # that will never produce an ``handle_endtag`` call).
        if tag in NOISY_TAGS:
            if tag not in _VOID_ELEMENTS:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return

        attr_dict = {k: v for k, v in attrs if v is not None}
        for key in ("href", "src", "poster"):
            if key in attr_dict:
                attr_dict[key] = self._resolve(attr_dict[key])

        # Block-container boundary → flush previous.
        if tag in BLOCK_CONTAINERS:
            self._flush_buf()

        # Handle block-level formatting that goes before content.
        self._handle_block_prefix(tag, attr_dict)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in NOISY_TAGS:
            if tag not in _VOID_ELEMENTS:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        # Handle block-level formatting that goes after content.
        self._handle_block_suffix(tag)

        # Block-container boundary → flush this block.
        if tag in BLOCK_CONTAINERS:
            self._flush_buf()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            # External anchor — skip anchor text entirely.
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self._emit(text)
            if not self._a_is_external:
                self._buf_has_internal = True

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            return
        self._emit(_HTML_ENTITIES.get(name, f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._skip_depth:
            return
        if self._strip_external and self._a_is_external:
            return
        try:
            self._emit(chr(int(name)))
        except (ValueError, OverflowError):
            self._emit(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        pass  # Discard comments.

    # ── tag-specific formatting ──────────────────────────────────────────────

    def _handle_block_prefix(self, tag: str, attrs: dict[str, str]) -> None:
        if tag in WHITELIST:
            self._emit_whitelist_prefix(tag, attrs)

    def _handle_block_suffix(self, tag: str) -> None:
        if tag in WHITELIST:
            self._emit_whitelist_suffix(tag)

    def _emit_whitelist_prefix(self, tag: str, attrs: dict[str, str]) -> None:
        # ── links ────────────────────────────────────────────────
        if tag == "a":
            href = attrs.get("href", "")
            is_ext = self._is_external(href)
            self._a_href = href
            self._a_is_external = is_ext
            if self._strip_external and is_ext:
                self._buf_has_external = True
                # Don't emit '[', don't emit anchor text.
                return
            self._emit("[")
            if href:
                self._buf_has_internal = True
            return

        # ── images / media ────────────────────────────────────────
        if tag == "img":
            src = attrs.get("src", "")
            alt = attrs.get("alt", "")
            if src.startswith("data:"):
                self._emit(f"![{alt}]()")
            else:
                self._emit(f"![{alt}]({src})")
            self._buf_has_internal = True
            return

        if tag == "video":
            src = attrs.get("src", "")
            poster = attrs.get("poster", "")
            parts: list[str] = []
            if poster:
                poster_md = f"![Poster]()" if poster.startswith("data:") else f"![Poster]({poster})"
                parts.append(poster_md)
            if src:
                if src.startswith("data:"):
                    parts.append("[Video]")
                else:
                    parts.append(f"[Video]({src})")
            if parts:
                self._emit(" ".join(parts))
                self._buf_has_internal = True
            return

        if tag == "source":
            src = attrs.get("src", "")
            if src:
                self._emit(f" [Stream]({src})")
                self._buf_has_internal = True
            return

        if tag == "picture":
            return  # children (img, source) are handled individually.

        # ── headings ──────────────────────────────────────────────
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._emit(f"\n{'#' * level} ")
            return

        # ── lists ─────────────────────────────────────────────────
        if tag in ("ul", "ol"):
            if not self._list_stack:
                self._emit("\n")
            self._list_stack.append(tag)
            self._list_counters.append(0)
            return

        if tag == "li":
            indent = "  " * len(self._list_stack)
            prefix = "-"
            if self._list_stack and self._list_stack[-1] == "ol":
                self._list_counters[-1] += 1
                prefix = f"{self._list_counters[-1]}."
            self._emit(f"\n{indent}{prefix} ")
            return

        # ── blockquote ────────────────────────────────────────────
        if tag == "blockquote":
            self._emit("\n> ")
            return

        # ── code ──────────────────────────────────────────────────
        if tag == "pre":
            self._in_pre = True
            self._emit("\n```\n")
            return

        if tag == "code":
            if not self._in_pre:
                self._emit("`")
            return

        # ── inline formatting ─────────────────────────────────────
        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return

        # ── thematic break ────────────────────────────────────────
        if tag == "hr":
            self._emit("\n---\n")
            return

        if tag == "br":
            self._emit("\n")
            return

    def _emit_whitelist_suffix(self, tag: str) -> None:
        # ── links ────────────────────────────────────────────────
        if tag == "a":
            if self._strip_external and self._a_is_external:
                self._a_href = ""
                self._a_is_external = False
                return
            if self._a_href:
                self._emit(f"]({self._a_href})")
            self._a_href = ""
            self._a_is_external = False
            return

        # ── headings ──────────────────────────────────────────────
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n")
            return

        # ── lists ─────────────────────────────────────────────────
        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
                self._list_counters.pop()
            self._emit("\n")
            return

        # ── blockquote ────────────────────────────────────────────
        if tag == "blockquote":
            self._emit("\n")
            return

        # ── code ──────────────────────────────────────────────────
        if tag == "pre":
            self._in_pre = False
            self._emit("\n```\n")
            return
        if tag == "code" and not self._in_pre:
            self._emit("`")
            return

        # ── inline formatting ─────────────────────────────────────
        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return

    # ── result ─────────────────────────────────────────────────────────────────

    def get_result(self) -> str:
        """Finalise and return the processed Markdown string."""
        self._flush_buf()
        text = "".join(self._output)

        # Clean up whitespace artifacts.
        text = re.sub(r" +\n", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n", text)
        text = re.sub(r"  +", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return text.strip()


# ═════════════════════════════════════════════════════════════════════════════
#  Public API
# ═════════════════════════════════════════════════════════════════════════════


def skimmd(html: str, base_url: str | None = None, *, strip_external: bool = True) -> str:
    """Convert HTML to Skimmed Markdown.

    Parameters
    ----------
    html : str
        Raw HTML to process.
    base_url : str or None
        Base URL for resolving relative paths (e.g. ``/images/foo.png``).
    strip_external : bool
        When True (default), block containers that contain *only* external
        links are discarded.  Mixed blocks keep internal content and drop
        external link anchor text.

    Returns
    -------
    str
        Compact Markdown with links, images, and videos preserved.
    """
    parser = SkimmdParser(base_url=base_url, strip_external=strip_external)
    parser.feed(html)
    return parser.get_result()


# ═════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(
            "Usage: python skimmd.py [file.html] [--base URL] [--keep-external]\n"
            "\n"
            "If no file is given, reads from stdin."
        )
        sys.exit(0)

    base_url: str | None = None
    strip_external = True

    args = sys.argv[1:]
    if "--base" in args:
        idx = args.index("--base")
        if idx + 1 < len(args):
            base_url = args[idx + 1]

    if "--keep-external" in args:
        strip_external = False

    html_input: str | None = None
    for arg in args:
        if not arg.startswith("--") and html_input is None:
            with open(arg) as f:
                html_input = f.read()

    if html_input is None:
        html_input = sys.stdin.read()

    if html_input:
        print(skimmd(html_input, base_url=base_url, strip_external=strip_external))
