"""Meeting invitations in the mailbox (no Qt imports; unit-testable).

Exchange delivers an invitation as an event message (Graph: eventMessage / eventMessageRequest)
that points at an event on your calendar. GraphClient.message_event() fetches the message with
that event expanded; parse_invite() reads it. Answering is done on the event (accept,
tentativelyAccept, decline), the same as from the calendar.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .calendars import event_row, event_span, parse_utc

# Your response, as Graph's responseStatus.response says it.
RESPONSE_TEXT = {
    "accepted": "You accepted",
    "tentativelyAccepted": "You tentatively accepted",
    "declined": "You declined",
}
ANSWERS = {"accept": "accepted", "tentativelyAccept": "tentativelyAccepted",
           "decline": "declined"}


# Graph's meetingRequestType: the first message about a meeting, or an update to it.
_UPDATES = {"fullupdate", "informationalupdate", "silentupdate"}


def _utc(value: dict | None) -> dt.datetime | None:
    """A dateTimeTimeZone as a local time, when it is in UTC (we ask Graph for UTC)."""
    if not value or not value.get("dateTime"):
        return None
    if (value.get("timeZone") or "UTC").upper() not in ("UTC", "ETC/UTC"):
        return None  # another zone: better to say nothing than to show a wrong time
    try:
        return parse_utc(value["dateTime"]).astimezone()
    except ValueError:
        return None


@dataclass
class Invite:
    event_id: str
    subject: str
    start: dt.datetime        # local time (all-day: midnight)
    end: dt.datetime
    all_day: bool
    location: str
    organizer: str            # the organizer's name (or address)
    response: str             # notResponded / accepted / tentativelyAccepted / declined / organizer
    is_organizer: bool
    cancelled: bool           # the meeting was canceled
    out_of_date: bool         # a newer update to the meeting has arrived
    online_url: str = ""
    updated: bool = False     # this changes a meeting you were already told about
    was_start: dt.datetime | None = None   # before the update (None: unchanged / unknown)
    was_end: dt.datetime | None = None
    was_location: str | None = None

    @property
    def can_respond(self) -> bool:
        return not self.cancelled and not self.is_organizer

    def status_text(self) -> str:
        if self.cancelled:
            return "This meeting was canceled."
        if self.is_organizer:
            return "You organized this meeting."
        if self.updated:
            who = f"{self.organizer} updated this meeting" if self.organizer else \
                "This meeting was updated"
        else:
            who = f"{self.organizer} invited you" if self.organizer else "You were invited"
        answer = RESPONSE_TEXT.get(self.response)
        text = f"{who}. " + (answer if answer else "You haven't responded.")
        return text + (" This invitation is out of date: a newer one has arrived."
                       if self.out_of_date else "")

    @property
    def time_changed(self) -> bool:
        return self.was_start is not None and (self.was_start, self.was_end) != (
            self.start, self.end)

    @property
    def location_changed(self) -> bool:
        return self.was_location is not None and self.was_location != self.location

    def answered(self, response: str) -> Invite:
        """The invite after you answered it (`response`: accept / tentativelyAccept / decline)."""
        return Invite(**{**self.__dict__, "response": ANSWERS[response]})


def parse_invite(data: dict | None) -> Invite | None:
    """From a message fetched with its event expanded; None when it isn't an invitation, or
    when its event isn't on the calendar (there is nothing to answer then)."""
    if not data:
        return None
    kind = (data.get("meetingMessageType") or "").lower()
    if not (data.get("@odata.type") or "").lower().endswith(("eventmessagerequest", "eventmessage")):
        return None
    if kind not in ("meetingrequest", "meetingcancelled"):  # responses to your own meetings
        return None
    event = data.get("event")
    if not event or not event.get("id"):
        return None
    row = event_row(event, "", "")
    start, end = event_span(row)
    response = (event.get("responseStatus") or {}).get("response") or "notResponded"
    request_type = (data.get("meetingRequestType") or "").lower()
    was_start, was_end = _utc(data.get("previousStartDateTime")), _utc(data.get("previousEndDateTime"))
    was_location = (data.get("previousLocation") or {}).get("displayName")
    return Invite(
        event_id=event["id"], subject=row["subject"], start=start, end=end,
        all_day=bool(row["all_day"]), location=row["location"],
        organizer=row["organizer_name"] or row["organizer_addr"],
        response="notResponded" if response == "none" else response,
        is_organizer=bool(row["is_organizer"]),
        cancelled=kind == "meetingcancelled" or bool(row["cancelled"]),
        out_of_date=bool(data.get("isOutOfDate")) or request_type == "outdated",
        online_url=row["online_url"],
        updated=kind == "meetingrequest" and (request_type in _UPDATES or bool(
            was_start or was_location)),
        was_start=was_start, was_end=was_end if was_start else None,
        was_location=was_location)
