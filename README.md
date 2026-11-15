# WaddleMail (prototype)

A Linux mail client for Microsoft 365 / Outlook.com built on Microsoft Graph.

## What works

- Sign-in with MSAL (public client, system browser + PKCE, no client secret). Tokens are kept in the
  system keyring, or a 0600 file if no keyring is available.
- **Several accounts and shared mailboxes** in one folder pane (File > Add account… / Add shared
  mailbox…, or right-click the pane). Each mailbox is a heading with its folders below; right-click
  a heading to remove a shared mailbox or sign out of an account. A shared mailbox can be a full
  mailbox (e.g. support@) or a person who shared only some folders with you: if the mailbox
  itself can't be listed, WaddleMail shows whichever default folders (Inbox, Sent Items, ...) you
  can open, with their subfolders.
- Folder pane in Outlook order: Inbox, Drafts, Sent Items, Deleted Items, Junk Email, Archive,
  Outbox, then other folders A-Z (subfolders A-Z). Each default folder has its own icon; unread
  counts are right-aligned (Drafts/Outbox show how many items they hold), and a collapsed folder
  adds up the unread mail in the subfolders it hides. Collapsed/expanded state is remembered.
- Message list served from a local SQLite store, kept current with Graph **delta queries** (one
  cursor per folder). If the first sync fails (offline), the stored mail is still shown.
  - Columns: read dot, replied/forwarded, attachment, importance, From (To in Sent Items, Drafts
    and Outbox), Subject, Received, flag; To and Size can be switched on. Click a header to sort
    by it, drag headers to reorder, right-click the header to show/hide columns. Order, widths
    and sort are remembered.
  - Unread mail is bold. Click the dot (or press **M**) to toggle read, click the flag column
    (or press **S**) to toggle the flag; both work on the whole selection. Message menu and
    right-click menu: Open in new window, Mark as read/unread (Ctrl+U), flag, Mark flag complete.
  - **Quick filter bar** (Thunderbird style, Ctrl+Shift+K): Unread / Flagged / Attachment
    toggles and a search box; every word typed must appear in the sender, recipients, subject or
    preview. Esc clears. The filter resets when you change folder.
- **Reading pane** (View > Reading pane): Right, Bottom or Off. On the right the list switches to
  compact two-line rows with a Sort button. The pane shows a header (subject, sender, To/Cc,
  date) above the body. Mail shown there is marked read, immediately or after a delay
  (Preferences > Mail), or not at all. With the pane off, selecting doesn't mark anything read.
- **Double-click** (or Enter) opens a message in its own window and marks it read; Esc closes it.
- Read/flag changes show at once and are queued locally, then sent to the server (also when
  offline: they go out on the next successful sync). If the server refuses one, for example in a
  shared mailbox you can only read, it is undone locally.
- Window size, pane layout and splitter positions are remembered.
- **Hybrid reader**: headers and previews are synced; a message body is fetched from the server the
  first time you open it, then cached (LRU). Nothing has to be downloaded up front.
- Sandboxed reader: JavaScript off, HTML sanitized (nh3), a Content-Security-Policy blocks remote
  images unless enabled, links open in your default browser.
- Menus: **File** (New > Mail message / Event, Send/Receive F5, Quit), **Edit** (Cut, Copy, Paste,
  Select all, Preferences), **View** (Load remote images for this session, folder pane), **Help**
  (About, shows the version).
- Toolbar: **New** (button + dropdown for Mail message / Event), **Send/Receive**, **Cut / Copy /
  Paste**, and Reply / Reply all / Forward / Delete (disabled placeholders until implemented).
  Toolbar and menus share the same actions. Cut/Copy/Paste act on whichever field has focus (the
  reader is read-only, so Copy/Select all work there and Cut/Paste enable once compose is a window
  of its own). Icons are bundled [Lucide](https://lucide.dev) SVGs (ISC licence, copy in
  `waddlemail/ui/icons/LICENSE-lucide.txt`), tinted to your theme's text colour at startup, so they
  work in light and dark themes (restart after switching theme). A missing icon falls back to text.
  To add one, copy `<name>.svg` from the `lucide-static` npm package (or
  `https://api.iconify.design/lucide/<name>.svg`) into `waddlemail/ui/icons/` and pass its name
  to `_themed(...)` in `ui/main_window.py`.
- **Preferences** (Edit > Preferences, Ctrl+,), saved to `~/.config/WaddleMail/WaddleMail.conf`:
  - Load remote images by default
  - Check for new mail every N minutes
  - Keep mail locally for N days/months/all (sync window)
  - Show in message list: last N days/months/all (display filter only; nothing is deleted)
  - Data location (applies after restart; existing data is not moved)
  - Body cache size limit, discard cached bodies not opened for N days/months, Clear cache button

