"""The settings on the Settings page. Every setting lives in SQLite, none in the environment."""

from collections import namedtuple

# help: a short hint under the field. tip: what the field is for, behind its info icon.
Setting = namedtuple("Setting", "key label kind help group tip", defaults=("text", "", "", ""))

SETTING_GROUPS = {
    "Odoo": "Where the suggestions are written. Use the login of the Odoo user and an API key made by that user.",
    "AI": "The model that reads the documents, and what it is asked.",
    "Processing": "How much is read per file and per run, and when an email is given up.",
    "Worker": "How often the mailboxes are checked, and how long the activity log is kept.",
}

SETTINGS = [
    Setting("odoo_url", "Odoo address", "text", "For example http://katana.local", "Odoo",
            "The web address of your Odoo. The service sends booking and bill suggestions there."),
    Setting("odoo_db", "Database", "text", "", "Odoo",
            "The Odoo database the suggestions go into, for example 1025."),
    Setting("odoo_user", "Login", "text", "The user's login in Odoo (Settings > Users), not the display name", "Odoo",
            "The Odoo user the service signs in as. Everything it writes in Odoo is recorded under this user, "
            "and it can only do what this user is allowed to do."),
    Setting("odoo_api_key", "API key", "secret", "Odoo: Preferences > Account Security > New API Key", "Odoo",
            "Lets the service sign in to Odoo without a password. It must be made by the user above. "
            "Delete the key in Odoo to cut the service off at once."),
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
