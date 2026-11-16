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
- **Deleting mail**: Delete (Del; toolbar, Message menu, right-click, and the message window)
  moves messages to Deleted Items, as Outlook does, with no question; the next message is
  selected. In Deleted Items (or a folder in it), or with **Delete permanently** (Shift+Del),
  they're deleted for good after asking; Outlook's "Recover deleted items" can still bring them
  back for a while. Both go through the change queue, so they show at once and work offline;
  one the server refuses (a read-only shared mailbox) comes back.
  - **Empty Deleted Items**: right-click Deleted Items, or File > Empty Deleted Items…. After
    asking, it deletes every message in it on the server (not only those kept here), 20 per
    request with progress in the status bar, and the folders in it.
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
- **Received attachments** (and your own, in Sent Items) are listed under the message header,
  in the reading pane and the message window. Double-click one to download it to a private
  temporary folder and open it with the desktop's app; right-click to save it (Save as), the
  selected ones or all of them (into a folder; names already there get " (2)"). An attached
  message saves as `.eml`. Pictures in the body (inline) aren't listed.
- **Pictures in the body** (inline `cid:` attachments, such as logos in signatures and pasted
  screenshots) show in the reader even with remote images off: they're fetched once, stored
  beside the cached body (counting toward the cache size) and work offline.
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
- **Writing mail** (New Mail, Reply Ctrl+R, Reply all Ctrl+Shift+R, Forward Ctrl+F, in the
  main window and in a message's own window) opens a compose window:
  - From (with several accounts), To, Cc and Bcc (shown on demand; a reply all shows Cc when
    it has some), Subject, all completing names like the contacts.
  - A WYSIWYG body on a white page: font, size, bold/italic/underline/strikethrough, colour,
    highlight, bullets and numbering, indent, alignment, link (Ctrl+K), clear formatting.
    **Plain text** switches the message to text only (formatting is dropped).
  - **Pictures in the body**: Insert picture on the formatting bar, paste one (a screenshot,
    say), or drop picture files on the body (other files dropped there are attached).
    Pictures wider than 640 px go in at that width; up to 10 MB each. Click a picture to
    select it (Delete removes it), then drag a corner handle to resize it, keeping its
    proportions (one undo puts a whole drag back); right-click it for **Picture size**:
    original, three quarters, half, quarter, fit the page, or a width you type. They're sent as inline
    attachments the body links to, as Outlook does; a quoted message's pictures go along
    with a reply or forward. Signatures can have pictures too.
  - Replies and forwards: RE:/FW: subjects (no stacking), reply all leaves you out, and the
    body starts with the cursor at the top, then your signature, then a line and Outlook's
    From / Sent / To / Cc / Subject block over the original.
  - **Signatures**, one per account (Preferences > Signatures, rich text), added
    automatically to new messages and/or replies and forwards; **Insert signature** adds it
    just above the quoted text (or at the end of a new message).
  - **Attachments**: Attach (Ctrl+Shift+A) or drop files anywhere on the window (dropping on
    the body attaches rather than pasting the path); the strip under the subject shows each
    with its size, Delete or × takes one out, and the total is shown. A forward lists the
    original's attachments so you can leave some out. Files up to 150 MB each (Graph's
    limit: under 3 MB in one request, larger ones through an upload session in pieces under
    4 MB); a total over 35 MB asks first, as most servers refuse bigger messages. Mail with
    attachments goes out as a draft (create, attach, send); a saved draft keeps them, and
    later saves or the send only upload or remove the differences.
  - Send (Ctrl+Enter). New mail without attachments is sent directly; replies and forwards go through Exchange's
    reply/forward draft, so they stay in the conversation and a forward keeps the original's
    attachments. Save draft (Ctrl+S) keeps it in Drafts; closing an unsent message asks.
  - **Outbox**: when the server can't be reached (offline, timing out, busy), the message
    waits in the Outbox, with copies of its files, and the window closes. The account's
    Outbox folder counts and lists it (the reader shows it as it will go out); it goes out
    after the next successful sync, or on Send/Receive (F5). Each try carries on where the
    last stopped (same draft, files not uploaded twice). A message the server refuses (an
    address it won't take) stays there, you're told, and it isn't retried by itself: open it
    (double-click) to fix it, which takes it out of the Outbox, or delete it. A refusal while
    you're sending is shown straight away, with the window kept open.
  - Preferences > Compose: write new mail in HTML or plain text; default font, size and
    colour (a font that isn't installed, such as Calibri on Linux, is shown as its stand-in
    but mail keeps naming it).
- **Calendar** (the rail, or Ctrl+2):
  - Day, Work week, Week and Month views; Today and back/forward; a mini month and your
    calendars (tick to show/hide) on the left; the selected event's details on the right.
  - Events are coloured by their first category (your Outlook categories and colours), else
    by their calendar. The bar on an event's left shows its Show as, as Outlook draws it:
    solid Busy, hatched Tentative, purple Out of office, dotted Working elsewhere, outline
    Free. All-day events run across the top; a red line marks the time now.
  - Drag across empty time to pick a period, then double-click or Enter (or New event) to
    make an event there; double-click the all-day strip or a month cell for an all-day one.
    Double-click an event to edit it; Delete removes it.
  - Event editor: title, attendees (required and optional, completed like To), location,
    start/end or All day, **Teams meeting** (Graph creates it and its join link; offered when
    your account can), **Show as** (Free, Working elsewhere, Tentative, Busy, Out of office),
    categories, reminder, calendar, private, **attachments** (an existing event's are
    listed, so you can add or remove), and **notes** with the same formatting bar as mail:
    pictures can be inserted, pasted, dropped and resized there too (saved as inline
    attachments, shown again when the event is opened). With attendees it is a meeting: Save
    becomes Send and Exchange sends the invitations; changes send updates. Attachments and
    pictures go on first, so the invitation or update carries them: a new meeting is created,
    given its files, then its attendees are invited.
  - **Scheduling**: everyone's free/busy for the day (getSchedule) in a grid next to the
    meeting; click a time to move the meeting there, or **Find next free time** for the
    first slot (weekdays 8:00-18:00) all attendees are free, optionally avoiding tentative.
  - Invitations show Accept / Tentative / Decline (the organizer is told) and **Join Teams**.
    Deleting a meeting you organised cancels it for the attendees.
  - **Repeating events**: Repeat offers Outlook's choices for the event's date (daily, every
    weekday, weekly on that day, monthly on that date or on its Nth weekday, yearly) and
    **Custom…**: every N days / weeks (choose the weekdays) / months / years, ending never,
    on a date or after a number of times. Opening or deleting a repeating event asks "just
    this one, or the whole series?"; the series editor can change or stop the pattern.
  - Times are sent in your own time zone (by its Windows name, e.g. Central Standard Time),
    so a weekly 9:00 meeting stays at 9:00 across daylight saving changes.
  - **Reminders**: a window that stays on top lists what is due (soonest first, "in 10
    min"), with Join Teams, Open item, Dismiss, Dismiss all and Snooze (5 minutes to a day),
    plus a desktop notification (alarm sound, stays until dealt with). Dismiss and snooze are
    sent to Exchange too, so Outlook elsewhere agrees. The coming week is kept cached, so
    reminders also work offline. Preferences > General can turn them off.
  - The period shown is fetched when you go to it, and refreshed every 5 minutes.
- **Contacts** (the rail, or Ctrl+3; Ctrl+1 is Mail):
  - Sources: all personal contacts and each contact folder, **contact groups**, the
    **organization directory**, and **recent recipients** (people you send to, from Sent
    Items, and people whose mail you replied to; most used first).
  - Search by name, address, company, title or department; a details card with avatar,
    addresses (click to write), phones and notes; "Email everyone in this group".
  - Personal contacts can be created, edited and deleted: names, up to three addresses,
    company/title/department, phones, work and home postal addresses, nickname, birthday,
    website, notes and a **photo** (any image; scaled down and saved as JPEG). Directory
    people and recent recipients can be added to your contacts.
  - **Contact groups** can be created, renamed, annotated and deleted, and members added or
    removed in the group editor (typing completes names like the To field). "Add to group"
    adds whoever is selected in any list, or starts a new group with them. Your contacts join
    a group as contacts, directory people as mailboxes, other addresses as plain addresses (if
    the server refuses a typed member, everyone in that batch is added as plain addresses).
  - **Photos** of your contacts and directory people show in the details card. Each is
    downloaded the first time that person is shown and then kept; people without one are
    checked again after a week.
  - Refreshed at start-up, hourly and with Refresh. A source that can't be loaded (no
    directory for personal Microsoft accounts; contact groups if the preview API is missing)
    is named in a note instead of breaking the rest.
  - **Autocomplete** in To and Cc of a new message: any part of a name, address or company;
    people you write to most come first, then contacts, groups (choosing one inserts all its
    members) and the directory.
  - Contact groups come from Microsoft Graph's **beta** `distributionLists` API (added in
    2026; there was no Graph API for them before, and EWS is being retired), so they may
    change or break without notice.
