# OCR Service

Reads Outlook mailboxes, extracts the attached documents (text, Tesseract, AI)
and writes suggestions into Odoo (module `b2b_ocr_documents`: Freight Booking
and Scraped Bills). The service never creates, confirms or links ERP records;
people press the buttons in Odoo.

Everything is set up on the service's own configuration page. There is no
`.env` file.

## Run

    docker compose up -d --build

Open http://localhost:8000. The first time, choose the admin password.

    docker compose logs -f worker                              # watch the worker
    docker compose run --rm worker flask --app app run-once    # one pass by hand
    docker compose down                                        # stop (data is kept)

## Configuration page

- **Settings**: the Odoo connection (URL, database, user, API key; use *Test
  Odoo connection*), the OpenAI key and model, the prompts and limits, and the
  seconds between worker runs.
- **Mailboxes**: the Azure app (tenant ID, client ID, client secret), the
  Odoo company, the filters, and *Read mail received from*. Press **Connect
  Outlook** to sign in once. Add the redirect URI shown on the mailbox page to
  the Azure app first.
- **Vendor Mappings** and **Subject Formats**: used to read the documents.
- **Emails**: every email read, with its state (new, review, failed, done,
  skipped) and a **Retry** button.

## Odoo setup

1. Install `b2b_ocr_documents`.
2. Create an internal user "OCR Service" with access to Freight Booking and
   Scraped Bills, and an API key for it (Preferences > Account Security).

## Data

The `ocr-data` Docker volume (`/data`) holds `ocr.sqlite3` (settings,
mailboxes, emails, run history), `secret.key` (made on first start; it
encrypts the passwords and tokens in the database) and `files/` (the source
documents). Keep the volume: removing it loses the Outlook connection and the
record of which emails were read.

## Tests

    pip install -r requirements-dev.txt
    pytest
