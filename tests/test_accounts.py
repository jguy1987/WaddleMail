"""Adding an account: the address goes to the sign-in page; no account means the wizard."""
from __future__ import annotations

from waddlemail.auth import Authenticator
from waddlemail.mailboxes import MailboxManager
from waddlemail.store import Store


class Tokens:
    def load(self):
        return None

    def save(self, data):
        pass


class FakeMsal:
    def __init__(self):
        self.calls, self.accounts = [], []

    def acquire_token_interactive(self, scopes, **kw):
        self.calls.append(kw)
        self.accounts = [{"home_account_id": "h1", "username": "jordan@contoso.com"}]
        return {"access_token": "t", "id_token_claims": {"preferred_username":
                                                         "jordan@contoso.com"}}

    def get_accounts(self, username=None):
        return [a for a in self.accounts if username in (None, a["username"])]


def auth() -> Authenticator:
    a = Authenticator(client_id="x", token_store=Tokens())
    a._app = FakeMsal()
    return a


def test_the_address_is_filled_in_on_the_sign_in_page(tmp_path):
    a = auth()
    boxes = MailboxManager(Store(tmp_path / "m.db"), a)
    assert not boxes.has_accounts()  # first run: the wizard asks
    box = boxes.add_account("jordan@contoso.com")
    assert a._app.calls == [{"login_hint": "jordan@contoso.com"}]
    assert box.display_name == "jordan@contoso.com" and boxes.has_accounts()
    a.add_account()  # no address: the browser's account picker
    assert a._app.calls[-1] == {"prompt": "select_account"}


def test_starting_with_no_account_signs_nobody_in(tmp_path):
    a = auth()
    boxes = MailboxManager(Store(tmp_path / "m.db"), a)
    boxes.ensure_accounts()  # (used to open the browser by itself)
    assert a._app.calls == [] and boxes.all() == []
