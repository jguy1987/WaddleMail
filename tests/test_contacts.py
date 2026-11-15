"""Contacts: normalising Graph data, syncing each source, recent recipients, suggestions."""
from __future__ import annotations

import json

from waddlemail.contacts import (
    ContactSync, directory_row, format_address, group_members, personal_row, suggestions,
)
from waddlemail.graph import GraphClient, GraphError
from waddlemail.store import Store
from tests.test_mailboxes import FakeResponse


def contact(cid, first, last, *emails, **kw):
    return {"id": cid, "givenName": first, "surname": last, "displayName": f"{first} {last}",
            "emailAddresses": [{"name": f"{first} {last}", "address": e} for e in emails], **kw}


class FakeGraph:
    def __init__(self, groups_error=None, directory_error=None):
        self.groups_error, self.directory_error = groups_error, directory_error

    def contacts(self, folder_id=None):
        if folder_id == "fam":
            return [contact("c3", "Mom", "Lee", "mom@home.net")]
        return [contact("c1", "Priya", "Shah", "priya@x.com", companyName="Contoso",
                        businessPhones=["+1 555 0100"], mobilePhone="+1 555 0199"),
                contact("c2", "Bo", "Chan", "bo@y.org")]

    def contact_folders(self):
        return [{"id": "fam", "displayName": "Family"}]

    def directory_users(self):
        if self.directory_error:
            raise self.directory_error
        return [{"id": "u1", "displayName": "Ana Ruiz", "mail": "ana@x.com", "jobTitle": "CFO"},
                {"id": "u2", "displayName": "Conference Room"}]  # no mail: skipped

    def contact_groups(self):
        if self.groups_error:
            raise self.groups_error
        return [{"id": "g1", "displayName": "Project Team", "members": [
            {"id": "ana@x.com", "displayName": "Ana Ruiz", "type": "mailbox"},
            {"id": "k1", "displayName": "Priya", "type": "contact", "memberId": "c1"},
            {"id": "k2", "displayName": "Bo", "type": "contact",
             "contact": {"emailAddresses": [{"address": "bo@y.org"}]}},
        ]}]


