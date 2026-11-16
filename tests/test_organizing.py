"""Favorite folders, the order of accounts, and keeping each account's contacts apart."""
from __future__ import annotations

from waddlemail.store import Store


def folders(st, mailbox, *ids):
    st.upsert_folders(mailbox, [{"id": i, "displayName": i.title()} for i in ids])


def test_favorites_keep_their_order_and_survive_syncs(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "Work")
    st.add_mailbox("b", "b", None, "Home")
    folders(st, "a", "inbox", "projects")
    folders(st, "b", "family")
    for fid in ("projects", "family", "inbox"):
        st.set_favorite(fid, True)
    st.set_favorite("projects", True)  # (already one: stays where it is)
    assert [r["id"] for r in st.favorite_folders()] == ["projects", "family", "inbox"]
    st.move_favorite("inbox", -1)
    assert [r["id"] for r in st.favorite_folders()] == ["projects", "inbox", "family"]
    folders(st, "a", "inbox", "projects")  # a sync: favorites stay
    assert len(st.favorite_folders()) == 3
    st.set_favorite("inbox", False)
    folders(st, "a", "inbox")  # a favorite folder deleted on the server goes too
    assert [r["id"] for r in st.favorite_folders()] == ["family"]


def test_accounts_can_be_reordered(tmp_path):
    st = Store(tmp_path / "m.db")
    for mid, name in (("a", "Work"), ("b", "Home"), ("c", "Club")):
        st.add_mailbox(mid, mid, None, name)
    st.move_mailbox("c", -1)
    assert [r["id"] for r in st.mailbox_rows()] == ["a", "c", "b"]
    st.move_mailbox("a", -1)  # (already first)
    st.move_mailbox("a", 5)   # (as far as it goes)
    assert [r["id"] for r in st.mailbox_rows()] == ["c", "b", "a"]


def test_each_account_has_its_own_contacts_and_recipients(tmp_path):
    st = Store(tmp_path / "m.db")
    for acct, name in (("a", "Work"), ("b", "Home")):
        st.add_mailbox(acct, acct, None, name)
        st.upsert_folders(acct, [{"id": f"sent-{acct}", "displayName": "Sent Items",
                                  "wellKnownName": "sentitems"}])
        st.upsert_contact({"id": f"c-{acct}", "account_id": acct, "source": "personal",
                           "folder_id": "", "display_name": f"{name} friend"})
        st.upsert_contact({"id": f"d-{acct}", "account_id": acct, "source": "directory",
                           "display_name": f"{name} colleague"})
    to = lambda a: [{"emailAddress": {"name": a, "address": a}}]  # noqa: E731
    st.apply_message_changes("sent-a", [{"id": "s1", "toRecipients": to("boss@work.com"),
                                         "receivedDateTime": "2026-11-01T00:00:00Z"}])
    st.apply_message_changes("sent-b", [{"id": "s2", "toRecipients": to("mum@home.org"),
                                         "receivedDateTime": "2026-11-02T00:00:00Z"},
                                        {"id": "s3", "toRecipients": to("boss@work.com"),
                                         "receivedDateTime": "2026-11-03T00:00:00Z"}])
    st.rebuild_recipients()
    assert [r["display_name"] for r in st.contacts(account_id="b")] == ["Home colleague",
                                                                         "Home friend"]
    assert [r["display_name"] for r in st.contacts("directory", account_id="a")] == \
        ["Work colleague"]
    assert [r["account_id"] for r in st.contact_folders("a")] == ["a"]
    assert [r["address"] for r in st.recent_recipients(account_id="a")] == ["boss@work.com"]
    everyone = {r["address"]: r["sent"] for r in st.recent_recipients()}
    assert everyone == {"boss@work.com": 2, "mum@home.org": 1}  # all accounts: added up
