"""The jobs the service does. Each job lives in its own folder (app/freight_booking, app/scraped_bills, app/customs,
app/payables) and describes itself with a Job in that folder's __init__.py; app/jobs.py lists them.

The shared parts (the worker, the pipeline, the pages, the commands) ask the jobs what to do and never name one, so
a job is changed, or removed, in its own folder only.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class MenuItem:
    """An entry of the top menu: its label, the page it opens, and the URL prefix that marks it current."""
    label: str
    endpoint: str
    prefix: str


@dataclass(frozen=True)
class TransferPart:
    """A job's part of the Import / Export page: a file kind it takes (uploaded, previewed, then applied), the template
    of its section, and what that section shows."""
    kind: str
    pending: str                                    # the uploaded file's name in the data folder until Apply
    template: str
    preview: Callable                               # preview(path) -> {"summary": ...} (raises when unreadable)
    apply: Callable                                 # apply(db, pending path): use the file
    context: Callable = lambda db: {}               # context(db) -> what its section shows


@dataclass(frozen=True)
class Job:
    purpose: str                                    # the mailbox purpose it handles ("freight_booking")
    label: str                                      # as the Mailboxes form lists the purpose
    reading: object = None                          # what the AI reads its attachments for (documents/extract.Reading)
    handler: Optional[Callable] = None              # handler(run): takes this job's emails in one pipeline pass
    own_worker: bool = False                        # read by its own loop, never by the shared worker
    every_pass: Optional[Callable] = None           # every_pass(db): after each worker pass (its own timing inside)
    records: dict = field(default_factory=dict)     # the Odoo models it writes, as the Emails page names them
    menu: tuple = ()                                # MenuItem per entry of the top menu
    commands: Optional[Callable] = None             # commands(cli): adds its command-line commands
    pages: Optional[str] = None                     # the module whose import registers its pages
    transfer: tuple = ()                            # TransferPart per part of the Import / Export page
    datasets: Optional[str] = None                  # the module whose DATASETS list its data sets (data/__init__.py)
    skips_senders: bool = False                     # its mailboxes' Skip Senders keep those emails unread
    unprefixed_keys: bool = False                   # its stored emails and files carry no purpose in their keys (the
                                                    # first job's, from before the others: changing them refetches all)


def all_jobs():
    from app.jobs import JOBS
    return JOBS


def job_for(purpose):
    """The Job handling mailboxes of ``purpose``; None for an unknown purpose."""
    return next((job for job in all_jobs() if job.purpose == purpose), None)
