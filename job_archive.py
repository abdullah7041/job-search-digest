"""Durable job snapshots and Discord application card payloads."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re


JD_MIN_CHARS = 400
JD_MIN_WORDS = 60
RESPONSIBILITY_MARKERS = ("responsibil", "you will", "duties", "about the role")
REQUIREMENT_MARKERS = ("requirement", "qualification", "experience", "skills")

# Snapshots exist so a "Tailor CV" button clicked days later still resolves to
# the exact captured job description. Once a job id has aged out of seen.json
# there is nothing left to resolve, so this matches STATE_TTL_DAYS in digest.py.
ARCHIVE_TTL_DAYS = 120


def jd_completeness_failures(jd_text: str) -> list[str]:
    """Return evidence that captured text is only a snippet, not a usable JD."""
    normalized = " ".join((jd_text or "").split())
    if not normalized:
        return ["missing"]
    failures = []
    if len(normalized) < JD_MIN_CHARS or len(re.findall(r"\b\w+\b", normalized)) < JD_MIN_WORDS:
        failures.append("too_short")
    lowered = normalized.lower()
    if not any(marker in lowered for marker in RESPONSIBILITY_MARKERS):
        failures.append("responsibilities_not_evidenced")
    if not any(marker in lowered for marker in REQUIREMENT_MARKERS):
        failures.append("requirements_not_evidenced")
    return failures


def job_key(job: dict) -> str:
    """Stable provider-scoped key suitable for a short Discord custom_id."""
    return f"{job.get('source') or 'unknown'}:{job.get('id') or job.get('url') or 'unknown'}"


def make_snapshot(job: dict, captured_at: str) -> dict:
    """Copy the evidence needed to tailor after the scheduled run has ended."""
    jd_text = (job.get("description") or "").strip()
    completeness_failures = jd_completeness_failures(jd_text)
    return {
        "schema_version": 1,
        "job_key": job_key(job),
        "captured_at": captured_at,
        "source": job.get("source", ""),
        "source_id": job.get("id", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "salary": job.get("salary", ""),
        "tier": job.get("tier", "Worth reviewing"),
        "score": job.get("score", 0),
        "why": job.get("why", ""),
        "ranking_verified": bool(job.get("ranking_verified")),
        "link_status": job.get("link_status", "unknown"),
        "jd": {
            "status": "captured" if not completeness_failures else ("missing" if not jd_text else "incomplete"),
            "text": jd_text,
            "sha256": hashlib.sha256(jd_text.encode("utf-8")).hexdigest() if jd_text else "",
            "completeness_failures": completeness_failures,
        },
    }


def job_card_payload(snapshot: dict) -> dict:
    """Return an application-owned Discord message with one Tailor CV button."""
    jd_available = snapshot["jd"]["status"] == "captured"
    footer = f"{snapshot['tier']} · fit {snapshot['score']}"
    if not jd_available:
        footer += " · Job description needed before tailoring"
    if snapshot.get("link_status") == "unknown":
        footer += " · Link check inconclusive"
    return {
        "embeds": [{
            "title": snapshot["title"],
            "url": snapshot["url"],
            "description": snapshot["why"],
            "fields": [
                {"name": "Company", "value": snapshot["company"] or "Unknown", "inline": True},
                {"name": "Location", "value": snapshot["location"] or "Unknown", "inline": True},
            ],
            "footer": {"text": footer},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 1,
                "label": "Tailor CV",
                "custom_id": f"tailor:{hashlib.sha256(snapshot['job_key'].encode('utf-8')).hexdigest()[:32]}",
                "disabled": False,
            }],
        }],
        "allowed_mentions": {"parse": []},
    }


def prune_snapshots(archive_dir: str = "jobs", ttl_days: int = ARCHIVE_TTL_DAYS,
                    today: datetime.date | None = None) -> list[str]:
    """Delete archive files older than the TTL and return the removed paths."""
    if not os.path.isdir(archive_dir):
        return []
    cutoff = ((today or datetime.date.today()) - datetime.timedelta(days=ttl_days)).isoformat()
    removed = []
    for name in sorted(os.listdir(archive_dir)):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        # Only prune files this module names. An unexpected filename is left
        # alone rather than guessed at.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
            continue
        if stem < cutoff:
            path = os.path.join(archive_dir, name)
            os.remove(path)
            removed.append(path)
    return removed


def write_snapshots(jobs: list[dict], date: str, archive_dir: str = "jobs") -> str:
    """Append new same-day evidence without replacing an existing job snapshot."""
    os.makedirs(archive_dir, exist_ok=True)
    prune_snapshots(archive_dir)
    path = os.path.join(archive_dir, f"{date}.json")
    existing_jobs = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
        if existing.get("schema_version") != 1 or existing.get("date") != date:
            raise ValueError(f"Existing snapshot archive {path} has an incompatible schema or date.")
        existing_jobs = existing.get("jobs") or []

    merged = list(existing_jobs)
    existing_keys = {snapshot.get("job_key") for snapshot in existing_jobs}
    for job in jobs:
        snapshot = make_snapshot(job, f"{date}T00:00:00Z")
        if snapshot["job_key"] not in existing_keys:
            merged.append(snapshot)
            existing_keys.add(snapshot["job_key"])
    archive = {
        "schema_version": 1,
        "date": date,
        "jobs": merged,
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(archive, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return path
