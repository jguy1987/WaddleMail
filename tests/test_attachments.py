"""Attachments: uploading small and large files, reconciling an item's attachments, and
downloading received ones."""
from __future__ import annotations

import base64

import pytest

from waddlemail.attachments import Attachment, file_name, human_size, reconcile, unique_path
from waddlemail.graph import GraphClient, GraphError
from tests.test_mailboxes import FakeResponse


class Recorder:
    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, headers=None, json=None, data=None, **kw):
        self.calls.append((method, url, headers or {}, json, data))
        if url.endswith("/attachments") and method == "POST":
            return FakeResponse(201, {"id": "A1"})
        if url.endswith("createUploadSession"):
            return FakeResponse(201, {"uploadUrl": "https://outlook.office.com/up?authtoken=x"})
        if url.startswith("https://outlook.office.com/up"):
            start, rest = headers["Content-Range"][6:].split("-")
            end, total = rest.split("/")
            if int(end) + 1 < int(total):
                return FakeResponse(200, {"nextExpectedRanges": [str(int(end) + 1)]})
            resp = FakeResponse(201, "")
            resp.headers = {"Location": "https://outlook.office.com/api/v2.0/Users('u')/"
                                        "Messages('M')/Attachments('BIG1')"}
            return resp
        return FakeResponse(200, {"value": []})


def test_small_and_large_uploads():
    s = Recorder()
    g = GraphClient(lambda: "t", session=s)
    item = g.message_path("M")
    assert g.add_attachment(item, "a.txt", b"hello", "text/plain") == "A1"
    method, url, headers, body, _ = s.calls[0]
    assert url.endswith("/me/messages/M/attachments") and body["contentBytes"] == \
        base64.b64encode(b"hello").decode()
    assert body["@odata.type"] == "#microsoft.graph.fileAttachment"

    big = bytes(range(256)) * (4 * 1024 * 4 + 5)  # ~4 MB: an upload session, 2 pieces
    s.calls.clear()
    assert g.add_attachment(item, "big.bin", big) == "BIG1"
    session, *puts = s.calls
    assert session[3] == {"AttachmentItem": {"attachmentType": "file", "name": "big.bin",
                                             "size": len(big)}}
    assert len(puts) == 2 and b"".join(p[4] for p in puts) == big
    assert puts[0][2]["Content-Range"] == f"bytes 0-{GraphClient.UPLOAD_CHUNK - 1}/{len(big)}"
    assert all("Authorization" not in p[2] for p in puts)  # the URL carries its own auth
    with pytest.raises(GraphError):
        g.add_attachment(item, "huge", b"x" * (GraphClient.MAX_ATTACHMENT + 1))


class FakeItems:
    """attachments / add / delete on one item."""

    def __init__(self, remote):
        self.remote, self.added, self.deleted, self.n = list(remote), [], [], 0

    def attachments(self, item):
        return list(self.remote)

    def delete_attachment(self, item, att_id):
        self.deleted.append(att_id)
        self.remote = [r for r in self.remote if r["id"] != att_id]

    def add_attachment(self, item, name, data, content_type):
        self.n += 1
        self.added.append((name, data))
        self.remote.append({"id": f"new{self.n}", "name": name, "size": len(data)})
        return f"new{self.n}"


def test_reconcile_a_forward(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"12345")
    # the forward draft got copies of the original's two attachments (new ids)
    g = FakeItems([{"id": "c1", "name": "plan.pdf", "size": 10},
                   {"id": "c2", "name": "photo.jpg", "size": 20},
                   {"id": "img", "name": "logo.png", "size": 5, "isInline": True}])
    plan = Attachment.from_server({"id": "o1", "name": "plan.pdf", "size": 10}, original=True)
    wanted = [plan, Attachment.from_file(str(f))]  # photo.jpg was taken out
    r = reconcile(g, "item", wanted)
    assert plan.remote_id == "c1" and g.deleted == ["c2"]   # inline logo left alone
    assert g.added == [("notes.txt", b"12345")] and wanted[1].remote_id == "new1"
    assert r.uploaded == ["notes.txt"] and r.removed == ["photo.jpg"]
    again = reconcile(g, "item", wanted)                     # nothing changed: nothing sent
    assert not again.uploaded and not again.removed and g.n == 1
    wanted.pop()                                             # then notes.txt taken out
    reconcile(g, "item", wanted)
    assert g.deleted[-1] == "new1"


def test_reconcile_keeps_unknown_when_the_list_wasnt_loaded():
    g = FakeItems([{"id": "c1", "name": "plan.pdf", "size": 10}])
    reconcile(g, "item", [], remove_others=False)
    assert g.deleted == []


def test_attachment_from_file_and_sizes(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF" + b"0" * 2044)
    a = Attachment.from_file(str(f))
    assert (a.name, a.size, a.content_type) == ("report.pdf", 2048, "application/pdf")
    assert [human_size(n) for n in (500, 2048, 5 * 1024 * 1024)] == ["500 B", "2 KB", "5.0 MB"]


def test_download_names_and_content(tmp_path):
    class Raw:
        def request(self, method, url, **kw):
            self.url = url
            resp = FakeResponse(200, {})
            resp.content = b"%PDF-1.7"
            return resp

    s = Raw()
    g = GraphClient(lambda: "t", session=s)
    assert g.attachment_content(g.message_path("M"), "A/1") == b"%PDF-1.7"
    assert s.url.endswith("/me/messages/M/attachments/A%2F1/$value")
    assert file_name({"name": "../../etc/passwd"}) == "_.._etc_passwd"
    assert file_name({"name": ""}) == "attachment"
    assert file_name({"name": "Lunch?", "@odata.type": "#microsoft.graph.itemAttachment"}) \
        == "Lunch?.eml"
    (tmp_path / "a.pdf").write_bytes(b"")
    assert unique_path(str(tmp_path), "b.pdf") == str(tmp_path / "b.pdf")
    assert unique_path(str(tmp_path), "a.pdf") == str(tmp_path / "a (2).pdf")
