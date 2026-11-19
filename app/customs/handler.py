"""Customs' part of a pipeline pass: each entry summary is read here (Tesseract for scans, no AI) and uploaded to the
customs audit (uploader.py)."""

from app.customs.uploader import CustomsUploader


class CustomsHandler:
    def __init__(self, run):
        self.uploader = CustomsUploader(run.db, run.odoo, run.connection_id, int(run.settings["max_pages"]))

    def take_file(self, message, file):
        """``(state, note, record id)`` for a saved file: customs reads its own files."""
        return self.uploader.take(file, message["received"])
