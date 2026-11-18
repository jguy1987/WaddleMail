"""The To/Cc field's completer."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from waddlemail.contacts import Suggestion  # noqa: E402
from waddlemail.ui.recipients import RecipientEdit  # noqa: E402

ANN, BOB = "Ann Lee <ann@x.com>", "Bob Ray <bob@x.com>"


def _choose(field: RecipientEdit, typed: str) -> None:
    QTest.keyClicks(field, typed)
    QTest.qWait(150)
    popup = field.completer().popup()
    QTest.keyClick(popup, Qt.Key.Key_Down)  # highlighting already writes into the field
    QTest.keyClick(popup, Qt.Key.Key_Return)
    QTest.qWait(50)


def test_choosing_a_suggestion_inserts_it_once(qapp):
    field = RecipientEdit([Suggestion(ANN, [ANN], "personal", "ann lee ann@x.com"),
                           Suggestion(BOB, [BOB], "personal", "bob ray bob@x.com")])
    field.show()
    field.setFocus()
    assert QTest.qWaitForWindowExposed(field)
    QTest.qWait(100)
    _choose(field, "an")
    assert field.text() == f"{ANN}; "
    _choose(field, "bo")
    assert field.text() == f"{ANN}; {BOB}; "
    assert field.addresses() == [ANN, BOB]
    field.close()
