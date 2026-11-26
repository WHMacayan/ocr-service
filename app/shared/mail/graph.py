"""Microsoft Graph access for one Outlook mailbox.

Delegated sign-in: the Settings page connects the mailbox once (authorization
code flow) and the worker keeps it signed in with the refresh token. Microsoft
rotates the refresh token, so every new token is saved straight away. Retries
follow the freight payables scanner: wait as long as Outlook asks when it is
busy, retry passing network errors.
"""

import logging
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from app.shared.odoo.client import ODOO_DATETIME, utc_now

_logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "openid profile email offline_access User.Read Mail.Read"
NETWORK_RETRIES = 3
RETRY_SECONDS = 5
# While Outlook says it is busy (429, 503, 504) a call waits as long as asked, but not forever: a mailbox that stays
# throttled gives up (the read shows the error and is tried again at its next turn) instead of holding the worker
# for good (review F07).
BUSY_RETRIES = 12
BUSY_BUDGET_SECONDS = 600
MAX_WAIT_SECONDS = 120
DEFAULT_WAIT_SECONDS = 10
# Attachment pages followed for one email, under a ceiling against an endless nextLink (review F06).
MAX_ATTACHMENT_PAGES = 50


def retry_after(resp):
    """Seconds Outlook asks to wait, from the Retry-After header: a whole number of seconds, kept within 1 and
    MAX_WAIT_SECONDS. A missing, negative or unreadable value (a date, text) is the default."""
    try:
        seconds = int(float(str(resp.headers.get("Retry-After", "")).strip()))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_WAIT_SECONDS
    return min(max(seconds, 1), MAX_WAIT_SECONDS) if seconds > 0 else DEFAULT_WAIT_SECONDS


class GraphError(Exception):
    pass


def _login_url(mailbox, endpoint):
    return f"https://login.microsoftonline.com/{mailbox['tenant_id']}/oauth2/v2.0/{endpoint}"


def authorize_url(mailbox, redirect_uri, state):
    """Microsoft's sign-in page for connecting the mailbox."""
    return _login_url(mailbox, "authorize") + "?" + urlencode({
        "client_id": mailbox["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "prompt": "select_account",
    })


def _token_values(resp, old_refresh_token=None):
    if resp.status_code != 200:
        raise GraphError(f"Token request failed:\n{resp.text}")
    data = resp.json()
    expires_at = utc_now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", old_refresh_token),
        "token_expires_at": expires_at.strftime(ODOO_DATETIME),
    }


def exchange_code(mailbox, code, redirect_uri, http=requests):
    """Tokens for the code Microsoft sent back after sign-in."""
    resp = http.post(_login_url(mailbox, "token"), data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": mailbox["client_id"],
        "client_secret": mailbox["client_secret"],
        "scope": SCOPE,
    }, timeout=30)
    return _token_values(resp)


class Graph:
    def __init__(self, mailbox, save_tokens, http=requests, sleep=time.sleep):
        self.mailbox = mailbox
        self.save_tokens = save_tokens
        self.http = http
        self.sleep = sleep

    def _token_expired(self):
        expires = self.mailbox.get("token_expires_at")
        if not expires or not self.mailbox.get("access_token"):
            return True
        return datetime.strptime(expires, ODOO_DATETIME) <= utc_now()

    def _refresh_token(self):
        if not self.mailbox.get("refresh_token"):
            raise GraphError("The mailbox is not connected: press Connect Outlook on the mailbox.")
        resp = self.http.post(_login_url(self.mailbox, "token"), data={
            "grant_type": "refresh_token",
            "refresh_token": self.mailbox["refresh_token"],
            "client_id": self.mailbox["client_id"],
            "client_secret": self.mailbox["client_secret"],
            "scope": SCOPE,
        }, timeout=30)
        values = _token_values(resp, self.mailbox["refresh_token"])
        self.save_tokens(values)
        self.mailbox.update(values)

    def _headers(self):
        if self._token_expired():
            self._refresh_token()
        return {
            "Authorization": f"Bearer {self.mailbox['access_token']}",
            # Immutable ids survive a message moving between folders.
            "Prefer": 'IdType="ImmutableId"',
        }

    def get(self, url, params=None, timeout=60):
        """One GET returning the JSON body, retried while Outlook is busy."""
        network_errors = busy_waits = waited = 0
        while True:
            try:
                resp = self.http.get(url, headers=self._headers(), params=params, timeout=timeout)
            except requests.exceptions.RequestException as exc:
                network_errors += 1
                if network_errors > NETWORK_RETRIES:
                    raise GraphError(f"Network error calling Microsoft Graph: {exc}") from exc
                self.sleep(RETRY_SECONDS)
                continue
            if resp.status_code in (500, 502) and network_errors < NETWORK_RETRIES:
                network_errors += 1
                self.sleep(RETRY_SECONDS)
                continue
            if resp.status_code in (429, 503, 504):
                wait = retry_after(resp)
                busy_waits += 1
                waited += wait
                if busy_waits > BUSY_RETRIES or waited > BUSY_BUDGET_SECONDS:
                    raise GraphError(f"Outlook stayed busy ({resp.status_code}) for {waited - wait} seconds; "
                                     "the mailbox is read again at its next turn.")
                _logger.warning("Outlook busy (%s), retrying in %ss", resp.status_code, wait)
                self.sleep(wait)
                continue
            if resp.status_code != 200:
                raise GraphError(f"Microsoft Graph error {resp.status_code}:\n{resp.text[:1000]}")
            return resp.json()

    def messages_page(self, url, params=None):
        data = self.get(url, params, timeout=30)
        return data.get("value", []), data.get("@odata.nextLink")

    def attachment_list(self, message_id):
        """Every attachment's details, following the pages Outlook splits a long list into. A list that is still
        not complete after MAX_ATTACHMENT_PAGES is an error, never a shorter list taken for the whole."""
        url, params, found = f"{GRAPH_BASE}/me/messages/{message_id}/attachments", {
            "$select": "id,name,contentType,size,isInline"}, []
        for _page in range(MAX_ATTACHMENT_PAGES):
            data = self.get(url, params, timeout=120)
            found += data.get("value", [])
            url = data.get("@odata.nextLink")
            if not url:
                return found
            params = None  # the next link already carries them
        raise GraphError(f"The attachment list of message {message_id} is longer than {MAX_ATTACHMENT_PAGES} pages.")

    def attachments(self, message_id):
        """Downloadable attachments of a message: not inline, not empty."""
        return [att for att in self.attachment_list(message_id)
                if not att.get("isInline") and att.get("size", 0) > 0]

    def attachment(self, message_id, attachment_id):
        return self.get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}",
                        timeout=120)

    def attached_email(self, message_id, attachment_id):
        """The raw MIME of an email attached to an email (an item attachment), with its own files inside."""
        url = f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}/$value"
        for _attempt in range(NETWORK_RETRIES + 1):
            resp = self.http.get(url, headers=self._headers(), timeout=120)
            if resp.status_code in (429, 503, 504):
                self.sleep(retry_after(resp))
                continue
            if resp.status_code != 200:
                raise GraphError(f"Microsoft Graph error {resp.status_code}:\n{resp.text[:1000]}")
            return resp.content
        raise GraphError("Outlook stayed busy: the attached email was not downloaded")
