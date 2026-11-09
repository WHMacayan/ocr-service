-- The service's own state. Run at every start: CREATE ... IF NOT EXISTS only.

CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Defaults the worker needs; the Settings page changes them.
INSERT OR IGNORE INTO setting (key, value) VALUES ('poll_seconds', '60');
INSERT OR IGNORE INTO setting (key, value) VALUES ('activity_days', '180');

-- What happened, for people: setting changes, sign-ins, processing, writes to Odoo.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,       -- UTC
    kind TEXT NOT NULL,                               -- settings | security | processing | odoo
    level TEXT NOT NULL DEFAULT 'info',               -- info | warning | error
    actor TEXT NOT NULL,                              -- admin (and address) | worker
    summary TEXT NOT NULL,
    detail TEXT,
    link TEXT                                         -- page in this app the entry is about
);
CREATE INDEX IF NOT EXISTS activity_at ON activity (at);

CREATE TABLE IF NOT EXISTS mailbox (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    purpose TEXT NOT NULL DEFAULT 'freight_booking',  -- freight_booking | bill_matching | customs_entry
                                                       -- | freight_payables (Excel only; see payables_* tables)
    company_id INTEGER,                                -- Odoo res.company id
    booking_number_pattern TEXT,
    tenant_id TEXT,
    client_id TEXT,
    client_secret TEXT,                                -- encrypted
    refresh_token TEXT,                                -- encrypted
    access_token TEXT,                                 -- encrypted
    token_expires_at TEXT,
    auto_sync INTEGER NOT NULL DEFAULT 1,
    sync_interval INTEGER NOT NULL DEFAULT 15,         -- minutes
    sync_requested INTEGER NOT NULL DEFAULT 0,
    batch_size INTEGER NOT NULL DEFAULT 25,
    folder TEXT,
    fetch_window_days INTEGER NOT NULL DEFAULT 14,
    start_from TEXT,                                   -- never read mail received before this
    fetch_resume_from TEXT,
    group_address TEXT,
    sender_filter TEXT,
    subject_keyword TEXT,
    skip_sender_domains TEXT,
    status TEXT NOT NULL DEFAULT 'new',                -- new | running | success | partial | error
    last_sync TEXT,
    last_error TEXT,
    group_miss_note TEXT
);

-- The Odoo databases the service writes to. Every connection that is on gets the same writes.
CREATE TABLE IF NOT EXISTS odoo_connection (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT,
    database TEXT,
    login TEXT,
    api_key TEXT,                                      -- encrypted
    is_production INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,                 -- on: only once complete and its mappings check out
    start_from TEXT,                                   -- mail received before this is never written to it
    last_check_at TEXT,
    last_check_note TEXT
);

CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailbox(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    graph_message_id TEXT,
    internet_message_id TEXT,
    conversation_id TEXT,
    subject TEXT,
    sender TEXT,
    sender_domain TEXT,
    received TEXT,                                     -- UTC, YYYY-MM-DD HH:MM:SS
    body_html TEXT,
    body_text TEXT,                                    -- filled by the pipeline
    skipped_attachments TEXT,
    state TEXT NOT NULL DEFAULT 'new',                 -- new | done | skipped | review | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    email_done INTEGER NOT NULL DEFAULT 0,             -- subject/body values consolidated
    processed_at TEXT,                                 -- last processed, UTC; odoo_change is complete from then
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS message_state ON message (state);
CREATE INDEX IF NOT EXISTS message_conversation ON message (conversation_id);
CREATE INDEX IF NOT EXISTS message_internet_id ON message (internet_message_id);

CREATE TABLE IF NOT EXISTS file (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES message(id),
    filename TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,                                -- on the data volume
    doc_class TEXT,
    extracted TEXT,                                    -- JSON, reused on retry
    state TEXT NOT NULL DEFAULT 'new',                 -- new | done | skipped | waiting | failed
    note TEXT,
    odoo_id INTEGER                                    -- booking or scraped bill it went to
);
CREATE INDEX IF NOT EXISTS file_message ON file (message_id);

-- Where an email and its files stand in each Odoo connection. No row yet: not processed there.
-- message.state and file.state/note/odoo_id summarise these for the lists (see store.refresh_messages).
CREATE TABLE IF NOT EXISTS message_target (
    message_id INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    connection_id INTEGER NOT NULL REFERENCES odoo_connection(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'new',                 -- new | done | review | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    email_done INTEGER NOT NULL DEFAULT 0,
    processed_at TEXT,
    PRIMARY KEY (message_id, connection_id)
);

CREATE TABLE IF NOT EXISTS file_target (
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    connection_id INTEGER NOT NULL REFERENCES odoo_connection(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'new',                 -- new | done | skipped | waiting | failed
    note TEXT,
    odoo_id INTEGER,
    PRIMARY KEY (file_id, connection_id)
);
CREATE INDEX IF NOT EXISTS file_target_odoo ON file_target (connection_id, odoo_id);

-- Every record the service created or changed in Odoo, against the email (and file) it came from.
CREATE TABLE IF NOT EXISTS odoo_change (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,        -- UTC
    action TEXT NOT NULL,                              -- created | updated | deleted
    model TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    label TEXT,                                        -- the record's name in Odoo, e.g. the booking number
    detail TEXT,                                       -- field: value per line
    values_json TEXT,                                  -- the values written, as JSON
    link TEXT                                          -- the record in Odoo
);
CREATE INDEX IF NOT EXISTS odoo_change_message ON odoo_change (message_id);
-- odoo_change.connection_id (added in store.ADDED_COLUMNS): the connection the change was written to.

CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailbox(id),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',            -- running | success | partial | error
    emails_found INTEGER NOT NULL DEFAULT 0,
    emails_skipped INTEGER NOT NULL DEFAULT 0,
    emails_matched INTEGER NOT NULL DEFAULT 0,
    attachments INTEGER NOT NULL DEFAULT 0,
    documents_created INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    log TEXT
);
CREATE INDEX IF NOT EXISTS run_mailbox ON run (mailbox_id);

CREATE TABLE IF NOT EXISTS vendor_mapping (
    id INTEGER PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL,                                -- forwarder | factory
    sender_domain TEXT,
    document_name TEXT,
    partner_id INTEGER,                                -- Odoo res.partner id
    partner_name TEXT,
    freight_mode TEXT,                                 -- ocean | inland | NULL
    freight_product_id INTEGER,                        -- Odoo product.product id
    cost_per_container REAL
);

CREATE TABLE IF NOT EXISTS subject_format (
    id INTEGER PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    sequence INTEGER NOT NULL DEFAULT 10,
    name TEXT NOT NULL,
    sender_domains TEXT,
    subject_pattern TEXT NOT NULL,
    confirms_booking INTEGER NOT NULL DEFAULT 0
);

-- Freight Payables: its own copy of the mail, never read by the Odoo pipeline (which reads message and file).
CREATE TABLE IF NOT EXISTS payables_setting (
    mailbox_id INTEGER PRIMARY KEY REFERENCES mailbox(id) ON DELETE CASCADE,
    invoices_enabled INTEGER NOT NULL DEFAULT 1,       -- invoice flow: Consolidated_Invoice.xlsx
    audit_enabled INTEGER NOT NULL DEFAULT 1           -- audit flow: Freight Payable.xlsx
);

CREATE TABLE IF NOT EXISTS payables_message (
    id INTEGER PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailbox(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    graph_message_id TEXT,
    internet_message_id TEXT,
    conversation_id TEXT,
    subject TEXT,
    sender TEXT,
    sender_domain TEXT,
    received TEXT,                                     -- UTC, YYYY-MM-DD HH:MM:SS
    body_html TEXT,
    unique_body_html TEXT,                             -- only what this reply added
    skipped_attachments TEXT,
    state TEXT NOT NULL DEFAULT 'new',                 -- new | skipped
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS payables_message_conversation ON payables_message (conversation_id);

CREATE TABLE IF NOT EXISTS payables_file (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,                                -- files/payables/<sha[:2]>/<sha> on the data volume
    kind TEXT NOT NULL,                                -- pdf | image | xlsx | xls | zip | eml | msg
    size INTEGER NOT NULL
);

-- Every email a file arrived on: a file re-attached in another thread is linked there too.
CREATE TABLE IF NOT EXISTS payables_message_file (
    message_id INTEGER NOT NULL REFERENCES payables_message(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES payables_file(id) ON DELETE CASCADE,
    filename TEXT,
    is_inline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, file_id)
);
CREATE INDEX IF NOT EXISTS payables_message_file_file ON payables_message_file (file_id);
