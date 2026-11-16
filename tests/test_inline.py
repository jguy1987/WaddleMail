"""Pictures in bodies: cid: links shown from stored inline attachments, and the editor's
data: pictures sent as inline attachments."""
from __future__ import annotations

import base64

from waddlemail.attachments import reconcile_inline
from waddlemail.bodies import BodyService
from waddlemail.graph import GraphClient
from waddlemail.inline import cid_refs, data_uri, extract_inline, resolve_cids
from waddlemail.store import Store
from tests.test_mailboxes import FakeResponse

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def test_resolve_and_extract():
    html = ('<p>Hi<img src="cid:image001.png@01DB1234.ABCD"> and '
            "<img src=cid:Logo%20One><img src='cid:gone'></p>")
    assert cid_refs(html) == {"image001.png@01db1234.abcd", "logo one", "gone"}
    out = resolve_cids(html, {"image001.png@01db1234.abcd": ("image/png", PNG),
                              "logo one": ("image/gif", b"GIF89a")})
    assert data_uri("image/png", PNG) in out and "data:image/gif;base64," in out
    assert "src='cid:gone'" in out  # one we don't have stays a link

    uri = data_uri("image/png", PNG)
    body, images = extract_inline(f'<p><img src="{uri}" width="40" /><img src="{uri}" />'
                                  '<img src="https://x.org/a.png" /></p>')
    assert len(images) == 1  # the same picture twice: one attachment
    img = images[0]
    assert img.data == PNG and img.content_type == "image/png"
    assert img.name.startswith("image-") and img.name.endswith(".png")
    assert body.count(f'src="cid:{img.content_id}"') == 2 and "https://x.org/a.png" in body
    assert extract_inline(f'<img src="{uri}">')[1][0].name == img.name  # stable names


class PictureGraph:
    def __init__(self):
        self.calls = 0

    def get_message_body(self, mid):
        return "html", '<p><img src="cid:a@x"><img src="cid:b@x"></p>'

    def inline_images(self, mid, wanted):
        self.calls += 1
        return {"a@x": ("image/png", PNG)} if "a@x" in wanted else {}


def test_body_pictures_fetched_once_and_cached(tmp_path):
    st, g = Store(tmp_path / "m.db"), PictureGraph()
    bodies = BodyService(st, g)
    first = bodies.get("M")
    assert data_uri("image/png", PNG) in first.content and "cid:b@x" in first.content
    again = bodies.get("M")  # b@x never resolves: not asked again this session
    assert again.from_cache and again.content == first.content and g.calls == 1
    assert BodyService(st, g).get("M").content == first.content and g.calls == 2  # (only b)
    size = st.conn.execute("SELECT size FROM bodies WHERE message_id='M'").fetchone()[0]
    assert size > len(PNG)  # the pictures count toward the cache
    st.conn.execute("DELETE FROM bodies")
    assert st.inline_images("M") == {}  # and leave with the body


def test_graph_fetches_inline_pictures():
    calls = []

    class S:
        def request(self, method, url, params=None, **kw):
            calls.append(url)
            if url.endswith("/attachments"):
                return FakeResponse(200, {"value": [
                    {"id": "big", "name": "huge.png", "size": 50 * 1024 * 1024,
                     "isInline": True},
                    {"id": "i1", "name": "image001.png", "size": 10, "isInline": True},
                    {"id": "f1", "name": "report.pdf", "size": 10, "isInline": False,
                     "contentType": "application/pdf"}]})
            return FakeResponse(200, {"contentId": "<image001.png@01DB>",
                                      "name": "image001.png",
                                      "contentType": "application/octet-stream",
                                      "contentBytes": base64.b64encode(PNG).decode()})

    g = GraphClient(lambda: "t", session=S())
    found = g.inline_images("M", {"image001.png@01db"})
    assert found == {"image001.png@01db": ("image/png", PNG)}
    assert [u.rsplit("/", 1)[1] for u in calls] == ["attachments", "i1"]  # not huge, not pdf


class FakeItem:
    def __init__(self, existing):
        self.items, self.added, self.deleted = existing, [], []

    def attachments(self, item):
        return list(self.items)

    def add_attachment(self, item, name, data, content_type="", content_id=None):
        self.added.append((name, content_id))
        self.items.append({"id": name, "name": name, "isInline": True})

    def delete_attachment(self, item, aid):
        self.deleted.append(aid)
        self.items = [a for a in self.items if a["id"] != aid]


def test_reconcile_inline():
    _, images = extract_inline(f'<img src="{data_uri("image/png", PNG)}">')
    g = FakeItem([{"id": "x1", "name": "image001.png", "isInline": True},  # Exchange's copy
                  {"id": "f1", "name": "report.pdf"}])                     # an attachment
    reconcile_inline(g, "item", images)
    assert g.deleted == ["x1"] and g.added == [(images[0].name, images[0].content_id)]
    reconcile_inline(g, "item", images)  # saved again: nothing to do
    assert len(g.added) == 1 and g.deleted == ["x1"]
    g = FakeItem([{"id": "x1", "name": "image001.png", "isInline": True}])
    reconcile_inline(g, "item", [], keep_others=True)  # the body still links to it
    assert g.deleted == []
