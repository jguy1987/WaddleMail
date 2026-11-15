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
- **Folder management**: right-click a folder for New subfolder, Rename (F2) and Delete (Del),
  or a mailbox heading for New folder; also File > New > Folder. Names are typed in place, as in
  Outlook (Enter saves, Esc cancels); a name already used next to it is refused. Delete moves
  the folder and everything in it to Deleted Items; deleting a folder that is already in Deleted
  Items removes it for good (after a warning). Default folders (Inbox, Sent Items, ...) and
  Exchange's system folders (Sync Issues and its Conflicts / Local Failures / Server Failures)
  can't be renamed, moved or deleted, and Outbox and the Sync Issues folders can't have
  subfolders (not by New folder, nor by moving a folder into them).
- **Moving folders**: drag a folder onto another folder, or onto the mailbox heading for the top
  level (the folder under the pointer is highlighted; hovering opens a closed folder), or
  right-click > Move folder….
- **Moving and copying mail**: drag messages from the list onto a folder (hold Ctrl to copy), or
  Message menu / right-click > Move to folder… (Ctrl+Shift+V) or Copy to folder…. The message
  window has the same Move and Copy buttons and a Message menu. The folder chooser has a
  filter box: type part of a name and press Enter. It starts on the folder you used last.
  - A move shows at once (the next message is selected, counts update) and goes through the
    same change queue as read/flag, so it also works offline and is undone if the server
    refuses it. A copy needs the server, which creates the new message.
  - Moves and copies work within one mailbox; Graph can't move mail between mailboxes.
- Message list served from a local SQLite store, kept current with Graph **delta queries** (one
  cursor per folder). If the first sync fails (offline), the stored mail is still shown.
  - Columns: read dot, replied/forwarded, attachment, importance, From (To in Sent Items, Drafts
    and Outbox), Subject, Received, flag; To and Size can be switched on. Click a header to sort
    by it, drag headers to reorder, right-click the header to show/hide columns. Order, widths
    and sort are remembered.
  - Unread mail is bold. Click the dot (or press **M**) to toggle read, click the flag column
    (or press **S**) to toggle the flag; both work on the whole selection. Message menu and
    right-click menu: Open in new window, Mark as read/unread (Ctrl+U), flag, Mark flag complete.
  - **Conversations** (View > Group by conversation): messages sharing a conversation fold
    into one row showing the participants, the subject without `RE:`/`FW:`, the message count
    and the newest date; it is bold while any message in it is unread. Click its arrow (or
    double-click, or press Enter / Right) to expand. Selecting it shows the newest message;
    marking read or flagging it applies to every message in it.
  - **Date headings** (View > Show date headings): Today, Yesterday, This week, Last week, This
    month, Last month, Older, while sorted by Received. Click a heading to fold it.
  - **Quick filter bar** (Thunderbird style, Ctrl+Shift+K): Unread / Flagged / Attachment
    toggles and a search box; every word typed must appear in the sender, recipients, subject,
    preview or **message text**. Esc clears. The filter resets when you change folder.
    - Mail stored locally is searched at once: metadata by substring, downloaded bodies through
      a full-text index (word prefixes, accent-insensitive).
    - A moment after you stop typing, the server is searched too (Graph `$search`, current
      folder, up to 250 hits). It finds mail whose body was never downloaded and mail older
      than the sync window; those hits are stored and merged into the list. The count shows
      how many came from the server.
    - While searching, the "Show in message list" period is ignored, so old mail is found.
- **Reading pane** (View > Reading pane): Right, Bottom or Off. On the right the list switches to
  compact two-line rows with a Sort button. The pane shows a header (subject, sender, To/Cc,
  date) above the body. Mail shown there is marked read, immediately or after a delay
  (Preferences > Mail), or not at all. With the pane off, selecting doesn't mark anything read.
