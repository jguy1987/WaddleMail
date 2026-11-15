"""Sign-in via MSAL (public client, system browser + PKCE, no client secret).

The MSAL token cache (which contains refresh tokens) is kept in the system
keyring (Secret Service) when available, otherwise in a 0600 file.
"""
from __future__ import annotations

import logging
import os
import threading

import msal

from . import config

log = logging.getLogger(__name__)

_KR_SERVICE = "waddlemail"
_KR_USER = "msal-token-cache"


class AuthError(Exception):
    pass


class TokenStore:
    """Persists the serialized MSAL cache."""

    def __init__(self) -> None:
        self._file = config.data_dir() / "token_cache.json"
        self._kr = None
        try:
            import keyring

            keyring.get_password(_KR_SERVICE, _KR_USER)  # probe the backend
            self._kr = keyring
        except Exception as exc:  # no backend, locked, etc.
            log.warning("keyring unavailable (%s); using 0600 file", exc)

    def load(self) -> str | None:
        if self._kr is not None:
            try:
                data = self._kr.get_password(_KR_SERVICE, _KR_USER)
                if data:
                    return data
            except Exception as exc:
                log.warning("keyring read failed: %s", exc)
        if self._file.exists():
            return self._file.read_text()
        return None

    def save(self, data: str) -> None:
        if self._kr is not None:
            try:
                self._kr.set_password(_KR_SERVICE, _KR_USER, data)
                return
            except Exception as exc:
                log.warning("keyring write failed (%s); using file", exc)
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(data)


class Authenticator:
    def __init__(
        self,
        client_id: str | None = None,
        authority: str | None = None,
        scopes: list[str] | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self.client_id = client_id or config.CLIENT_ID
        if not self.client_id:
            raise AuthError(
                "No client ID. Register an Entra app and set WADDLEMAIL_CLIENT_ID (see README)."
            )
        self.scopes = scopes or config.SCOPES
        self._store = token_store or TokenStore()
        self._cache = msal.SerializableTokenCache()
        data = self._store.load()
        if data:
            self._cache.deserialize(data)
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=authority or config.AUTHORITY,
            token_cache=self._cache,
        )
        self._lock = threading.Lock()  # one sign-in at a time

    def _persist(self) -> None:
        if self._cache.has_state_changed:
            self._store.save(self._cache.serialize())

    def accounts(self) -> list[dict]:
        """Signed-in accounts (MSAL dicts with "home_account_id" and "username")."""
        return self._app.get_accounts()

    def _account(self, account_id: str | None) -> dict | None:
        accounts = self._app.get_accounts()
        if account_id is None:
            return accounts[0] if accounts else None
        return next((a for a in accounts if a["home_account_id"] == account_id), None)

    def get_token(self, account_id: str | None = None, interactive: bool = True) -> str:
        """Token for one account (default: the first). Opens the browser if it must."""
        with self._lock:
            result = None
            account = self._account(account_id)
            if account:
                result = self._app.acquire_token_silent(self.scopes, account=account)
            if not result and interactive:
                hint = account["username"] if account else None
                result = self._app.acquire_token_interactive(self.scopes, login_hint=hint)
            self._persist()
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "sign-in required")
            raise AuthError(detail)
        return result["access_token"]

    def add_account(self) -> dict:
        """Interactive sign-in with the account picker; returns the (new or existing) account."""
        with self._lock:
            result = self._app.acquire_token_interactive(self.scopes, prompt="select_account")
            self._persist()
        if "access_token" not in result:
            raise AuthError(result.get("error_description", "sign-in cancelled"))
        claims = result.get("id_token_claims") or {}
        username = claims.get("preferred_username")
        matches = self._app.get_accounts(username=username) if username else []
        if not matches:
            raise AuthError("signed in, but the account was not found in the token cache")
        return matches[0]

    def sign_out(self, account_id: str | None = None) -> None:
        """Forget one account, or all of them."""
        with self._lock:
            for acct in self._app.get_accounts():
                if account_id is None or acct["home_account_id"] == account_id:
                    self._app.remove_account(acct)
            self._persist()
