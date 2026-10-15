"""HTML-to-text for job descriptions, stdlib only.

Greenhouse returns `content` as HTML that is itself HTML-escaped (`&lt;p&gt;...`), so we unescape
once, then parse tags, keeping paragraph and list breaks and dropping scripts/styles.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "tr", "blockquote", "pre", "hr",
}  # fmt: skip
_SKIP_TAGS = {"script", "style", "head", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


_INLINE_WS = re.compile(r"[ \t\r\f\v ]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")


def html_to_text(value: str | None, *, unescape: bool = True) -> str:
    """Readable plain text: tags stripped, entities decoded, paragraphs and bullets kept, whitespace collapsed."""
    if not value:
        return ""
    if unescape:
        value = html.unescape(value)
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    lines = [_INLINE_WS.sub(" ", line).strip() for line in text.split("\n")]
    # Drop bullets that ended up empty (e.g. <li><p>..</p></li> puts the text on the next line).
    merged: list[str] = []
    for line in lines:
        if merged and merged[-1] == "-":
            if line and line != "-":
                merged[-1] = f"- {line}"
            continue
        merged.append(line)
    merged = [line for line in merged if line != "-"]
    # Keep list items tight: drop blank lines between two consecutive bullets.
    kept: list[str] = []
    for i, line in enumerate(merged):
        if not line and kept and kept[-1].startswith("- "):
            nxt = next((n for n in merged[i + 1 :] if n), "")
            if nxt.startswith("- "):
                continue
        kept.append(line)
    return _MANY_NEWLINES.sub("\n\n", "\n".join(kept)).strip()
