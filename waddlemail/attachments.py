"""Attachments being written: files you add, and ones already on the item (a forward's
copies of the original's, a saved draft's, an event's). reconcile() makes the server item
match the list: it removes what you took out and uploads what you added (no Qt imports;
unit-testable)."""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field

from .graph import GraphClient


@dataclass
class Attachment:
    name: str
    size: int
    content_type: str = "application/octet-stream"
    path: str | None = None        # a file to upload (None: already on the server)
    remote_id: str | None = None   # its id on the item, once known
    original: bool = False         # a forward's copy of the original's: matched by name/size

    @classmethod
    def from_file(cls, path: str) -> "Attachment":
        guessed, _ = mimetypes.guess_type(path)
        return cls(os.path.basename(path), os.path.getsize(path),
                   guessed or "application/octet-stream", path=path)

    @classmethod
    def from_server(cls, item: dict, original: bool = False) -> "Attachment":
        """From an attachments() entry: of the item itself, or (original=True) of the
        message being forwarded, whose copies are found on the new draft by name and size."""
        return cls(item.get("name") or "attachment", int(item.get("size") or 0),
                   item.get("contentType") or "application/octet-stream",
                   remote_id=None if original else item.get("id"), original=original)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def file_name(item: dict) -> str:
    """A safe name to save a received attachment as: no folders in it, and an attached
    message (an itemAttachment, which downloads as MIME) gets .eml."""
    name = (item.get("name") or "").replace("/", "_").replace("\\", "_").strip(" .")
    name = name or "attachment"
    if "itemAttachment" in (item.get("@odata.type") or "") and not name.lower().endswith(".eml"):
        name += ".eml"
    return name


def unique_path(folder: str, name: str) -> str:
    """`folder`/`name`, or "name (2).ext" and so on if that's taken."""
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{stem} ({n}){ext}")
    return path


@dataclass
class ReconcileResult:
    uploaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def reconcile(graph: GraphClient, item: str, wanted: list[Attachment],
              remove_others: bool = True) -> ReconcileResult:
    """Make the attachments of `item` (a path from graph.message_path/event_path) the
    `wanted` ones. Inline attachments (pictures in the body) are left alone, and so is
    everything not in `wanted` when `remove_others` is False (when the list of what was there
    couldn't be loaded, say). Updates each Attachment's remote_id, so doing it again later
    only sends the differences."""
    result = ReconcileResult()
    remote = [a for a in graph.attachments(item) if not a.get("isInline")]
    known = {a.remote_id for a in wanted if a.remote_id}
    for att in wanted:  # a forward's copies: one each, by name and size
        if att.remote_id is None and att.original:
            match = next((r for r in remote if r["id"] not in known
                          and r.get("name") == att.name and int(r.get("size") or 0) == att.size),
                         None)
            if match is not None:
                att.remote_id = match["id"]
                known.add(match["id"])
    for r in remote if remove_others else []:
        if r["id"] not in known:  # taken out of the list
            graph.delete_attachment(item, r["id"])
            result.removed.append(r.get("name") or r["id"])
    for att in wanted:
        if att.remote_id is None and att.path:
            with open(att.path, "rb") as fh:
                data = fh.read()
            att.remote_id = graph.add_attachment(item, att.name, data, att.content_type)
            if att.remote_id is None:  # (the server didn't name it): find it by name and size
                fresh = [r for r in graph.attachments(item) if r["id"] not in known
                         and r.get("name") == att.name and int(r.get("size") or 0) == att.size]
                att.remote_id = fresh[0]["id"] if fresh else None
            if att.remote_id:
                known.add(att.remote_id)
            result.uploaded.append(att.name)
    return result
