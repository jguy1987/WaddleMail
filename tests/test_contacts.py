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


def test_more_contact_fields():
    from waddlemail.contacts import format_postal
    r = personal_row({"id": "c", "displayName": "A", "birthday": "1990-04-02T11:59:00Z",
                      "businessHomePage": "x.com", "nickName": "Al",
                      "homeAddress": {"street": "1 Main St", "city": "Springfield",
                                      "state": "IL", "postalCode": "62701"}}, "a", "", "C")
    assert (r["birthday"], r["website"], r["nickname"]) == ("1990-04-02", "x.com", "Al")
    [[label, parts]] = json.loads(r["addresses"])
    assert label == "Home" and format_postal(parts) == "1 Main St\nSpringfield, IL 62701"


def test_member_identity_round_trip():
    from waddlemail.contacts import member_payload, removal_payload
    [ana, priya, _] = group_members(FakeGraph().contact_groups()[0]["members"],
                                    {"c1": [["Priya", "priya@x.com"]]})
    assert removal_payload(ana) == {"type": "mailbox", "key": "ana@x.com"}
    assert removal_payload(priya) == {"type": "contact", "key": "k1", "memberId": "c1"}
    assert removal_payload(["Old", "o@x"]) == {"type": "oneOff", "key": "o@x"}  # older format
    assert member_payload("Bo", "bo@y.org", "personal")["type"] == "contact"
    assert member_payload("Ana", "ana@x.com", "directory")["type"] == "mailbox"
    assert member_payload("", "z@z", "recent") == {"displayName": "z@z", "key": "z@z",
                                                   "type": "oneOff"}


class Recorder:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def request(self, method, url, params=None, headers=None, json=None, data=None, **kw):
        path = url.split("graph.microsoft.com")[1]
        self.calls.append((method, path, json, data, (headers or {}).get("Content-Type")))
        status = self.fail.pop((method, path), None)
        if status:
            return FakeResponse(status, {"error": "no"})
        if path.endswith("/photo/$value") and method == "GET":
            resp = FakeResponse(200, {})
            resp.content, resp.text = b"\x89PNG", "x"
            return resp
        return FakeResponse(200, {"id": "G1"} if method != "DELETE" else "")


def test_group_and_photo_requests():
    s = Recorder({("POST", "/beta/me/distributionLists/G1/addMembers"): 400,
                  ("GET", "/v1.0/users/u9/photo/$value"): 404})
    g = GraphClient(lambda: "t", session=s)
    assert g.create_group("Team", "n")["id"] == "G1"
    g.update_group("G1", "Team 2", "")
    g.add_group_members("G1", [{"displayName": "Bo", "key": "bo@y", "type": "contact"}])
    g.remove_group_members("G1", [{"type": "mailbox", "key": "a@x"}])
    g.delete_group("G1")
    assert g.photo("dir:u9") is None                 # no photo: not an error
    assert g.photo("c1", "fam") == b"\x89PNG"
    g.set_contact_photo("c1", b"JPEG")
    calls = [(m, p) for m, p, *_ in s.calls]
    assert calls == [
        ("POST", "/beta/me/distributionLists"),
        ("PATCH", "/beta/me/distributionLists/G1"),
        ("POST", "/beta/me/distributionLists/G1/addMembers"),   # refused as "contact"...
        ("POST", "/beta/me/distributionLists/G1/addMembers"),   # ...so again as an address
        ("POST", "/beta/me/distributionLists/G1/deleteMembers"),
        ("DELETE", "/beta/me/distributionLists/G1"),
        ("GET", "/v1.0/users/u9/photo/$value"),
        ("GET", "/v1.0/me/contactFolders/fam/contacts/c1/photo/$value"),
        ("PUT", "/v1.0/me/contacts/c1/photo/$value"),
    ]
    assert s.calls[3][2]["members"][0]["type"] == "oneOff"
    assert s.calls[-1][3:] == (b"JPEG", "image/jpeg")


def test_photo_cache_and_contacts_upgrade(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)  # a contacts table from before the extra columns
    c.execute("CREATE TABLE contacts(id TEXT PRIMARY KEY, account_id TEXT NOT NULL, source TEXT"
              " NOT NULL, folder_id TEXT, folder_name TEXT, display_name TEXT NOT NULL,"
              " given_name TEXT, surname TEXT, company TEXT, job_title TEXT, department TEXT,"
              " office TEXT, emails TEXT, phones TEXT, notes TEXT)")
    c.commit()
    c.close()
    st = Store(path)
    st.upsert_contact(personal_row({"id": "c", "displayName": "A", "nickName": "Al"},
                                   "a", "", "C"))
    assert st.contact_row("c")["nickname"] == "Al"
    assert st.photo("c") is None
    st.set_photo("c", None)            # asked; has none
    assert st.photo("c")[0] is None
    st.set_photo("c", b"img")
    assert st.photo("c")[0] == b"img"
    st.delete_contact("c")
    assert st.photo("c") is None
