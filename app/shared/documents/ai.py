"""One structured OpenAI call."""

import json
import time

import requests

URL = "https://api.openai.com/v1/chat/completions"
# A statement of account lists hundreds of invoices: its answer can take over two minutes (email 939, Nov 2026).
TIMEOUT = 300


class AiError(Exception):
    pass


class AnswerTooLong(AiError):
    """The answer was cut off at the model's output limit: a statement listing hundreds of invoices
    (email 939, Nov 2026)."""


# A dropped connection or a busy OpenAI is tried again after a short wait (17 Nov 2026: a few minutes without
# DNS made ten readings fail in the same second and used up email attempts).
RETRY_WAITS = (5, 20, 60)
RETRY_STATUS = (429, 500, 502, 503, 504)


def _post(payload, headers, sleep=time.sleep):
    for wait in (*RETRY_WAITS, None):
        try:
            resp = requests.post(URL, json=payload, headers=headers, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout):
            if wait is None:
                raise
        else:
            if resp.status_code not in RETRY_STATUS or wait is None:
                return resp
        sleep(wait)


def extract(api_key, model, prompt, text, schema):
    """The model's answer to ``prompt`` for ``text``, shaped by the JSON ``schema``."""
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "extraction", "strict": True, "schema": schema}},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = _post(payload, headers)
    if resp.status_code == 400 and "temperature" in resp.text:
        # Reasoning models accept only their default temperature.
        payload.pop("temperature")
        resp = _post(payload, headers)
    if resp.status_code != 200:
        raise AiError(f"OpenAI error ({resp.status_code}): {resp.text[:500]}")
    try:
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise AnswerTooLong("The answer ran past the model's output limit and was cut off.")
        return json.loads(choice["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        raise AiError(f"OpenAI returned an unreadable answer: {exc}") from exc
