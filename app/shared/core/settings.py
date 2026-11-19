"""The settings on the Settings page. Every setting lives in SQLite, none in the environment.
The Odoo connections have their own table (store.connections)."""

from collections import namedtuple

# help: a short hint under the field. tip: what the field is for, behind its info icon.
Setting = namedtuple("Setting", "key label kind help group tip", defaults=("text", "", "", ""))

SETTING_GROUPS = {
    "AI": "The model that reads the documents, and what it is asked.",
    "Processing": "How much is read per file and per run, and when an email is given up.",
    "Worker": "How often the mailboxes are checked, and how long the activity log is kept.",
}

SETTINGS = [
    Setting("openai_api_key", "OpenAI API key", "secret", "", "AI",
            "Lets the service use OpenAI to read the attached documents. Without it, emails are still saved "
            "but their files are not read."),
    Setting("openai_model", "Model", "text", "", "AI",
            "Which OpenAI model reads the documents. gpt-4o-mini is fast and cheap; larger models read "
            "difficult documents better but cost more."),
    Setting("freight_prompt", "Freight booking prompt", "textarea", "", "AI",
            "The instructions the AI follows when it reads a shipping document: which document types exist "
            "and which facts to take from them."),
    Setting("invoice_prompt", "Invoice prompt", "textarea", "", "AI",
            "The instructions the AI follows when it reads a supplier invoice for bill matching."),
    Setting("max_chars", "Characters sent to the AI per file", "int", "", "Processing",
            "Long documents are cut to this many characters before they go to the AI. Keeps the cost of very "
            "long files in check."),
    Setting("max_pages", "Pages read per file", "int", "", "Processing",
            "Only this many pages of each file are read. Most shipping documents carry what is needed on the "
            "first pages."),
    Setting("ai_parallel", "Files read by the AI at once", "int", "", "Processing",
            "How many of a run's new files the AI reads at the same time, before anything is written to Odoo. "
            "More is faster on a backlog; OpenAI's rate limit for your key is the ceiling. 1 reads them one by one."),
    Setting("documents_per_run", "Files processed per run", "int", "", "Processing",
            "How many files are read in one pass of the worker. Spreads the work and the AI cost over time."),
    Setting("max_retries", "Attempts before an email is marked failed", "int", "", "Processing",
            "How many times an email is tried again after an error, such as Odoo being unreachable, before it "
            "is marked Failed for a person to look at."),
    Setting("poll_seconds", "Seconds between checks", "int", "", "Worker",
            "How long the worker waits between checks for mailboxes that are due and files to read."),
    Setting("activity_days", "Keep the activity log (days)", "int", "", "Worker",
            "Activity log entries older than this are removed."),
]

SECRET_SETTINGS = {s.key for s in SETTINGS if s.kind == "secret"}
