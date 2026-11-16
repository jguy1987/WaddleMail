"""An address field (To, Cc) that completes names as you type.

Suggestions come from contacts.suggestions(): the people you write to most, your contacts,
your contact groups (choosing one inserts all its members) and the organization directory.
Any part matches: first name, last name, address, company. Several addresses are separated
with "; ", and only the one being typed is completed.
"""
from __future__ import annotations

from email.utils import getaddresses

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QCompleter, QLineEdit

from ..contacts import Suggestion
from . import iconset

SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1
INSERT_ROLE = Qt.ItemDataRole.UserRole + 2
_ICONS = {"recent": "reply", "personal": "contact", "directory": "users-round",
          "group": "users-round"}


def split_addresses(text: str) -> list[str]:
    """'Ann <a@x>; "Lee, Bo" <b@x>, c@x' -> ['Ann <a@x>', '"Lee, Bo" <b@x>', 'c@x']."""
    out = []
    for name, address in getaddresses([text.replace(";", ",")]):
        if not address:
            continue
        if name:
            quoted = f'"{name}"' if any(c in name for c in ',;<>') else name
            out.append(f"{quoted} <{address}>")
        else:
            out.append(address)
    return out


class _Completer(QCompleter):
    def splitPath(self, path: str) -> list[str]:
        # complete only what follows the last separator
        last = max(path.rfind(";"), path.rfind(","))
        return [path[last + 1:].strip()]

    def pathFromIndex(self, index) -> str:
        # Highlighting a suggestion already writes it into the field, so what precedes the
        # address being typed comes from what the user typed (RecipientEdit.typed_before),
        # not from the field's current text: that would insert the choice twice.
        field = self.widget()
        before = field.typed_before() if field is not None else ""
        chosen = "; ".join(index.data(INSERT_ROLE) or [])
        return (f"{before} " if before else "") + chosen + "; "


class RecipientEdit(QLineEdit):
    def __init__(self, suggestions: list[Suggestion], parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Type a name or address")
        model = QStandardItemModel(self)
        for s in suggestions:
            item = QStandardItem(iconset.load(_ICONS.get(s.kind, "contact")), s.text)
            item.setData(s.search + " " + s.text.lower(), SEARCH_ROLE)
            item.setData(s.addresses, INSERT_ROLE)
            item.setToolTip("\n".join(s.addresses) if s.kind == "group" else s.text)
            model.appendRow(item)
        completer = _Completer(model, self)
        completer.setCompletionRole(SEARCH_ROLE)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setModelSorting(QCompleter.ModelSorting.UnsortedModel)  # keep our ranking
        completer.setMaxVisibleItems(10)
        self.setCompleter(completer)
        self._completer = completer
        self._before = ""
        self.textEdited.connect(self._remember_before)  # (not emitted by highlighting)

    def _remember_before(self, text: str) -> None:
        last = max(text.rfind(";"), text.rfind(","))
        self._before = text[:last + 1].strip()

    def typed_before(self) -> str:
        """The addresses already in the field before the one being typed."""
        return self._before

    def addresses(self) -> list[str]:
        return split_addresses(self.text())