## Not yet

Sending mail and saving events (the New dialogs open but only collect input), personal contacts,
calendar views, server-side search (the quick filter only searches mail stored locally, and the
preview rather than the full body), attachments and inline (`cid:`) images, move/delete,
conversation threading.

## Register the app (one time)

In the Entra admin center (entra.microsoft.com) > App registrations > New registration:

1. Supported account types: *Accounts in any organizational directory and personal Microsoft
   accounts* (or single tenant if it is only for your organization).
2. Redirect URI: platform **Mobile and desktop applications**, value `http://localhost`.
3. Under **Authentication**, set "Allow public client flows" to **Yes**.
4. **API permissions** > Microsoft Graph > Delegated: `Mail.ReadWrite` (mark read, flag) and
   `Mail.ReadWrite.Shared` (shared mailboxes and folders). Existing users are asked to consent
   once more after upgrading. Sending and events will
   need `Mail.Send` and `Calendars.ReadWrite` (add them to `SCOPES` in `config.py` as well). In your
   own tenant you can click "Grant admin consent" so users never see a prompt.
5. Copy the **Application (client) ID**.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export WADDLEMAIL_CLIENT_ID=<your client id>      # optional: WADDLEMAIL_TENANT=<tenant id>
waddlemail            # or: python -m waddlemail   or: python run.py
```

Do not run `waddlemail/__main__.py` directly as a script: it uses package-relative imports. In
PyCharm use a run configuration with **Module name** `waddlemail`, or use **Script path** `run.py`;
set `WADDLEMAIL_CLIENT_ID` under Environment variables.

The first launch opens your browser to sign in. Mail data lives in `~/.local/share/waddlemail/`
unless you change the data location in Preferences.

## Tests

```bash
pytest                                   # sync, cache, Graph client, renderer, settings (no network)
QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu" python tests/smoke_ui.py
```

## Layout

```
run.py                     launcher for IDEs
waddlemail/__init__.py     __version__ (single source; pyproject reads it)
waddlemail/auth.py         MSAL sign-in (several accounts) + token persistence
waddlemail/mailboxes.py    own + shared mailboxes; one Graph client/sync/body service per mailbox
waddlemail/graph.py        Graph client: paging, 429/503 retry, delta, bodies (ImmutableId ids)
waddlemail/store.py        SQLite: folders, messages, body cache (LRU), delta cursors
waddlemail/sync.py         Delta sync; first sync only pulls the last N days
waddlemail/bodies.py       Cache-first body access (the hybrid mode)
waddlemail/settings.py     Preferences (QSettings) and their defaults
waddlemail/ui/             main window + menus/toolbar, preferences, new mail/event dialogs, safe rendering
waddlemail/ui/folder_pane.py  folder tree: mailbox headings, folder order, icons, unread counts
waddlemail/ui/message_list.py  message model/view (columns, sorting, compact rows), quick filter bar
waddlemail/ui/message_view.py  message header + sandboxed body; the separate message window
waddlemail/ui/iconset.py   loads and tints the bundled Lucide SVGs (icons/)
```

## Design notes

- The UI never calls Graph directly: it reads the store and starts background workers.
- A shared mailbox goes through the signed-in account's token with Graph rooted at
  `/users/{address}` instead of `/me`. Graph can't list which mailboxes you have access to, so
  they are added by address (Outlook's automapping uses Exchange Autodiscover, not Graph).
- Delta cursors are saved only after all pages are applied, so an interrupted sync replays safely.
- A 410 (expired cursor) triggers a full resync that prunes messages deleted server-side.
- Immutable IDs: moving a message between folders keeps its id, so cached bodies stay valid, and a
  "removed" notice from the old folder cannot delete a message already re-homed in the new one.
- Delta updates merge field by field, so a partial update never blanks a subject or sender.
- Replied/forwarded state and size come from MAPI properties (`PidTagLastVerbExecuted` 0x1081,
  `PidTagMessageSize` 0x0E08) expanded in the same delta request. If a server refuses the
  `$expand`, sync continues without them. Adding these fields reset the delta cursors once, so
  existing mail gets them on the next sync.
- A queued read/flag change wins over an older value arriving in a delta page, so the list
  never flickers back to unread while the change is on its way.

## Next steps

1. Real sending (`Mail.Send`) through an outbox, and move/delete through the change queue
   (`pending` table) that read/flag already use.
2. Personal contacts (`/me/contacts`) and recipient autocomplete.
3. Events (`Calendars.ReadWrite`), then calendar views.
4. FTS5 search over metadata and cached bodies; attachments and `cid:` images.
5. "Load older mail" beyond the sync window.
6. Publisher verification of the app registration before sharing with other organizations.
