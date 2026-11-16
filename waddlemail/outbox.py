"""Sending mail, and the Outbox (no Qt imports; unit-testable).

A message to send is a payload: a JSON-able dict with what the compose window made (the Graph
message, with its pictures as cid: links; the recipients and subject as typed; its attachments
and pictures; for a reply or forward, the message it answers; the draft it was saved as).
deliver() sends one: straight out when it's a plain new message, otherwise through a draft
(created or updated, attachments and pictures reconciled, then sent).

When the server can't be reached (offline, timing out, busy), the message waits in the Outbox
(the store's outbox table, with copies of its files) and goes out on a later try. Each try
records how far it got (the draft's id, the attachments' ids) in the payload, so the next one
carries on instead of making a second draft or uploading files twice.
"""
from __future__ import annotations

import base64
import os
import shutil
from dataclasses import asdict

import requests

from .attachments import Attachment, reconcile, reconcile_inline
from .auth import AuthError
from .compose import Outgoing
from .graph import GraphClient, GraphError
from .inline import InlineImage, cid_refs

TRANSIENT = {None, 408, 429, 500, 502, 503, 504}  # worth trying again later


def transient(exc: BaseException) -> bool:
    """Whether a failed send is worth trying again later (rather than the server refusing
    the message: an address it won't take, no permission)."""
    if isinstance(exc, (requests.RequestException, AuthError)):  # offline, signed out
        return True
    return isinstance(exc, GraphError) and exc.status in TRANSIENT


def make_payload(mailbox_id: str, out: Outgoing, kind: str = "new",
                 original_id: str | None = None, draft_id: str | None = None,
                 attachments: list[Attachment] | None = None, originals_known: bool = True,
                 has_had_attachments: bool = False, had_pictures: bool = False) -> dict:
    return {
        "mailbox_id": mailbox_id, "kind": kind, "original_id": original_id,
        "draft_id": draft_id, "message": out.graph_message(),
        "subject": out.subject, "html": out.html, "body": out.body,
        "to": list(out.to), "cc": list(out.cc), "bcc": list(out.bcc),
        "attachments": [asdict(a) for a in attachments or []],
        "inline": [{"name": i.name, "content_id": i.content_id,
                    "content_type": i.content_type,
                    "data": base64.b64encode(i.data).decode("ascii")} for i in out.inline],
        "originals_known": originals_known,
        "has_had_attachments": has_had_attachments or bool(attachments),
        "had_pictures": had_pictures, "tried": False, "files": None,
    }


def attachments_of(p: dict) -> list[Attachment]:
    return [Attachment(**a) for a in p["attachments"]]


def pictures_of(p: dict) -> list[InlineImage]:
    return [InlineImage(i["name"], i["content_id"], i["content_type"],
                        base64.b64decode(i["data"])) for i in p["inline"]]


def write_draft(graph: GraphClient, p: dict) -> str:
    """Create or update the draft `p` describes, with its attachments and pictures; returns
    its id. The payload keeps the ids (draft, attachments), so writing it again only sends
    what changed."""
    message = p["message"]
    if p["draft_id"] is None and p["original_id"] is None:
        p["draft_id"] = graph.create_draft(message)["id"]
    else:
        if p["draft_id"] is None:  # Exchange's reply/forward draft: threading, and the
            p["draft_id"] = graph.create_response(p["original_id"], p["kind"])["id"]  # copies
        graph.update_message(p["draft_id"], message)
    item = graph.message_path(p["draft_id"])
    if p["has_had_attachments"]:  # upload the new, remove the ones taken out
        atts = attachments_of(p)
        reconcile(graph, item, atts, remove_others=p["originals_known"])
        p["attachments"] = [asdict(a) for a in atts]
    pictures = pictures_of(p)
    if pictures or p["had_pictures"]:  # the body's cid: links point to these
        ours = {i.content_id.lower() for i in pictures}
        reconcile_inline(graph, item, pictures,
                         keep_others=bool(cid_refs(message["body"]["content"]) - ours))
        p["had_pictures"] = True
    return p["draft_id"]


def deliver(graph: GraphClient, p: dict) -> None:
    """Send the message `p`. Raises on failure; transient() says whether to try again."""
    if p["tried"]:
        # An earlier try may have uploaded files without learning their ids: find them on
        # the draft by name and size rather than uploading them again.
        for a in p["attachments"]:
            if a["path"] and not a["remote_id"]:
                a["original"] = True
    p["tried"] = True
    if p["original_id"] is None and p["draft_id"] is None and not p["attachments"] \
            and not p["inline"]:
        graph.send_mail(p["message"])  # a plain new message: straight out
        return
    graph.send_draft(write_draft(graph, p))


def keep_files(p: dict, folder: str) -> None:
    """Copy the attachments to `folder` (the originals may be gone by the time it's sent)
    and point the payload at the copies."""
    os.makedirs(folder, exist_ok=True)
    for n, a in enumerate(p["attachments"]):
        if a["path"]:
            copy = os.path.join(folder, f"{n}-{os.path.basename(a['path'])}")
            shutil.copyfile(a["path"], copy)
            a["path"] = copy
    p["files"] = folder


def drop_files(p: dict) -> None:
    if p.get("files"):
        shutil.rmtree(p["files"], ignore_errors=True)
