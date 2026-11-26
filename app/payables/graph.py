"""What Freight Payables reads from Outlook beyond the shared Graph client.

The audit needs more than the booking flow: pictures pasted into the email (a screenshot of
an invoice), emails attached as items, and the part of each reply that is new. The shared
client stays as it is; this extends it.
"""

import requests

from app.shared.mail.graph import (BUSY_BUDGET_SECONDS, BUSY_RETRIES, GRAPH_BASE, NETWORK_RETRIES, RETRY_SECONDS,
                                   Graph, GraphError, retry_after)


class PayablesGraph(Graph):
    def all_attachments(self, message_id):
        """Every non-empty attachment of a message, inline pictures included."""
        return [att for att in self.attachment_list(message_id) if att.get("size", 0) > 0]

    def attachment_bytes(self, message_id, attachment_id):
        """The raw content of an attachment; an attached email comes back as MIME (.eml).
        Retried like Graph.get: as long as Outlook asks when busy, and on passing network errors."""
        url = f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}/$value"
        network_errors = busy_waits = waited = 0
        while True:
            try:
                resp = self.http.get(url, headers=self._headers(), timeout=120)
            except requests.exceptions.RequestException as exc:
                network_errors += 1
                if network_errors > NETWORK_RETRIES:
                    raise GraphError(f"Network error calling Microsoft Graph: {exc}") from exc
                self.sleep(RETRY_SECONDS)
                continue
            if resp.status_code in (429, 503, 504):
                wait = retry_after(resp)
                busy_waits += 1
                waited += wait
                if busy_waits > BUSY_RETRIES or waited > BUSY_BUDGET_SECONDS:
                    raise GraphError(f"Outlook stayed busy ({resp.status_code}): the attachment was not downloaded.")
                self.sleep(wait)
                continue
            if resp.status_code != 200:
                raise GraphError(f"Microsoft Graph error {resp.status_code}:\n{resp.text[:1000]}")
            return resp.content

    def unique_body(self, message_id):
        """Only what this reply added, without the quoted history (HTML), or None."""
        data = self.get(f"{GRAPH_BASE}/me/messages/{message_id}", {"$select": "uniqueBody"})
        return (data.get("uniqueBody") or {}).get("content")
