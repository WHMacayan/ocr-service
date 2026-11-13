# OCR Service: notes for Claude

Read README.md for what the service does and how to run it. This file holds what the README doesn't:
the rules for working on the UI, and where the redesign stands.

## Rules from the owner

- **UI/UX work must not change logic.** Leave alone how mail is fetched, how documents are read and
  what is written to Odoo (`app/mail/`, `app/flows/`, `app/odoo/`, `app/documents/`, `app/payables/`,
  `app/worker.py`). Pages may *read* more data; they never write.
- **Look: monochrome.** Black, white, hairline borders; colour only for status (green matches, amber
  needs review, red failed). Numbers, dates and file names in the mono font.
  - Not like Odoo (rejected).
  - Not the indigo/slate SaaS look of commit 7718733 (rejected; it is still the base layer of app.css).
- Don't commit or push without being asked.

## Redesign status (branch `feature/new-design`, last updated 2026-11-13)

Designed with the owner step by step (mockups in `.superpowers/brainstorm/`), then built in commit 0d599bd.
Not merged into `development` yet; the owner still has to try it with real data.

### What was agreed

1. **Activity** answers "is everything running, did anything need me?":
   mailbox health → problems of the last 24 h → one card per day with routine service work folded per run.
   It's calm: only the top two parts refresh live. The old table stays as "Full log".
2. **Records over emails.** The owner follows *conversations about a booking or bill*, not single emails.
   Bookings / Scraped bills / Customs each open as one workspace:
   - left: the list of records, with "Needs you" on top;
   - middle: what Odoo holds now, then the whole email thread oldest-first, with what each email changed;
   - right: Attachments → Odoo.
   "All emails" stays reachable for unmatched mail.
3. **Sidebar by job:** Work, Monitor, Setup.

### Where things are

| What | File |
|---|---|
| Sign-out crash fix (schema setup in one locked transaction) | `app/core/store.py` `connect()` |
| Record's thread, files, what each fed (read only) | `app/web/conversation.py` |
| Bookings list pane + workspace; `?view=table` keeps the old table and Group By | `app/web/bookings.py` |
| Same for scraped bills and customs (`PANES`, `_pane`) | `app/web/odoo_records.py` |
| Activity overview (`digest`, `runs` folding); `?view=log` or a search shows the old log | `app/web/activity.py` |
| Templates | `records/_pane.html`, `records/workspace.html`, `records/_conversation.html`, `records/booking.html`, `records/odoo_record.html`, `activity/digest.html`, `layout/base.html` (sidebar) |
| Styles: the monochrome layer replaces the old Odoo layer; components appended at the end | `app/web/static/css/app.css` |

Bump the `?v=` on app.css / app.js in `base.html` after editing them.

### Next steps

- [ ] Owner tries it with real mailboxes and a real Odoo. Not yet seen with real data: only sample data and
      a stand-in Odoo were used. The Scraped bills and Customs workspaces were checked by tests only, never
      looked at.
- [ ] Merge into `development` once approved.
- [ ] Remove `.superpowers/` from the repo (mockups, committed by mistake); it's now in `.gitignore`.
- [ ] Setup pages (Mailboxes, Vendor mappings, Subject formats, Settings) only got the new colours; no UX pass yet.
- [ ] The single-email page and the All emails list got a light pass only.

### Known limits

- "Emails to review / Failed emails" in a record list link to All emails filtered by state and grouped by
  mailbox. They are not limited to that list's purpose.
- Activity days are UTC days (`app/web/activity.py`, marked `shortcut:`).
- Mailbox health uses the mailbox's own `status` / `last_error`. There is no "mark as handled", so problems
  drop off after 24 h.

## Verifying

- **Tests.** Python isn't installed on the owner's Windows PC; run them in the image with the repo mounted:
  `docker compose run --rm -v "$(pwd -W):/srv/ocr-service" web sh -c "pip install -q pytest==8.3.3 openpyxl==3.1.5; python -m pytest -q -p no:cacheprovider"`
  (Git Bash; in PowerShell use `${PWD}`).
- **Seeing pages without real data.** The Chrome extension was not connected, so pages were checked this way:
  1. Render signed-in pages in the container with Flask's test client, using a throwaway
     `OCR_DATA_DIR=/tmp/sample` and a stub for `app.odoo.client.Odoo.from_store`.
  2. Point `/static/` at the local files.
  3. Screenshot with headless Edge (`msedge --headless=new --screenshot`). Narrow shots need at least 600 px.
- **Rebuild the running app:** `docker compose up -d --build`.