def make_store(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    return st


def test_rows():
    r = personal_row(contact("c1", "Priya", "Shah", "priya@x.com", mobilePhone="1"), "a", "", "C")
    assert r["display_name"] == "Priya Shah" and json.loads(r["emails"]) == [["Priya Shah",
                                                                             "priya@x.com"]]
    assert json.loads(r["phones"]) == [["Mobile", "1"]]
    assert directory_row({"id": "u", "displayName": "Room"}, "a") is None
    assert directory_row({"id": "u", "mail": "a@x"}, "a")["id"] == "dir:u"


def test_group_member_addresses():
    members = group_members(FakeGraph().contact_groups()[0]["members"],
                            {"c1": [["Priya", "priya@x.com"]]})
    assert [m[1] for m in members] == ["ana@x.com", "priya@x.com", "bo@y.org"]


def test_refresh_all_sources(tmp_path):
    st = make_store(tmp_path)
    res = ContactSync(st, FakeGraph(), "a").refresh()
    assert (res.personal, res.directory, res.groups, res.problems) == (3, 1, 1, [])
    assert [r["display_name"] for r in st.contacts("personal")] == ["Bo Chan", "Mom Lee",
                                                                    "Priya Shah"]
    assert [r["display_name"] for r in st.contacts(search="contoso")] == ["Priya Shah"]
    assert [r["folder_name"] for r in st.contact_folders()] == ["Contacts", "Family"]
    assert len(json.loads(st.groups()[0]["members"])) == 3


def test_missing_sources_are_reported_not_fatal(tmp_path):
    st = make_store(tmp_path)
    res = ContactSync(st, FakeGraph(groups_error=GraphError("beta gone", 404),
                                    directory_error=GraphError("personal acct", 403)),
                      "a").refresh()
    assert res.personal == 3 and res.directory is None and res.groups is None
    assert len(res.problems) == 1 and "Contact groups" in res.problems[0]  # no directory: fine


def test_recent_recipients(tmp_path):
    st = make_store(tmp_path)
    st.upsert_folders("a", [{"id": "S", "displayName": "Sent Items", "wellKnownName": "sentitems"},
                            {"id": "I", "displayName": "Inbox", "wellKnownName": "inbox"}])
    to = lambda *a: [{"emailAddress": {"name": n, "address": x}} for n, x in a]  # noqa: E731
    st.apply_message_changes("S", [
        {"id": "s1", "receivedDateTime": "2026-11-01T00:00:00Z",
         "toRecipients": to(("Priya", "Priya@X.com"), ("Me", "me@x.com"))},
        {"id": "s2", "receivedDateTime": "2026-11-02T00:00:00Z",
         "toRecipients": to(("Priya Shah", "priya@x.com")), "ccRecipients": to(("Bo", "bo@y.org"))},
    ])
    st.apply_message_changes("I", [
        {"id": "i1", "receivedDateTime": "2026-11-03T00:00:00Z",
         "from": {"emailAddress": {"name": "Ana", "address": "ana@x.com"}},
         "singleValueExtendedProperties": [{"id": "Integer 0x1081", "value": "102"}]}])
    st.rebuild_recipients()
    rows = st.recent_recipients()
    # a reply counts double: Ana (1 reply) ties Priya (2 sent); the more recent goes first
    assert [r["address"] for r in rows] == ["ana@x.com", "priya@x.com", "bo@y.org"]
    assert (rows[1]["sent"], rows[1]["name"]) == (2, "Priya Shah")  # newest name wins
    assert rows[0]["replied"] == 1 and "me@x.com" not in [r["address"] for r in rows]


def test_suggestions_rank_and_dedupe(tmp_path):
    st = make_store(tmp_path)
    ContactSync(st, FakeGraph(), "a").refresh()
    st.conn.execute("INSERT INTO recipients VALUES('bo@y.org', 'Bo', 5, 0, '2026-11-01')")
    st.conn.commit()
    sug = suggestions(st)
    assert sug[0].kind == "recent" and "bo@y.org" in sug[0].text
    assert sum("bo@y.org" in s.text for s in sug) == 1          # once, as a recent
    group = next(s for s in sug if s.kind == "group")
    assert group.text == "Project Team (group, 3)" and len(group.addresses) == 3
    assert next(s for s in sug if "ana@x.com" in s.text).kind == "directory"
    assert format_address("Lee, Bo", "b@x") == '"Lee, Bo" <b@x>'


def test_contact_groups_use_beta_and_fall_back():
    class Session:
        def __init__(self):
            self.urls = []

        def request(self, method, url, params=None, **kw):
            self.urls.append((url, (params or {}).get("$expand")))
            if url.endswith("/distributionLists"):
                return FakeResponse(200, {"value": [{"id": "g1", "displayName": "T"}]})
            if params and params.get("$expand") == "members($expand=contact)":
                return FakeResponse(400, {"error": "nested expand not supported"})
            return FakeResponse(200, {"members": [{"id": "a@x", "type": "mailbox"}]})

    s = Session()
    groups = GraphClient(lambda: "t", session=s).contact_groups()
    assert groups[0]["members"] == [{"id": "a@x", "type": "mailbox"}]
    assert s.urls[0][0] == "https://graph.microsoft.com/beta/me/distributionLists"
    assert [e for _, e in s.urls[1:]] == ["members($expand=contact)", "members"]


def test_split_addresses():
    from waddlemail.ui.recipients import split_addresses
    assert split_addresses('Ann <a@x>; "Lee, Bo" <b@x>, c@x; ') == [
        "Ann <a@x>", '"Lee, Bo" <b@x>', "c@x"]