- **Status indicator** (bottom right): a dot for the connection (green: the server answered the
  last request; red: it couldn't be reached; amber: connecting or sign-in needed), what is
  running now with a spinner ("Syncing with server…", "Sending changes…", "Searching
  server…", ...), and otherwise "Up to date · 14:32" or "Offline · 3 changes waiting". Hover
  for details; click to sync now. The connection state comes from the requests themselves
  (`graph.status_hook`), not from a separate check.
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
- Toolbar: **New** follows the page you're on (New Mail, New Event, New Contact); its arrow
  offers Mail message (Ctrl+N), Event (Ctrl+Shift+E), Contact (Ctrl+Shift+C) and Folder from
  anywhere.
  The toolbar also has **Send/Receive**, **Cut / Copy /
  Paste**, and Reply / Reply all / Forward / Delete.
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
  - Keep mail locally for N days/months/all (sync window). Making it longer downloads the
    extra mail on the next sync. Older mail stays on the server: when a folder has more there
    than here, a bar under the message list offers **Download older mail** (500 at a time).
    Mail fetched that way (and server-search hits) isn't kept up to date by sync, but isn't
    thrown away by it either.
  - Show in message list: last N days/months/all. Mail is downloaded for the longer of the
    two periods (headers only; message text comes when you open a message), so "all" really
    shows everything; a shorter period only hides older mail (nothing is deleted). Folders
    have no size cap in the list.
  - Data location (applies after restart; existing data is not moved)
  - Body cache size limit, discard cached bodies not opened for N days/months, Clear cache button

## Not yet

Searching all folders at once, emptying Junk
Email, moving or copying between mailboxes. Conversations only group messages within the current
folder (your replies in Sent Items are not pulled in, as Outlook does).

## Register the app (one time)

In the Entra admin center (entra.microsoft.com) > App registrations > New registration:

1. Supported account types: *Accounts in any organizational directory and personal Microsoft
   accounts* (or single tenant if it is only for your organization).
2. Redirect URI: platform **Mobile and desktop applications**, value `http://localhost`.
3. Under **Authentication**, set "Allow public client flows" to **Yes**.
4. **API permissions** > Microsoft Graph > Delegated: `Mail.ReadWrite` (mark read, flag, move),
   `Mail.ReadWrite.Shared` (shared mailboxes and folders), `Mail.Send` (sending), `Contacts.ReadWrite` (contacts and
   contact groups), `User.ReadBasic.All` (the organization directory), `Calendars.ReadWrite`
   (calendar, meetings, free/busy) and `MailboxSettings.Read` (your categories and colours);
   none needs admin consent. Existing users are asked to consent once more after upgrading. Sending and events will
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
waddlemail/contacts.py     contacts/directory/contact group sync, To/Cc suggestions
waddlemail/calendars.py    calendars, events, categories, free/busy, layout and free-time search
waddlemail/compose.py      replies: recipients, subjects, quoted header; the message to send
waddlemail/outbox.py       sending (directly or through a draft), and the Outbox
waddlemail/attachments.py  attachments being written (reconcile an item's with the list); safe names to save
waddlemail/inline.py       pictures in bodies: cid: links to data: URIs and back
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
waddlemail/ui/status_indicator.py  connection dot and activity in the status bar
waddlemail/ui/calendar_view.py  the Calendar page: day/week/month views and event details
waddlemail/ui/event_editor.py  the event/meeting editor and the scheduling assistant
waddlemail/ui/recurrence_dialog.py  custom repeat pattern
waddlemail/ui/reminders.py  reminders window and the service that raises it
waddlemail/ui/compose_window.py  the compose window (new, reply, forward)
waddlemail/ui/rich_editor.py  WYSIWYG editor with formatting bar (mail and signatures)
waddlemail/ui/attachment_bar.py  the attachments strip (compose window, event editor)
waddlemail/ui/contacts_view.py  the Contacts page
waddlemail/ui/contact_dialogs.py  contact editor (with photo), contact group editor, avatars
waddlemail/ui/recipients.py  To/Cc field with name completion
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
- Replied/forwarded state is the MAPI property `PidTagLastVerbExecuted` (0x1081; 102 replied,
  103 replied to all, 104 forwarded) and size is `PidTagMessageSize` (0x0E08). Exchange's delta
  query accepts `$expand` for extended properties but returns nothing for it, and the filter in
  that `$expand` takes only `id eq` (one property per request). So: the replied/forwarded marks
  are back-filled once per folder (and after a full re-download) with Graph's documented
  property filter, one small query per kind of mark that returns only the marked messages;
  after that, messages a sync reports as changed (replying in Outlook changes the original)
  are looked up with a single-message `$expand`, 20 per `$batch`. Sizes come from the folder's
  message list, ids and sizes only, 500 a page, while any are missing. Replies and forwards
  sent from WaddleMail mark the original at once. None of this can fail a sync; if Exchange
  refuses it, the status bar says so.
- A queued read/flag change wins over an older value arriving in a delta page, so the list
  never flickers back to unread while the change is on its way.
- Search: `body_fts` is an FTS5 table keyed by the `bodies` rowid. `put_body` adds the text and
  a trigger removes it when a body is evicted, so the index never outgrows the cache. Bodies
  cached before the index existed are indexed once on upgrade.

## Next steps

1. Removing a contact's photo (Graph has no call for it); photos in lists and autocomplete.
2. Calendar: other people's shared calendars, room booking; reminders dismissed on another
   device aren't noticed here until they'd fire (Graph's reminderView could tell).
3. Search across all folders; optional background download of
   bodies so local search covers everything offline.
4. Publisher verification of the app registration before sharing with other organizations.
