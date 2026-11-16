"""Sending through deliver(), and the Outbox: messages that wait for the server."""
from __future__ import annotations

import json

import requests

from waddlemail.attachments import Attachment
from waddlemail.compose import Outgoing
from waddlemail.graph import SEND_MARK, GraphClient, GraphError
from waddlemail.inline import data_uri, extract_inline
from waddlemail.outbox import (
    SETTLE, deliver, drop_files, keep_files, make_payload, possibly_sent, ready, transient,
)
from tests.test_mailboxes import FakeResponse
from tests.test_messages import make_store


class Server:
    """A mailbox that can be offline, or refuse sending."""

    def __init__(self):
        self.mail, self.drafts, self.sent, self.uploads = [], {}, [], []
        self.down, self.refuse = False, None
        self.n = 0

    def _check(self):
        if self.down:
            raise requests.ConnectionError("no route to host")

    def send_mail(self, message):
        self._check()
        self.mail.append(message)

    def create_draft(self, message):
        self._check()
        self.n += 1
        self.drafts[f"d{self.n}"] = {"message": message, "attachments": []}
        return {"id": f"d{self.n}"}

    def create_response(self, mid, kind):
        return self.create_draft({"subject": f"{kind} {mid}"})

    def update_message(self, did, message):
        self._check()
        self.drafts[did]["message"] = message

    def message_path(self, did):
        return did

    def attachments(self, did):
        self._check()
        return [dict(a) for a in self.drafts[did]["attachments"]]

    def add_attachment(self, did, name, data, ctype="", content_id=None):
        self._check()
        self.uploads.append(name)
        att = {"id": f"a{len(self.uploads)}", "name": name, "size": len(data),
               "isInline": content_id is not None}
        self.drafts[did]["attachments"].append(att)
        if name == "big.pdf" and not getattr(self, "_failed_once", False):
            self._failed_once = True  # uploaded, then the line drops before the answer
            self.down = True
            raise requests.ConnectionError("connection reset")
        return att["id"]

    def delete_attachment(self, did, aid):
        self.drafts[did]["attachments"] = [a for a in self.drafts[did]["attachments"]
                                           if a["id"] != aid]

    def send_draft(self, did):
        self._check()
        if self.refuse:
            raise GraphError(self.refuse, 400)
        self.sent.append(did)

    def sent_with_mark(self, mark):
        self._check()
        sent = self.mail + [self.drafts[d]["message"] for d in self.sent]
        return any(p["value"] == mark for m in sent
                   for p in m.get("singleValueExtendedProperties", []))


def outgoing(**kw):
    return Outgoing(subject="Hi", body="<p>Hello</p>", to=["Ana <ana@x.com>"], **kw)


def test_plain_mail_goes_straight_out():
    s = Server()
    deliver(s, make_payload("a", outgoing()))
    assert s.mail and not s.drafts


def test_a_retry_carries_on_with_the_same_draft(tmp_path):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF" + b"x" * 100)
    body, pictures = extract_inline(f'<p><img src="{data_uri("image/png", b"PNG")}"></p>')
    p = make_payload("a", outgoing(inline=pictures), attachments=[Attachment.from_file(str(f))])
    p["body"] = p["message"]["body"]["content"] = body
    s = Server()
    try:
        deliver(s, p)
    except Exception as exc:
        assert transient(exc)
    assert p["draft_id"] == "d1" and not s.sent
    keep_files(p, str(tmp_path / "outbox" / "1"))  # it waits in the Outbox
    assert p["attachments"][0]["path"].startswith(str(tmp_path / "outbox"))
    p = json.loads(json.dumps(p))  # (stored as JSON)
    s.down = False
    deliver(s, p)
    assert s.sent == ["d1"] and len(s.drafts) == 1          # the same draft
    assert sorted(a["name"] for a in s.drafts["d1"]["attachments"]) == sorted(
        ["big.pdf", pictures[0].name])                     # nothing uploaded twice
    drop_files(p)
    assert not (tmp_path / "outbox" / "1").exists()


