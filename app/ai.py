"""One structured OpenAI call."""

import json

import requests

URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 120


class AiError(Exception):
    pass


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
    resp = requests.post(URL, json=payload, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 400 and "temperature" in resp.text:
        # Reasoning models accept only their default temperature.
        payload.pop("temperature")
        resp = requests.post(URL, json=payload, headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise AiError(f"OpenAI error ({resp.status_code}): {resp.text[:500]}")
    try:
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        raise AiError(f"OpenAI returned an unreadable answer: {exc}") from exc
