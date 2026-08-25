"""One JSON answer from an OpenRouter model chain, with the retries it needs.

Both places this project calls a model want the same things: a chain that keeps
going when one member is missing or rate-limited, a schema the answer has to
match, and a hard deadline so a slow provider cannot outlive the workflow
waiting on it. That machinery was written once for resume tailoring and is
shared from here rather than copied.
"""

from __future__ import annotations

import json
import os
import time

import requests


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

CALL_TIMEOUT = 60
MIN_CALL_SECONDS = 15
HTTP_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 6)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

HEADERS = {
    "HTTP-Referer": "https://github.com/abdullah7041/job-search-digest",
    "X-Title": "job-search-digest",
}


def configured_chain(model_var: str, fallback_var: str,
                     default_model: str, default_fallbacks: str) -> tuple[str, list[str]]:
    """Resolve a model chain from repository variables, or fall back.

    A GitHub ``${{ vars.X }}`` that is unset renders as an empty string, so the
    two-argument form of os.environ.get would return "" instead of the default.
    """
    model = os.environ.get(model_var) or default_model
    raw = os.environ.get(fallback_var) or default_fallbacks
    return model, [name.strip() for name in raw.split(",") if name.strip()]


def _retry_after_seconds(response) -> float | None:
    """The wait the server asked for, when it named one we can act on."""
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return None


def _content_from_payload(payload: dict, model: str) -> dict:
    """Read one JSON answer out of an OpenRouter response body.

    Every failure names the model, because that string is what reaches the
    candidate in Discord and it is the only clue they get about which member of
    the chain answered.
    """
    error = payload.get("error")
    if error:
        # OpenRouter reports some upstream failures as a 200 with an error body.
        raise ValueError(f"OpenRouter error ({model}): {str(error)[:200]}")
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError(f"OpenRouter returned no choices ({model}).")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        reason = choices[0].get("finish_reason") or "unknown"
        raise ValueError(
            f"OpenRouter returned an empty answer ({model}, finish_reason={reason})."
        )
    return json.loads(content)


def chat_json(prompt: str | list, schema: dict, schema_name: str, api_key: str,
              model: str, fallbacks: list[str], deadline: float,
              *, reasoning_effort: str = "low", seed: int = 7,
              provider: dict | None = None) -> dict:
    """One schema-checked answer from the model chain, or raise for the caller.

    ``prompt`` is either the text of one user message, or the list of content
    parts that message is made of — ``[{"type": "text", ...}, {"type":
    "image_url", ...}]`` — which is how an image reaches a multimodal member of
    the chain. Everything else is identical either way, including the schema,
    the deadline and the no-retention routing policy.

    OpenRouter walks ``models`` itself when a member is missing, rate-limited or
    erroring, which a single-model call could not do: a 404 on the model name or
    a 429 on the quota ended the whole attempt. The loop below covers the
    remaining case, where the entire chain is briefly unavailable, and never
    runs past the caller's deadline.

    Note what is *not* sent: temperature. Callers pass ``require_parameters`` so
    the request only reaches providers that honour the JSON schema, and that
    same flag drops any provider missing a parameter the request names. Google
    serves Gemini Flash on Vertex endpoints that take seed but not temperature,
    and those are the discounted ones -- naming temperature routes around them
    for a knob that seed already covers here.
    """
    body = {
        "model": model,
        "models": [model, *fallbacks],
        "messages": [{"role": "user", "content": prompt}],
        "seed": seed,
        "reasoning": {"effort": reasoning_effort},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    if provider:
        body["provider"] = provider
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **HEADERS,
    }

    last_error = ""
    for attempt in range(HTTP_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining < MIN_CALL_SECONDS:
            raise ValueError(
                "The model call ran out of its time budget before an answer arrived. "
                f"Last problem: {last_error or 'none'}"
            )
        retry_after = None
        try:
            response = requests.post(ENDPOINT, json=body, headers=headers,
                                     timeout=min(CALL_TIMEOUT, remaining))
        except requests.RequestException as error:
            last_error = f"OpenRouter request failed ({model}): {error}"
        else:
            if response.status_code == 200:
                return _content_from_payload(response.json(), model)
            last_error = (f"OpenRouter HTTP {response.status_code} ({model}): "
                          f"{response.text[:200]}")
            if response.status_code not in RETRY_STATUSES:
                raise ValueError(last_error)
            retry_after = _retry_after_seconds(response)

        if attempt == HTTP_ATTEMPTS - 1:
            break
        wait = BACKOFF_SECONDS[attempt] if retry_after is None else retry_after
        if wait > deadline - time.monotonic() - MIN_CALL_SECONDS:
            # Sleeping this long would leave no room to use the result.
            break
        time.sleep(wait)
    raise ValueError(last_error)
