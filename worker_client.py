"""Signed HTTP client for the private Worker-to-Actions contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from urllib.parse import urlsplit

import requests


def signed_request(method: str, url: str, secret: str, body: bytes = b"") -> requests.Response:
    """Send one HMAC-authenticated request accepted by the Worker."""
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    path = urlsplit(url).path
    canonical = f"{timestamp}\n{nonce}\n{method}\n{path}\n".encode("utf-8") + body
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()
    ).decode("ascii")
    response = requests.request(
        method,
        url,
        data=body if method != "GET" else None,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Job-Digest-Signature": signature,
            "X-Job-Digest-Timestamp": timestamp,
            "X-Job-Digest-Nonce": nonce,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response
