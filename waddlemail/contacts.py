"""Contacts: personal contacts (every contact folder), the organization directory, personal
contact groups, and the people you send to and reply to.

Sync is a full refresh per source (contacts are few compared with mail, and Graph has no delta
for contact groups). Each source is independent: a personal Microsoft account has no
directory, and contact groups exist only in Graph's beta API, so either may be unavailable
without affecting the rest. `ContactSync.refresh` reports which sources worked.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .graph import GraphClient, GraphError
from .store import Store

log = logging.getLogger(__name__)

PERSONAL, DIRECTORY = "personal", "directory"


def _phones(item: dict) -> list[list[str]]:
    out = [["Work", n] for n in item.get("businessPhones") or [] if n]
    if item.get("mobilePhone"):
        out.append(["Mobile", item["mobilePhone"]])
    out += [["Home", n] for n in item.get("homePhones") or [] if n]
    return out


ADDRESS_PARTS = ("street", "city", "state", "postalCode", "countryOrRegion")


def _addresses(item: dict) -> list:
    """[[label, {street, city, state, postalCode, countryOrRegion}], ...] for non-empty ones."""
    out = []
    for label, key in (("Work", "businessAddress"), ("Home", "homeAddress")):
        a = item.get(key) or {}
        parts = {k: a.get(k) or "" for k in ADDRESS_PARTS}
        if any(parts.values()):
            out.append([label, parts])
    return out


def format_postal(parts: dict) -> str:
    """Street on its own line(s), then "City, State Postcode", then the country."""
    line2 = " ".join(x for x in (", ".join(x for x in (parts.get("city"), parts.get("state"))
                                           if x), parts.get("postalCode")) if x)
    return "\n".join(x for x in (parts.get("street"), line2, parts.get("countryOrRegion")) if x)


def personal_row(item: dict, account_id: str, folder_id: str, folder_name: str) -> dict:
    name = item.get("displayName") or " ".join(
        x for x in (item.get("givenName"), item.get("surname")) if x)
    emails = [[e.get("name") or "", e.get("address") or ""]
              for e in item.get("emailAddresses") or [] if e.get("address")]
    return {
        "id": item["id"], "account_id": account_id, "source": PERSONAL,
        "folder_id": folder_id, "folder_name": folder_name,
        "display_name": name or (emails[0][1] if emails else "(no name)"),
        "given_name": item.get("givenName"), "surname": item.get("surname"),
        "company": item.get("companyName"), "job_title": item.get("jobTitle"),
        "department": item.get("department"), "office": item.get("officeLocation"),
        "emails": json.dumps(emails), "phones": json.dumps(_phones(item)),
        "notes": item.get("personalNotes"),
        "addresses": json.dumps(_addresses(item)),
        "birthday": (item.get("birthday") or "")[:10] or None,
        "website": item.get("businessHomePage"),
        "nickname": item.get("nickName"),
    }


def directory_row(user: dict, account_id: str) -> dict | None:
    address = user.get("mail")
    if not address:  # rooms without mail, service accounts, guests not yet redeemed, ...
        return None
    return {
        "id": "dir:" + user["id"], "account_id": account_id, "source": DIRECTORY,
        "folder_id": None, "folder_name": None,
        "display_name": user.get("displayName") or address,
        "given_name": user.get("givenName"), "surname": user.get("surname"),
        "company": user.get("companyName"), "job_title": user.get("jobTitle"),
        "department": user.get("department"), "office": user.get("officeLocation"),
        "emails": json.dumps([[user.get("displayName") or "", address]]),
        "phones": json.dumps(_phones(user)), "notes": None,
    }


def group_members(raw: list[dict], personal: dict[str, list]) -> list[list[str]]:
    """[name, address, type, key, memberId] per member. The member's id (its key) is its
    address for mailbox and one-off members; contact members carry (or point to) a contact
    with addresses. key/memberId are what removing the member needs."""
    out = []
    for m in raw:
        kind = m.get("type") or m.get("recipientType") or ""
        name = m.get("displayName") or ""
        address = ""
        key = m.get("id") or m.get("key") or ""
        if "@" in key:
            address = key
        contact = m.get("contact") or {}
        for e in contact.get("emailAddresses") or []:
            address = address or e.get("address") or ""
        if not address and m.get("memberId") in personal:
            emails = personal[m["memberId"]]
            address = emails[0][1] if emails else ""
        out.append([name or address, address, kind, key, m.get("memberId") or ""])
    return out


def member_payload(name: str, address: str, source: str) -> dict:
    """How to add someone to a contact group: one of your contacts, a directory mailbox, or
    just an address."""
    kind = {PERSONAL: "contact", DIRECTORY: "mailbox"}.get(source, "oneOff")
    return {"displayName": name or address, "key": address, "type": kind}


def removal_payload(member: list) -> dict:
    """Identify a stored member ([name, address, type, key, memberId]) for deleteMembers."""
    address = member[1]
    kind = (member[2] if len(member) > 2 else "") or "oneOff"  # (older records lack fields)
    key = member[3] if len(member) > 3 and member[3] else address
    out = {"type": kind, "key": key}
    if len(member) > 4 and member[4]:
        out["memberId"] = member[4]
    return out


@dataclass
class RefreshResult:
    personal: int | None = None    # None: that source failed / isn't available
    directory: int | None = None
    groups: int | None = None
    problems: list[str] = field(default_factory=list)


class ContactSync:
    def __init__(self, store: Store, graph: GraphClient, account_id: str) -> None:
        self.store, self.graph, self.account_id = store, graph, account_id

    def refresh(self) -> RefreshResult:
        res = RefreshResult()
        try:
            res.personal = self.sync_personal()
        except Exception as exc:
            log.warning("personal contacts: %s", exc)
            res.problems.append(f"Personal contacts: {exc}")
        try:
            res.directory = self.sync_directory()
        except GraphError as exc:  # personal accounts have no directory: not an error
            if exc.status not in (400, 403, 404):
                res.problems.append(f"Directory: {exc}")
            log.info("directory unavailable: %s", exc)
        except Exception as exc:
            res.problems.append(f"Directory: {exc}")
        try:
            res.groups = self.sync_groups()
        except Exception as exc:
            log.warning("contact groups: %s", exc)
            res.problems.append(f"Contact groups: {exc}")
        return res

    def sync_personal(self) -> int:
        rows = [personal_row(c, self.account_id, "", "Contacts") for c in self.graph.contacts()]
        for folder in self.graph.contact_folders():
            rows += [personal_row(c, self.account_id, folder["id"], folder["displayName"])
                     for c in self.graph.contacts(folder["id"])]
        self.store.replace_contacts(self.account_id, PERSONAL, rows)
        return len(rows)

    def sync_directory(self) -> int:
        rows = [r for r in (directory_row(u, self.account_id)
                            for u in self.graph.directory_users()) if r]
        self.store.replace_contacts(self.account_id, DIRECTORY, rows)
        return len(rows)

    def photo(self, contact_id: str, folder_id: str | None = None) -> bytes | None:
        """Fetch a photo and cache it (also "no photo", so it isn't asked again soon)."""
        data = self.graph.photo(contact_id, folder_id)
        self.store.set_photo(contact_id, data)
        return data

    def sync_groups(self) -> int:
        personal = {r["id"]: json.loads(r["emails"] or "[]")
                    for r in self.store.contacts(PERSONAL)}
        groups = [{"id": g["id"], "name": g.get("displayName") or "(no name)",
                   "notes": g.get("notes"), "members": group_members(g["members"], personal)}
                  for g in self.graph.contact_groups()]
        self.store.replace_groups(self.account_id, groups)
        return len(groups)


# -- suggestions for the To / Cc fields ------------------------------------------------
@dataclass
class Suggestion:
    text: str                 # what the list shows
    addresses: list[str]      # what choosing it inserts ("Name <address>" each)
    kind: str                 # recent / personal / directory / group
    search: str               # lower-case text matched against what is typed


def format_address(name: str, address: str) -> str:
    if not name or name.lower() == address.lower():
        return address
    if any(c in name for c in ',;<>"'):
        name = '"' + name.replace('"', "'") + '"'
    return f"{name} <{address}>"


def suggestions(store: Store) -> list[Suggestion]:
    """Everyone you could address, best first: people you use most, then your contacts, then
    contact groups, then the directory. Each address appears once."""
    out: list[Suggestion] = []
    seen: set[str] = set()

    def add(name: str, address: str, kind: str, extra: str = ""):
        key = address.lower()
        if not address or key in seen:
            return
        seen.add(key)
        text = format_address(name, address)
        out.append(Suggestion(text, [text], kind, f"{name} {address} {extra}".lower()))

    for r in store.recent_recipients(limit=2000):
        add(r["name"] or "", r["address"], "recent")
    for source in (PERSONAL, "group", DIRECTORY):
        if source == "group":
            for g in store.groups():
                members = [format_address(m[0], m[1]) for m in json.loads(g["members"] or "[]")
                           if m[1]]
                if members:
                    out.append(Suggestion(f"{g['name']} (group, {len(members)})", members,
                                          "group", g["name"].lower()))
            continue
        for c in store.contacts(source):
            extra = " ".join(x or "" for x in (c["company"], c["department"]))
            for name, address in json.loads(c["emails"] or "[]"):
                add(c["display_name"] or name, address, source, extra)
    return out

