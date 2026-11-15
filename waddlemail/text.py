"""Plain text from a message body, for the full-text search index (no Qt imports)."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "head", "title"}
_BREAK = {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "pre", "table"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def body_text(content_type: str, content: str) -> str:
    """The readable text of a body: tags, styles and scripts removed, whitespace collapsed."""
    if content_type != "html":
        text = content
    else:
        parser = _Text()
        try:
            parser.feed(content)
            parser.close()
            text = "".join(parser.parts)
        except Exception:  # malformed markup: fall back to crude tag stripping
            text = html.unescape(re.sub(r"<[^>]*>", " ", content))
    return re.sub(r"[ \t\r\f\v ]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
