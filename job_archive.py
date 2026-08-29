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

# The headings a real posting puts in front of the part that states what the job
# actually is. Everything above the first one is recruiting copy: culture, perks,
# and how much the employer will invest in you. Ordering here does not matter --
# whichever appears earliest in the text wins.
SIGNAL_HEADINGS = (
    "your impact", "what you'll do", "what you will do", "what youll do",
    "responsibilities", "requirements", "qualifications", "who you are",
    "your profile", "what we're looking for", "what we are looking for",
    "what you bring", "about the role", "the role", "skills", "must have",
)

# Aggregators hand over a paragraph of marketing copy and call it a job
# description. It clears every threshold above -- 400 chars, 60 words, and the
# word "experience" -- while saying nothing a fit score could be built on. The
# Pasqal quantum posting archived on 2026-08-20 is 497 characters and was
# delivered as a scored match.
#
# Length alone is the wrong test: a short posting that says what the work is and
# what it needs is a real posting. A real one names both, however briefly. A
# teaser names neither -- it is one paragraph of prose with no structure at all.
# So a description is judged on the headings it carries, and length only decides
# how many it has to carry.
TEASER_MIN_CHARS = 1200
TEASER_MIN_HEADINGS = 2

GRADE_MISSING = "missing"
GRADE_INCOMPLETE = "incomplete"
GRADE_TEASER = "teaser"
GRADE_FULL = "full"

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


# A heading has to be in heading position. A bare substring search put the
# QuantumBlack window at "...your location, experience, and skills), we provide
# a comprehensive benefits package" — the word "skills" inside a sentence about
# perks. So a match only counts at the start of the text, the start of a line,
# or after sentence-ending punctuation or a bullet.
SIGNAL_RE = re.compile(
    r"(?:^|[\n\r.;:!?•*\-]\s*)(" + "|".join(re.escape(h) for h in SIGNAL_HEADINGS) + r")\b",
    re.IGNORECASE,
)


def signal_headings(jd_text: str) -> list[int]:
    """Offsets of every heading that introduces the work, earliest first."""
    return sorted(match.start(1) for match in SIGNAL_RE.finditer(jd_text or ""))


def signal_offset(jd_text: str) -> int:
    """Where the posting stops selling and starts describing the job."""
    found = signal_headings(jd_text)
    return found[0] if found else -1


def jd_signal(jd_text: str, limit: int) -> str:
    """The requirements-bearing window of a job description.

    Scoring used to read the first 900 characters, which for a posting like
    QuantumBlack's is entirely culture copy -- the role itself begins at
    character 1931. The model then reported, accurately, that the description
    named no technical requirements. It had never been shown them. Starting the
    window at the first heading that introduces the work fixes what the scorer
    reads without making it read everything.
    """
    text = jd_text or ""
    if len(text) <= limit:
        # It all fits, so there is nothing to choose between. Skipping ahead
        # here would only throw away the opening of a short posting: the Pasqal
        # teaser's one heading sits near its end, and starting there left 199
        # characters of a 497-character description.
        return text
    offset = signal_offset(text)
    return text[offset:offset + limit] if offset != -1 else text[:limit]


def jd_grade(jd_text: str) -> str:
    """How much of a real job description this text actually is."""
    text = jd_text or ""
    normalized = " ".join(text.split())
    if not normalized:
        return GRADE_MISSING
    if jd_completeness_failures(normalized):
        return GRADE_INCOMPLETE
    if (len(normalized) < TEASER_MIN_CHARS
            and len(signal_headings(text)) < TEASER_MIN_HEADINGS):
        return GRADE_TEASER
    return GRADE_FULL


