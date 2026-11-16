"""Building messages: who a reply goes to, subjects, the Outlook-style header over quoted
text, and the Graph message to send (no Qt imports; unit-testable)."""
from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass, field
from email.utils import parseaddr

from .store import recipients

NEW, REPLY, REPLY_ALL, FORWARD = "new", "reply", "replyAll", "forward"
_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|sv|wg|tr|antw|vs)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)


def reply_subject(subject: str | None, kind: str) -> str:
    """"RE: x" / "FW: x", without stacking prefixes ("RE: RE: RE:")."""
    subject = subject or ""
    if kind == NEW:
        return subject
    want = "FW" if kind == FORWARD else "RE"
    m = _PREFIX.match(subject)
    family = {"FWD": "FW"}.get(m.group(1).upper(), m.group(1).upper()) if m else ""
    if family == want:
        return subject
    return f"{want}: {subject}"


def reply_recipients(row, kind: str, me: set[str]) -> tuple[list, list]:
    """(to, cc) as (name, address) lists for a reply to a stored message `row`. Reply: the
    sender; reply all: the sender and the other To, with the Cc kept; never yourself."""
    if kind not in (REPLY, REPLY_ALL):
        return [], []
    mine = {a.lower() for a in me}
    sender = (row["sender_name"] or "", row["sender_addr"] or "")
    if sender[1].lower() in mine:  # your own message: back to the people it went to
        to = [(n, a) for n, a in recipients(row["to_recipients"]) if a.lower() not in mine]
        cc = [] if kind == REPLY else [(n, a) for n, a in recipients(row["cc_recipients"])
                                       if a.lower() not in mine]
        return to, cc
    to = [sender] if sender[1] else []
    cc: list = []
    if kind == REPLY_ALL:
        seen = {sender[1].lower()} | mine
        for name, addr in recipients(row["to_recipients"]):
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                to.append((name, addr))
        for name, addr in recipients(row["cc_recipients"]):
            if addr and addr.lower() not in seen:
                seen.add(addr.lower())
                cc.append((name, addr))
    return to, cc


def _people(value) -> str:
    return "; ".join(f"{n} <{a}>" if n and n != a else a for n, a in recipients(value))


def quote_header(row, sent: str) -> list[tuple[str, str]]:
    """Outlook's lines over quoted text: From / Sent / To / Cc / Subject."""
    sender = row["sender_name"] or ""
    addr = row["sender_addr"] or ""
    lines = [("From", f"{sender} <{addr}>" if sender and addr else sender or addr),
             ("Sent", sent), ("To", _people(row["to_recipients"]))]
    if recipients(row["cc_recipients"]):
        lines.append(("Cc", _people(row["cc_recipients"])))
    lines.append(("Subject", row["subject"] or ""))
    return lines


def quote_html(row, sent: str, original_html: str) -> str:
    """A line, the header block, then the original message, as Outlook quotes it."""
    head = "<br>".join(f"<b>{html.escape(k)}:</b> {html.escape(v)}"
                       for k, v in quote_header(row, sent))
    return ("<hr><div style='font-family:Calibri,Arial,sans-serif'>"
            f"{head}</div><br>{original_html}")


def quote_text(row, sent: str, original_text: str) -> str:
    head = "\n".join(f"{k}: {v}" for k, v in quote_header(row, sent))
    return "\n\n" + "_" * 32 + "\n" + head + "\n\n" + original_text


def format_sent(iso: str | None) -> str:
    """"Monday, November 16, 2026 9:00 AM" in local time, as Outlook writes it."""
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    hour = t.hour % 12 or 12  # (no %-I: it isn't portable)
    return f"{t:%A}, {t:%B} {t.day}, {t.year} {hour}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


@dataclass
class Outgoing:
    """A finished message; graph_message() is what Graph wants."""
    subject: str
    body: str
    html: bool = True
    to: list = field(default_factory=list)   # "Name <address>" strings
    cc: list = field(default_factory=list)
    bcc: list = field(default_factory=list)
    inline: list = field(default_factory=list)  # InlineImage: the body's pictures (cid:)

    @staticmethod
    def _recips(items: list) -> list[dict]:
        out = []
        for text in items:
            name, address = parseaddr(text)
            if "@" in address:
                out.append({"emailAddress": {"name": name or address, "address": address}})
        return out

    def graph_message(self) -> dict:
        return {"subject": self.subject,
                "body": {"contentType": "html" if self.html else "text", "content": self.body},
                "toRecipients": self._recips(self.to), "ccRecipients": self._recips(self.cc),
                "bccRecipients": self._recips(self.bcc)}

    def has_recipients(self) -> bool:
        return bool(self._recips(self.to) or self._recips(self.cc) or self._recips(self.bcc))