def test_what_is_worth_trying_again():
    assert transient(requests.ConnectionError())
    assert transient(GraphError("busy", 503)) and transient(GraphError("throttled", 429))
    assert transient(GraphError("failed after retries"))
    assert not transient(GraphError("bad recipient", 400))
    assert not transient(GraphError("not allowed", 403))
    assert not transient(FileNotFoundError("gone"))


def test_outbox_rows(tmp_path):
    st = make_store(tmp_path)
    p = make_payload("a", outgoing(cc=["bo@y.org"]))
    n = st.outbox_add("a", p, "offline")
    assert st.pending_count() == 1 and st.outbox_counts() == {"a": 1}
    [row] = st.outbox_rows("a", "OUT", "Me")
    assert row["id"] == f"outbox:{n}" and row["folder_id"] == "OUT"
    assert row["preview"].startswith("Waiting to be sent (offline)")
    assert json.loads(row["to_recipients"]) == [["Ana", "ana@x.com"]]
    assert json.loads(row["cc_recipients"]) == [["bo@y.org", "bo@y.org"]]
    st.outbox_update(n, p, "bad recipient", refused=True)
    assert st.outbox_items(refused=False) == []
    assert st.outbox_rows("a", "OUT")[0]["preview"].startswith("Not sent: bad recipient")
    st.outbox_remove(n)
    assert st.pending_count() == 0


def lost_answer():
    """What a dropped connection looks like after the request was written."""
    from urllib3.exceptions import ProtocolError
    return requests.ConnectionError(ProtocolError("Connection aborted.",
                                                  ConnectionResetError(104, "reset")))


def test_a_send_whose_answer_was_lost_is_not_sent_again():
    s = Server()
    p = make_payload("a", outgoing())
    assert p["message"]["singleValueExtendedProperties"] == [{"id": SEND_MARK,
                                                               "value": p["mark"]}]
    real = s.send_mail

    def got_through_but_no_answer(message):
        real(message)          # the server has it...
        raise lost_answer()    # ...but the answer never came

    s.send_mail = got_through_but_no_answer
    try:
        deliver(s, p)
    except Exception as exc:
        assert transient(exc) and possibly_sent(exc)
    assert len(s.mail) == 1 and p["maybe_sent"] and not ready(p)  # waits to settle
    assert ready(p, p["maybe_sent"] + SETTLE)
    s.send_mail = real
    deliver(s, json.loads(json.dumps(p)))  # the next try finds it in Sent Items
    assert len(s.mail) == 1


def test_offline_is_not_possibly_sent():
    from urllib3.exceptions import MaxRetryError, NewConnectionError
    offline = requests.ConnectionError(MaxRetryError(None, "/sendMail",
                                                     NewConnectionError(None, "no network")))
    assert transient(offline) and not possibly_sent(offline)
    assert not possibly_sent(requests.ConnectTimeout())
    assert possibly_sent(requests.ReadTimeout()) and possibly_sent(lost_answer())
    assert possibly_sent(GraphError("busy", 503)) and not possibly_sent(GraphError("bad", 400))
    s = Server()
    s.down = True  # offline: fails, nothing reached the server, no waiting
    p = make_payload("a", outgoing())
    try:
        deliver(s, p)
    except Exception:
        pass
    assert ready(p) and not s.mail


def test_sending_is_not_repeated_after_a_server_error():
    calls = []

    class S:
        def request(self, method, url, **kw):
            calls.append(url)
            return FakeResponse(503 if len(calls) == 1 else 202, {})

    g = GraphClient(lambda: "t", session=S(), sleep=lambda s: None)
    try:
        g.send_mail({"subject": "x"})
    except GraphError as exc:
        assert exc.status == 503
    assert len(calls) == 1  # (it may have been sent: the Outbox looks before trying again)
