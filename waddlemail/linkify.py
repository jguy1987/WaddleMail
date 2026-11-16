"""Turn bare http(s) addresses in text into links (no Qt imports; unit-testable).

Works on HTML that is already well formed (nh3's output, or Qt's toHtml): only text between tags
is touched, never tags and attributes, text already inside a link, or <style>/<script>/<title>.
"""
from __future__ import annotations

import re

# a tag (quoted attribute values may contain ">") or a comment
_TAG = re.compile(r"(<!--.*?-->|<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>)", re.S)
_NAME = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")
# the text is HTML-escaped, so an address ends before an escaped quote or angle bracket
_URL = re.compile(r"https?://(?:(?!&(?:quot|lt|gt|#0*39|#x0*27);)[^\s<>\"'])+", re.I)
_SKIP = {"a", "script", "style", "title", "textarea"}
_PAIRS = {")": "(", "]": "["}


def _trim(url: str) -> str:
    """Drop sentence punctuation and unbalanced closing brackets from the end of a match."""
    while url:
        last = url[-1]
        if last in ".,;:!?*" or (last in _PAIRS and url.count(last) > url.count(_PAIRS[last])):
            url = url[:-1]
        elif url.lower().endswith("&amp;"):
            url = url[:-5]
        else:
            break
    return url


def _link(match: re.Match) -> str:
    url = _trim(match.group(0))
    if not re.sub(r"^https?://", "", url, flags=re.I):  # nothing after the scheme
        return match.group(0)
    return f'<a href="{url}" rel="noopener noreferrer">{url}</a>' + match.group(0)[len(url):]


def linkify_text(escaped: str) -> str:
    """`escaped`: HTML-escaped text with no tags. Returns it with its addresses as links."""
    return _URL.sub(_link, escaped)


def linkify_html(content: str) -> str:
    out: list[str] = []
    skip = 0
    for part in _TAG.split(content):
        if part.startswith("<"):
            m = _NAME.match(part)
            if m and m.group(2).lower() in _SKIP and not part.endswith("/>"):
                skip = max(skip - 1, 0) if m.group(1) else skip + 1
            out.append(part)
        else:
            out.append(part if skip else linkify_text(part))
    return "".join(out)
