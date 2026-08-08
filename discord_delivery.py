"""Authenticated handoff from the daily Python digest to the Discord Worker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import requests

from job_archive import job_card_payload


def interactive_cards_payload(snapshots: list[dict], heartbeat: dict | None = None) -> dict:
    payload = {
        "cards": [
            {
                "job_key": snapshot["job_key"],
                "snapshot": snapshot,
                "payload": job_card_payload(snapshot),
            }
            for snapshot in snapshots
        ]
    }
    if heartbeat:
        payload["heartbeat"] = heartbeat
    return payload


def post_interactive_cards(
    worker_url: str,
    secret: str,
    snapshots: list[dict],
    *,
    heartbeat: dict | None = None,
) -> dict:
    payload = json.dumps(interactive_cards_payload(snapshots, heartbeat), ensure_ascii=False).encode("utf-8")
    path = "/internal/digest"
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    canonical = f"{timestamp}\n{nonce}\nPOST\n{path}\n".encode("utf-8") + payload
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()).decode("ascii")
    response = requests.post(
        worker_url.rstrip("/") + path,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Job-Digest-Signature": signature,
            "X-Job-Digest-Timestamp": timestamp,
            "X-Job-Digest-Nonce": nonce,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()
