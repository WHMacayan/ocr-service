"""Freight Payables keeps its own tables; the existing ones stay exactly as they were."""

PAYABLES_TABLES = {"payables_setting", "payables_message", "payables_file", "payables_message_file"}

# The layouts before Freight Payables was added (HEAD ae162f4), plus the record times added after it
# (store.TIMED_TABLES), appended at the end.
EXISTING = {
    "mailbox": ["id", "name", "active", "purpose", "company_id", "booking_number_pattern", "tenant_id",
                "client_id", "client_secret", "refresh_token", "access_token", "token_expires_at", "auto_sync",
                "sync_interval", "sync_requested", "batch_size", "folder", "fetch_window_days", "start_from",
                "fetch_resume_from", "group_address", "sender_filter", "subject_keyword", "skip_sender_domains",
                "status", "last_sync", "last_error", "group_miss_note", "sender_domains", "created_at", "updated_at"],
    "message": ["id", "mailbox_id", "dedupe_key", "graph_message_id", "internet_message_id", "conversation_id",
                "subject", "sender", "sender_domain", "received", "body_html", "body_text", "skipped_attachments",
                "state", "attempts", "note", "email_done", "processed_at", "created_at", "updated_at"],
    "file": ["id", "message_id", "filename", "sha256", "path", "doc_class", "extracted", "state", "note", "odoo_id",
             "created_at", "updated_at"],
}


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_payables_tables_exist(db):
    assert PAYABLES_TABLES <= tables(db)


def test_existing_tables_unchanged(db):
    for table, columns in EXISTING.items():
        assert [row["name"] for row in db.execute(f"PRAGMA table_info({table})")] == columns


def test_payables_setting_defaults_both_outputs_on(db):
    db.execute("INSERT INTO mailbox (name, purpose) VALUES ('Payables', 'freight_payables')")
    db.execute("INSERT INTO payables_setting (mailbox_id) VALUES (1)")
    row = db.execute("SELECT invoices_enabled, audit_enabled FROM payables_setting").fetchone()
    assert (row["invoices_enabled"], row["audit_enabled"]) == (1, 1)
