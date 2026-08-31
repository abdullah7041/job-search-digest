"""Authenticated handoff from the daily Python digest to the Discord Worker."""

from __future__ import annotations

import json

from job_archive import job_card_payload
from worker_client import signed_request


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
    """Hand the day's cards to the Worker over the signed internal contract.

    The signing lives in worker_client, not here. This function used to
    hand-roll the same HMAC and sign a hardcoded "/internal/digest", while the
    Worker verifies over the request's real pathname -- so a WORKER_URL with any
    base path would have signed one string and been checked against another, and
    every card delivery would have failed 401 with nothing in the logs saying
    why. Deriving the path from the URL is worker_client's job.
    """
    payload = json.dumps(interactive_cards_payload(snapshots, heartbeat),
                         ensure_ascii=False).encode("utf-8")
    url = worker_url.rstrip("/") + "/internal/digest"
    return signed_request("POST", url, secret, payload).json()
