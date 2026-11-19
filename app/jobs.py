"""The jobs this service does, each in its own folder, in the order the Mailboxes form lists their purposes.

To add a job: make its folder with a Job in its __init__.py (see app/shared/jobs.py) and list it here. The shared
worker, pipeline, pages and commands take it from there.
"""

from app import customs, freight_booking, payables, scraped_bills

JOBS = (freight_booking.JOB, scraped_bills.JOB, customs.JOB, payables.JOB)
