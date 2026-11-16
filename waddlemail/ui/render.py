"""Turn a message body into safe HTML for the reader (no Qt imports; unit-testable)."""
from __future__ import annotations

import html

import nh3

from ..linkify import linkify_html, linkify_text

_ATTRS = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
_ATTRS.setdefault("*", set()).update({"style", "class", "dir", "align", "bgcolor", "width", "height"})
_ATTRS.setdefault("img", set()).update({"src", "alt", "width", "height"})

_BASE_CSS = (
    "body{font-family:sans-serif;font-size:14px;margin:12px;color:#222;background:#fff;"
    "word-wrap:break-word}img{max-width:100%;height:auto}pre{white-space:pre-wrap}"
    "blockquote{margin:0 0 0 8px;padding-left:8px;border-left:3px solid #ccc}"
)


def csp(allow_remote_images: bool) -> str:
    img = "img-src data: https: http:" if allow_remote_images else "img-src data:"
    # No scripts, frames, forms or remote fonts/styles, ever. Remote images only on request.
    return f"default-src 'none'; {img}; style-src 'unsafe-inline'"


def render_body(content_type: str, content: str, allow_remote_images: bool = False) -> str:
    if content_type == "text":
        inner = f"<pre>{linkify_text(html.escape(content))}</pre>"
    else:
        inner = linkify_html(nh3.clean(content, attributes=_ATTRS,
                                       url_schemes={"http", "https", "mailto", "cid", "data"}))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{csp(allow_remote_images)}\">"
        f"<style>{_BASE_CSS}</style></head><body>{inner}</body></html>"
    )


def placeholder(text: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head>"
        f"<body><p style='color:#777'>{html.escape(text)}</p></body></html>"
    )


def quotable_html(content_type: str, content: str) -> str:
    """A received body as a safe HTML fragment, to quote in a reply or forward."""
    if content_type == "text":
        return "<div>" + html.escape(content).replace("\n", "<br>") + "</div>"
    return nh3.clean(content, attributes=_ATTRS,
                     url_schemes={"http", "https", "mailto", "cid", "data"})
