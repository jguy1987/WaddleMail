"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

# Entra app registration (public client). See README for setup.
CLIENT_ID = os.environ.get("WADDLEMAIL_CLIENT_ID", "")
# "common" = work/school and personal accounts. Use your tenant ID to restrict.
TENANT = os.environ.get("WADDLEMAIL_TENANT", "common")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"

# Delegated scopes. offline_access/openid/profile are added by MSAL itself.
# Mail.Read.Shared opens shared mailboxes and folders other people have shared with you.
SCOPES = ["Mail.Read", "Mail.Read.Shared"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# First sync of a folder only pulls this many days of mail; older mail is
# fetched on demand later (not implemented in this prototype).
INITIAL_SYNC_DAYS = int(os.environ.get("WADDLEMAIL_SYNC_DAYS", "90"))

# Upper bound for cached message bodies (least recently used are evicted).
BODY_CACHE_MAX_BYTES = int(os.environ.get("WADDLEMAIL_BODY_CACHE_MB", "200")) * 1024 * 1024


def data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "waddlemail"
    d.mkdir(parents=True, exist_ok=True)
    return d
