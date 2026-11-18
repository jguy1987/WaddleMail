"""Meeting invitations in the mailbox: reading Graph's event messages, and answering."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as dt  # noqa: E402

from waddlemail.graph import GraphClient  # noqa: E402
from waddlemail.invites import parse_invite  # noqa: E402
from tests.test_mailboxes import FakeResponse  # noqa: E402


def event(**kw):
    ev = {
        "id": "EV1", "subject": "placeholder for website review #1",
        "start": {"dateTime": "2026-11-19T21:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-19T22:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": False, "location": {"displayName": "Jeff's office"},
        "organizer": {"emailAddress": {"name": "Jeff Siblik", "address": "jsiblik@x.com"}},
        "isOrganizer": False, "responseStatus": {"response": "notResponded"},
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/meet/1"},
    }
    ev.update(kw)
    return ev


def request(**kw):
    msg = {"id": "M1", "@odata.type": "#microsoft.graph.eventMessageRequest",
           "meetingMessageType": "meetingRequest", "isOutOfDate": False, "event": event()}
    msg.update(kw)
    return msg


def test_a_meeting_request():
    inv = parse_invite(request())
    assert inv.event_id == "EV1" and inv.subject == "placeholder for website review #1"
    assert inv.location == "Jeff's office" and inv.organizer == "Jeff Siblik"
    assert (inv.start.astimezone(dt.timezone.utc).hour, (inv.end - inv.start).seconds) == (21, 3600)
    assert inv.can_respond and inv.response == "notResponded"
    assert inv.status_text() == "Jeff Siblik invited you. You haven't responded."
    assert inv.online_url.startswith("https://teams.microsoft.com")


def test_your_answer_is_shown_and_remembered():
    inv = parse_invite(request(event=event(responseStatus={"response": "accepted"})))
    assert inv.status_text() == "Jeff Siblik invited you. You accepted"
    again = inv.answered("decline")
    assert again.response == "declined" and "You declined" in again.status_text()
    assert inv.response == "accepted"  # (the original is unchanged)
    assert parse_invite(request(event=event(responseStatus={"response": "none"}))
                        ).response == "notResponded"


def test_updates_cancellations_and_your_own_meetings():
    assert "out of date" in parse_invite(request(isOutOfDate=True)).status_text()
    cancelled = parse_invite(request(**{"@odata.type": "#microsoft.graph.eventMessage",
                                        "meetingMessageType": "meetingCancelled"}))
    assert cancelled.cancelled and not cancelled.can_respond
    assert cancelled.status_text() == "This meeting was canceled."
    mine = parse_invite(request(event=event(isOrganizer=True)))
    assert not mine.can_respond and mine.status_text() == "You organized this meeting."


def test_an_update_says_what_changed():
    # the case from the screenshot: Fri 09:00-10:00 moved to Thu 15:00-16:00 (UTC 21:00-22:00)
    update = parse_invite(request(
        meetingRequestType="fullUpdate",
        previousStartDateTime={"dateTime": "2026-11-20T15:00:00.0000000", "timeZone": "UTC"},
        previousEndDateTime={"dateTime": "2026-11-20T16:00:00.0000000", "timeZone": "UTC"}))
    assert update.updated and update.time_changed and not update.location_changed
    assert update.status_text() == "Jeff Siblik updated this meeting. You haven't responded."
    assert update.was_start.astimezone(dt.timezone.utc).day == 20
    moved = parse_invite(request(meetingRequestType="fullUpdate",
                                 previousLocation={"displayName": "Room 2"}))
    assert moved.updated and moved.location_changed and not moved.time_changed
    # an update with nothing we can compare is still an update, and says no more than that
    plain = parse_invite(request(meetingRequestType="informationalUpdate"))
    assert plain.updated and not plain.time_changed and not plain.location_changed
    new = parse_invite(request(meetingRequestType="newMeetingRequest"))
    assert not new.updated and new.status_text().startswith("Jeff Siblik invited you.")
    # a time in another zone isn't guessed at
    odd = parse_invite(request(meetingRequestType="fullUpdate", previousStartDateTime={
        "dateTime": "2026-11-20T09:00:00.0000000", "timeZone": "Central Standard Time"}))
    assert odd.updated and odd.was_start is None
    assert parse_invite(request(meetingRequestType="outdated")).out_of_date


def test_all_day_meeting():
    inv = parse_invite(request(event=event(
        isAllDay=True, start={"dateTime": "2026-11-19T00:00:00.0000000", "timeZone": "UTC"},
        end={"dateTime": "2026-11-20T00:00:00.0000000", "timeZone": "UTC"})))
    assert inv.all_day and inv.start.date() == dt.date(2026, 11, 19)
    assert inv.end.date() == dt.date(2026, 11, 20)


def test_things_that_are_not_invitations():
    assert parse_invite(None) is None and parse_invite({}) is None
    assert parse_invite({"id": "M2", "subject": "hello"}) is None           # ordinary mail
    assert parse_invite(request(event=None)) is None                         # not on the calendar
    assert parse_invite(request(**{"@odata.type": "#microsoft.graph.eventMessageResponse",
                                   "meetingMessageType": "meetingAccepted"})) is None


def test_graph_asks_for_the_event_with_the_message():
    class Session:
        def request(self, method, url, **kw):
            self.method, self.url, self.kw = method, url, kw
            return FakeResponse(200, {"id": "M1"})

    s = Session()
    GraphClient(lambda: "t", session=s).message_event("M/1")
    assert s.method == "GET" and s.url.endswith("/me/messages/M%2F1")
    assert s.kw["params"] == {"$expand": "microsoft.graph.eventMessage/event"}


def test_the_card_shows_and_asks_to_answer(qapp):
    from waddlemail.ui.message_view import InviteBar
    bar = InviteBar()
    got = []
    bar.respond_requested.connect(lambda *a: got.append(a))
    bar.show_invite("M1", parse_invite(request()))
    assert not bar.isHidden() and "Jeff's office" == bar.where.text()
    assert all(not b.isHidden() and b.isEnabled() for b in bar.buttons.values())
    bar.buttons["tentativelyAccept"].click()
    assert got == [("M1", "EV1", "tentativelyAccept")]
    assert not any(b.isEnabled() for b in bar.buttons.values())  # until the answer is in
    assert bar.changes.isHidden()  # (a new invitation changed nothing)
    update = parse_invite(request(meetingRequestType="fullUpdate", previousLocation={
        "displayName": "Room 2"}))
    bar.show_invite("M1", update)
    assert not bar.changes.isHidden() and bar.changes.text() == "Location changed. Was: Room 2"
    bar.show_invite("M1", parse_invite(request()).answered("tentativelyAccept"))
    assert not bar.buttons["tentativelyAccept"].isEnabled() and bar.buttons["accept"].isEnabled()
    bar.show_invite("M1", parse_invite(request(event=event(isOrganizer=True))))
    assert all(b.isHidden() for b in bar.buttons.values())
    bar.forget()
    assert bar.isHidden()
    bar.setParent(None)
    bar.deleteLater()


def test_the_list_marks_meeting_messages(qapp):
    from waddlemail.ui.message_list import MessageModel, _meeting

    row = {"meeting": "eventMessageRequest", "is_read": 0}
    assert _meeting(row) == "eventMessageRequest" and _meeting({"is_read": 1}) is None
    model = MessageModel()
    unread_mail = model.message_icon({"is_read": 0})
    read_meeting = model.message_icon({"is_read": 1, "meeting": "eventMessageRequest"})
    assert not unread_mail.isNull() and not read_meeting.isNull()
    pix = lambda i: i.pixmap(16, 16).toImage()  # noqa: E731
    assert pix(unread_mail) != pix(read_meeting)           # different picture, and colour
    assert pix(model.message_icon({"is_read": 0, "meeting": "eventMessage"})) != \
        pix(unread_mail)                                   # a calendar, not an envelope
    assert pix(model.message_icon({"is_read": 0})) != pix(model.message_icon({"is_read": 1}))


def test_an_answer_is_never_sent_twice():
    """Answering mails the organizer, so Graph must not retry it after an unclear failure."""
    class Session:
        def __init__(self):
            self.n = 0

        def request(self, method, url, **kw):
            self.n += 1
            return FakeResponse(503, {"error": "busy"})

    s = Session()
    g = GraphClient(lambda: "t", session=s)
    import pytest

    from waddlemail.graph import GraphError
    with pytest.raises(GraphError):
        g.respond_event("EV1", "accept")
    assert s.n == 1  # (not retried)


class _Box:
    class graph:
        sent: list = []

        @staticmethod
        def respond_event(event_id, response):
            _Box.graph.sent.append((event_id, response))


def test_the_window_sends_one_answer_however_often_it_is_asked(qapp):
    """The buttons can fire twice (two connections, a double click): one answer goes out."""
    from types import SimpleNamespace

    from waddlemail.ui.main_window import MainWindow

    jobs, shown, bar = [], [], SimpleNamespace(shown=0)
    inv = parse_invite(request())
    fake = SimpleNamespace(
        store=SimpleNamespace(message_row=lambda mid: {"folder_id": "F"}),
        mailboxes=SimpleNamespace(for_folder=lambda fid: _Box),
        _invites={"M1": inv}, _answering=set(),
        _run=lambda job, done, **kw: jobs.append((job, done, kw)),
        _show_invite=lambda mid, i: shown.append((mid, i.response)),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *a: None),
        refresh_calendar=lambda: None)
    _Box.graph.sent = []
    MainWindow.respond_invite(fake, "M1", "EV1", "accept")
    MainWindow.respond_invite(fake, "M1", "EV1", "accept")   # the same click, again
    assert len(jobs) == 1
    job, done, _ = jobs[0]
    job()
    done(None)
    assert _Box.graph.sent == [("EV1", "accept")] and shown == [("M1", "accepted")]
    MainWindow.respond_invite(fake, "M1", "EV1", "decline")  # a change of mind is allowed
    assert len(jobs) == 2


def test_the_status_bar_shows_progress_not_just_connecting(qapp):
    from waddlemail.ui.status_indicator import StatusIndicator

    bar = StatusIndicator(lambda: 0)
    token = bar.begin("Connecting…")
    assert bar.text() == "Connecting…"
    bar.progress("Syncing Inbox… 1,200 messages")
    qapp.processEvents()
    assert bar.text() == "Syncing Inbox… 1,200 messages"
    bar.end(token)
    bar.progress("late report")                      # from a job that has already ended
    qapp.processEvents()
    assert "late report" not in bar.text() and "Syncing" not in bar.text()
    bar.setParent(None)
    bar.deleteLater()
