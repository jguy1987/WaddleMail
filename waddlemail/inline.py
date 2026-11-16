"""Pictures in message bodies (no Qt imports; unit-testable).

Mail shows them with `cid:` links to inline attachments. Reading, resolve_cids() swaps those
links for `data:` URIs of the stored pictures, which the reader (and the compose editor, when
quoting) can show offline. Writing, the editor holds pictures as `data:` URIs too, and
extract_inline() turns them back into `cid:` links plus the inline attachments to upload.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from urllib.parse import unquote

# src="cid:..." (quoted or not)
_CID_SRC = re.compile(r"""(\bsrc\s*=\s*["']?)cid:([^"'\s>]+)""", re.IGNORECASE)
# src="data:image/png;base64,...": what the editor writes
_DATA_SRC = re.compile(r"""(\bsrc\s*=\s*)(["'])data:([\w.+/-]+);base64,([A-Za-z0-9+/=\s]+)\2""",
                       re.IGNORECASE)

MAX_INLINE_IMAGE = 10 * 1024 * 1024  # bigger "inline" pictures aren't fetched for the reader


def cid_key(value: str) -> str:
    """A content id as a body links to it: no <>, unescaped, lower case."""
    return unquote(value).strip().strip("<>").lower()


def cid_refs(html: str) -> set[str]:
    """The content ids a body's pictures link to."""
    return {cid_key(m.group(2)) for m in _CID_SRC.finditer(html)}


def data_uri(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def image_type(content_type: str | None, name: str | None) -> str:
    """The picture's type; from its name when the server only says octet-stream."""
    if content_type and content_type.startswith("image/"):
        return content_type
    guessed, _ = mimetypes.guess_type(name or "")
    return guessed or content_type or "application/octet-stream"


def resolve_cids(html: str, images: dict[str, tuple[str, bytes]]) -> str:
    """Swap cid: links for data: URIs of `images` ({content id: (type, bytes)}); links to
    pictures we don't have stay as they are."""
    def swap(m: re.Match) -> str:
        found = images.get(cid_key(m.group(2)))
        return m.group(1) + data_uri(*found) if found else m.group(0)
    return _CID_SRC.sub(swap, html)


@dataclass
class InlineImage:
    name: str           # image-<hash>.png: also how a saved draft's copy is recognised
    content_id: str
    content_type: str
    data: bytes


def extract_inline(html: str) -> tuple[str, list[InlineImage]]:
    """The body with each data: picture replaced by a cid: link, and those pictures (once
    each; named after their content, so saving a draft again finds them already there)."""
    images: dict[str, InlineImage] = {}

    def swap(m: re.Match) -> str:
        try:
            data = base64.b64decode(re.sub(r"\s+", "", m.group(4)), validate=True)
        except (binascii.Error, ValueError):
            return m.group(0)
        ctype = m.group(3).lower()
        digest = hashlib.sha1(data).hexdigest()[:16]
        ext = (mimetypes.guess_extension(ctype) or ".bin").lstrip(".")
        ext = {"jpe": "jpg", "jpeg": "jpg"}.get(ext, ext)
        name = f"image-{digest}.{ext}"
        img = images.setdefault(name, InlineImage(name, f"{name}@waddlemail", ctype, data))
        return f"{m.group(1)}{m.group(2)}cid:{img.content_id}{m.group(2)}"

    return _DATA_SRC.sub(swap, html), list(images.values())