- **Double-click** (or Enter) opens a message in its own window and marks it read; Esc closes it.
- Read/flag changes show at once and are queued locally, then sent to the server (also when
  offline: they go out on the next successful sync). If the server refuses one, for example in a
  shared mailbox you can only read, it is undone locally.
- Window size, pane layout and splitter positions are remembered.
- **Desktop integration**
  - App icon (the courier penguin, `waddlemail/resources/`) for the window, task manager and
    application menu; a splash screen while starting (Preferences > General to turn it off).
  - **Help > Add to application menu** (or `waddlemail --install-desktop`) puts a launcher and
    the icons in `~/.local/share` for this user; Help > Remove from application menu (or
    `--uninstall-desktop`) takes them out. The launcher remembers the `WADDLEMAIL_CLIENT_ID` /
    `WADDLEMAIL_TENANT` you started with, since a menu launch has no shell environment.
  - **Notifications** for new mail in each signed-in account's Inbox (not shared mailboxes):
    one per message, or a summary when several arrive together; mail older than a day (a
    catch-up after being offline) is left out, as is mail in the Inbox you're looking at.
    Clicking one opens the message. On Linux they go through the freedesktop notification
    service (KDE, GNOME, ...) with the app icon, a sound and, on KDE, the mailbox name; on
    Windows/macOS through the tray icon. Every account's Inbox is checked on each sync.
  - **Tray icon**: the penguin in the panel's colour, with a red dot while there is unread
    Inbox mail; left-click shows or hides the window; menu: Open, Check for new mail, Quit.
    Optionally keep running in the tray when the window is closed (Preferences > General).
  - **Unread badge** on the task manager icon (KDE Plasma, and docks that support the Unity
    LauncherEntry API).
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
calendar views, searching all folders at once, attachments and inline (`cid:`) images,
deleting messages, moving or copying between mailboxes. Conversations only group messages within the current folder (your replies in Sent
Items are not pulled in, as Outlook does).

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

To get WaddleMail in your application menu (Linux), run `waddlemail --install-desktop` once
(or use Help > Add to application menu). GNOME shows tray icons only with the AppIndicator
extension; notifications work without it.

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
waddlemail/text.py         plain text from HTML bodies, for the full-text index
waddlemail/desktop.py      app icon paths; launcher + icon install for the application menu
waddlemail/notify.py       desktop notifications and the task manager unread badge
waddlemail/resources/      app icon (SVG + PNG sizes), tray icons, launcher template
waddlemail/settings.py     Preferences (QSettings) and their defaults
waddlemail/ui/             main window + menus/toolbar, preferences, new mail/event dialogs, safe rendering
waddlemail/ui/folder_pane.py  folder tree: mailbox headings, folder order, icons, unread counts
waddlemail/ui/message_list.py  message model/view (columns, sorting, compact rows), quick filter bar
waddlemail/ui/message_view.py  message header + sandboxed body; the separate message window
waddlemail/ui/folder_picker.py  "choose a folder" dialog for Move/Copy to folder and Move folder
waddlemail/ui/dnd.py       drag-and-drop data format and drag image
waddlemail/ui/splash.py    splash screen
waddlemail/ui/tray.py      system tray icon
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
- Search: `body_fts` is an FTS5 table keyed by the `bodies` rowid. `put_body` adds the text and
  a trigger removes it when a body is evicted, so the index never outgrows the cache. Bodies
  cached before the index existed are indexed once on upgrade.

## Next steps

1. Real sending (`Mail.Send`) through an outbox, and deleting messages through the change
   queue (`pending` table) that read/flag/move already use.
2. Personal contacts (`/me/contacts`) and recipient autocomplete.
3. Events (`Calendars.ReadWrite`), then calendar views.
4. Attachments and `cid:` images; search across all folders; optional background download of
   bodies so local search covers everything offline.
5. "Load older mail" beyond the sync window.
6. Publisher verification of the app registration before sharing with other organizations.