def snapshot_grade(snapshot: dict) -> str:
    """Read a snapshot's grade, deriving one for archives written before it.

    Snapshots on disk predate the field, so their grade is recovered from the
    text they carry rather than assumed. That keeps every archived job usable
    instead of silently downgrading months of evidence.
    """
    jd = snapshot.get("jd") or {}
    grade = jd.get("grade")
    if grade in (GRADE_MISSING, GRADE_INCOMPLETE, GRADE_TEASER, GRADE_FULL):
        return grade
    return jd_grade(jd.get("text") or "")


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
        # The employer's own posting when the run found it, else the link the
        # aggregator gave. This is the link the card offers and the one a later
        # "Tailor CV" click re-reads the JD from, so it wants to be the source.
        "url": job.get("apply_url") or job.get("url", ""),
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
            # Additive on purpose: schema_version stays 1 so a same-day append
            # to an archive written before this field still merges instead of
            # raising. snapshot_grade() covers the files that predate it.
            "grade": jd_grade(jd_text),
        },
    }


def job_card_payload(snapshot: dict) -> dict:
    """Return an application-owned Discord message with one Tailor CV button."""
    jd_available = snapshot["jd"]["status"] == "captured"
    footer = f"{snapshot['tier']} · fit {snapshot['score']}"
    if not snapshot.get("ranking_verified"):
        # A keyword fallback score rendered identically to a model score, so a
        # run where the ranker was down looked exactly like a run where it
        # worked. Say which one produced the number.
        footer += " · Heuristic score, ranker unavailable"
    if not jd_available:
        footer += " · Job description needed before tailoring"
    elif snapshot_grade(snapshot) == GRADE_TEASER:
        footer += " · Short teaser, not the full posting"
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


# A score that moves while the description behind it does not is the ranker
# being unstable, not the market changing. QuantumBlack's Software Engineer II
# scored 40, 40, 40, 45, 50, 50 and 75 across the seven runs the model scored it,
# on a byte-identical 5417-char description. Nothing in the daily average showed
# it, because those scores sat in different days' rows.
INSTABILITY_WINDOW_DAYS = 14
INSTABILITY_THRESHOLD = 15


def score_instability(archive_dir: str = "jobs", window_days: int = INSTABILITY_WINDOW_DAYS,
                      threshold: int = INSTABILITY_THRESHOLD,
                      today: datetime.date | None = None) -> list[dict]:
    """Jobs whose score moved while their job description did not.

    Read straight out of the archives already committed each day, so this costs
    no new state file and no extra call. Fail-soft: an unreadable archive is
    skipped, never raised, because this is a diagnostic and the digest has to go
    out either way.
    """
    if not os.path.isdir(archive_dir):
        return []
    cutoff = ((today or datetime.date.today()) - datetime.timedelta(days=window_days)).isoformat()
    seen: dict[str, dict] = {}
    for name in sorted(os.listdir(archive_dir)):
        stem = name[: -len(".json")] if name.endswith(".json") else ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem) or stem < cutoff:
            continue
        try:
            with open(os.path.join(archive_dir, name), encoding="utf-8") as handle:
                archive = json.load(handle)
        except (OSError, ValueError):
            continue
        for snapshot in archive.get("jobs") or []:
            # Only model scores are comparable; the keyword fallback is a
            # constant for a given title and would read as perfect stability.
            if not snapshot.get("ranking_verified"):
                continue
            digest = (snapshot.get("jd") or {}).get("sha256") or ""
            if not digest:
                continue
            # Keyed on the description itself, not on job_key: the aggregator
            # mints a fresh opaque id for the same posting every day, so a
            # key-based grouping would never see the same job twice. Identical
            # JD bytes are the same posting by any useful definition.
            record = seen.setdefault(digest, {
                "title": snapshot.get("title", ""),
                "company": snapshot.get("company", ""),
                "scores": [],
            })
            record["scores"].append(int(snapshot.get("score") or 0))

    unstable = []
    for record in seen.values():
        scores = record["scores"]
        if len(scores) < 2:
            continue
        spread = max(scores) - min(scores)
        if spread >= threshold:
            unstable.append({**record, "spread": spread})
    return sorted(unstable, key=lambda item: item["spread"], reverse=True)


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
