"""Composing: reply recipients and subjects, the quoted header, the Graph message, sending."""
from __future__ import annotations

import json

from waddlemail.compose import (
    FORWARD, NEW, REPLY, REPLY_ALL, Outgoing, format_sent, quote_header, quote_text,
    reply_recipients, reply_subject,
)
from waddlemail.graph import GraphClient
from tests.test_mailboxes import FakeResponse


def row(**kw):
    base = {"subject": "Budget", "sender_name": "Ana Ruiz", "sender_addr": "ana@x.com",
            "to_recipients": json.dumps([["Me", "me@x.com"], ["Bo", "bo@y.org"]]),
            "cc_recipients": json.dumps([["Cy", "cy@z.net"], ["Me again", "ME@x.com"]]),
            "received": "2026-11-16T15:00:00Z"}
    base.update(kw)
    return base


def test_subjects():
    assert reply_subject("Budget", REPLY) == "RE: Budget"
    assert reply_subject("RE: Budget", REPLY_ALL) == "RE: Budget"     # no "RE: RE:"
    assert reply_subject("RE: Budget", FORWARD) == "FW: RE: Budget"
    assert reply_subject("Fwd: Budget", FORWARD) == "Fwd: Budget"
    assert reply_subject(None, NEW) == ""


def test_reply_recipients():
    me = {"me@x.com"}
    assert reply_recipients(row(), REPLY, me) == ([("Ana Ruiz", "ana@x.com")], [])
    to, cc = reply_recipients(row(), REPLY_ALL, me)
    assert to == [("Ana Ruiz", "ana@x.com"), ("Bo", "bo@y.org")]   # not me
    assert cc == [("Cy", "cy@z.net")]                               # me left out, any case
    assert reply_recipients(row(), FORWARD, me) == ([], [])
    own = row(sender_name="Me", sender_addr="me@x.com")              # replying to my own mail
    assert reply_recipients(own, REPLY_ALL, me)[0] == [("Bo", "bo@y.org")]


def test_quote_header_and_text():
    sent = format_sent("2026-11-16T15:00:00Z")
    assert sent.split(",")[0] in ("Monday", "Sunday", "Tuesday") and "2026" in sent
    keys = [k for k, _ in quote_header(row(), sent)]
    assert keys == ["From", "Sent", "To", "Cc", "Subject"]
    assert dict(quote_header(row(), sent))["From"] == "Ana Ruiz <ana@x.com>"
    assert "Cc" not in dict(quote_header(row(cc_recipients="[]"), sent))
    text = quote_text(row(), sent, "Original words")
    assert text.index("From: Ana Ruiz") < text.index("Original words")


def test_graph_message():
    out = Outgoing("Hi", "<p>x</p>", to=['"Lee, Bo" <bo@y.org>', "bad"], cc=["c@z"],
                   bcc=["Hidden <h@q>"])
    m = out.graph_message()
    assert m["body"] == {"contentType": "html", "content": "<p>x</p>"}
    assert m["toRecipients"] == [{"emailAddress": {"name": "Lee, Bo", "address": "bo@y.org"}}]
    assert [r["emailAddress"]["address"] for r in m["bccRecipients"]] == ["h@q"]
    assert out.has_recipients() and not Outgoing("x", "y").has_recipients()
    assert Outgoing("x", "plain", html=False).graph_message()["body"]["contentType"] == "text"


def test_send_requests():
    class S:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, headers=None, json=None, **kw):
            self.calls.append((method, url.split("v1.0")[1], json))
            return FakeResponse(200 if "create" in url or url.endswith("messages") else 202,
                                {"id": "D1"} if "create" in url or url.endswith("messages")
                                else "")

    s = S()
    g = GraphClient(lambda: "t", session=s)
    g.send_mail({"subject": "x"})
    assert g.create_response("M1", "replyAll")["id"] == "D1"
    g.create_draft({"subject": "y"})
    g.send_draft("D1")
    assert [c[:2] for c in s.calls] == [("POST", "/me/sendMail"),
                                        ("POST", "/me/messages/M1/createReplyAll"),
                                        ("POST", "/me/messages"),
                                        ("POST", "/me/messages/D1/send")]
    assert s.calls[0][2] == {"message": {"subject": "x"}, "saveToSentItems": True}
