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

from .odoo import ODOO_DATETIME, utc_now

_logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "openid profile email offline_access User.Read Mail.Read"
NETWORK_RETRIES = 3
RETRY_SECONDS = 5


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
        network_errors = 0
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
                wait = int(resp.headers.get("Retry-After", 10))
                _logger.warning("Outlook busy (%s), retrying in %ss", resp.status_code, wait)
                self.sleep(wait)
                continue
            if resp.status_code != 200:
                raise GraphError(f"Microsoft Graph error {resp.status_code}:\n{resp.text[:1000]}")
            return resp.json()

    def messages_page(self, url, params=None):
        data = self.get(url, params, timeout=30)
        return data.get("value", []), data.get("@odata.nextLink")

    def attachments(self, message_id):
        """Downloadable attachments of a message: not inline, not empty."""
        data = self.get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments",
                        {"$select": "id,name,contentType,size,isInline"}, timeout=120)
        return [att for att in data.get("value", [])
                if not att.get("isInline") and att.get("size", 0) > 0]

    def attachment(self, message_id, attachment_id):
        return self.get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}",
                        timeout=120)
