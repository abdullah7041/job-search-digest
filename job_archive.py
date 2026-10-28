"""Durable job snapshots and Discord application card payloads."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re


JD_MIN_CHARS = 400
JD_MIN_WORDS = 60
RESPONSIBILITY_MARKERS = ("responsibil", "responsible for", "you will", "duties", "about the role")
REQUIREMENT_MARKERS = ("requirement", "qualification", "experience", "skills")
RESPONSIBILITY_HEADINGS = (
    "what you'll do", "what you’ll do", "what you will do", "your impact", "job description", "the role",
)

# The headings a real posting puts in front of the part that states what the job
# actually is. Everything above the first one is recruiting copy: culture, perks,
# and how much the employer will invest in you. Ordering here does not matter --
# whichever appears earliest in the text wins.
SIGNAL_HEADINGS = (
    "your impact", "what you'll do", "what you’ll do", "what you will do", "what youll do",
    "responsibilities", "requirements", "qualifications", "who you are",
    "your profile", "what we're looking for", "what we are looking for",
    "what you bring", "about the role", "the role", "skills", "must have", "job description",
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
    if (not any(marker in lowered for marker in RESPONSIBILITY_MARKERS)
            and not any(match.group(1).lower() in RESPONSIBILITY_HEADINGS
                        for match in SIGNAL_RE.finditer(jd_text or ""))):
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


# What a posting DEMANDS, as opposed to what it describes. These three patterns
# are what a hard requirement looks like in the wild: a credential, a span of
# years, or a word that marks the line as non-negotiable.
REQUIREMENT_RE = re.compile(
    r"(?i)\b(?:bachelor|master|phd|ph\.d|b\.?sc|m\.?sc|degree|diploma|certifi\w*"
    r"|licen[cs]e|qualification"
    r"|\d+\s*(?:[-\u2013\u2014+]\s*\d*)?\s*\+?\s*(?:years?|yrs?)"
    r"|must have|must be|required|requirement|minimum|mandatory|essential"
    r"|proven experience|hands-on experience)\b"
)

# A requirement line shorter than this is a fragment, and one longer than this is
# a paragraph that happens to contain the word "required". A long chunk is split
# on sentence boundaries first, which is what recovers a qualifications block
# written as prose rather than as bullets: 14 postings lost their requirement
# without that split, 11 with it.
REQUIREMENT_MIN_CHARS = 12
REQUIREMENT_MAX_CHARS = 400
REQUIREMENT_BUDGET = 900

CHUNK_SPLIT_RE = re.compile(r"\n(?=\s*[*\u2022\-]|\s*[A-Z])")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z])")


def jd_requirements(jd_text: str, limit: int = REQUIREMENT_BUDGET) -> list[str]:
    """The lines where a posting states what it demands, wherever they sit.

    jd_signal answers "where does this posting start describing the job", which
    is the right question for responsibilities and the wrong one for
    qualifications. A posting puts its Minimum Qualifications at the END, so a
    window anchored at the first heading walks away from them.

    Measured on the Ma'aden posting archived 2026-09-08: 5187 characters, with
    responsibilities at 217, Minimum Qualifications at 3958 and Skills at 4529.
    The 2000-character ranking window covered 217-2217, showed the model the
    flattering half only, and it scored 91 while Watheq scored the same posting
    67 on the full text. No single contiguous window reaches both ends, and
    anchoring on the LAST heading lands on Skills and skips the qualifications
    above it -- so this reads lines rather than a span.

    Measured across the 120 archived postings that state a qualification:

        jd_signal(text, 2000)     49 lose it
        a head-plus-tail window   50 lose it
        this                      11 lose it

    at a mean of 403 characters per posting. `limit` bounds what one ranking
    call carries for one job; it is headroom, not a working truncation.
    """
    chunks: list[str] = []
    for chunk in CHUNK_SPLIT_RE.split(jd_text or ""):
        if len(" ".join(chunk.split())) > REQUIREMENT_MAX_CHARS:
            chunks.extend(SENTENCE_SPLIT_RE.split(chunk))
        else:
            chunks.append(chunk)

    found: list[str] = []
    seen: set[str] = set()
    total = 0
    for chunk in chunks:
        # The bullet marker the split left on the front carries no meaning once
        # the line is out of its list.
        line = " ".join(chunk.split()).lstrip("*•- ")
        if not REQUIREMENT_MIN_CHARS <= len(line) <= REQUIREMENT_MAX_CHARS:
            continue
        if not REQUIREMENT_RE.search(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        if total + len(line) + 2 > limit:
            break
        found.append(line)
        total += len(line) + 2
    return found


def jd_grade(jd_text: str) -> str:
    """How much of a real job description this text actually is."""
    text = jd_text or ""
    normalized = " ".join(text.split())
    if not normalized:
        return GRADE_MISSING
    if jd_completeness_failures(text):
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
    prefix = "pending:" if job.get("application_state") == "needs_checking" else ""
    return prefix + f"{job.get('source') or 'unknown'}:{job.get('id') or job.get('url') or 'unknown'}"


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
        "application_action": job.get("application_action", ""),
        "application_state": job.get("application_state", ""),
        "pending_reasons": job.get("pending_reasons") or [],
        "score": None if job.get("application_state") == "needs_checking" else job.get("score", 0),
        "why": ("Potentially relevant role; application fit has not been verified."
                if job.get("application_state") == "needs_checking" else job.get("why", "")),
        "ranking_verified": bool(job.get("ranking_verified")),
        # Additive, like ceiling and jd.grade below. Which target track the
        # ranker judged this against, and the four rubric categories it
        # reported. Archived so a score can be read back apart from the one
        # number, which is the whole complaint that produced them.
        "track": job.get("track", ""),
        "categories": job.get("categories") or {},
        "match_evidence": [
            {"title": strength["title"], "resume_quote": quote["snippet"][:180]}
            for strength in (job.get("fit_check") or {}).get("strengths") or []
            for quote in strength.get("evidence") or []
            if quote.get("source") == "resume" and quote.get("snippet")
        ][:3],
        # Additive, like the jd.grade field below: schema_version stays 1 so a
        # same-day append to an archive written before these existed still
        # merges. Readers must treat a missing ceiling as "unknown".
        "ceiling": (job.get("fit_check") or {}).get("ceiling", "unknown"),
        "blockers": [blocker.get("title", "") for blocker
                     in ((job.get("fit_check") or {}).get("blockers") or [])][:3],
        "unclear": ((job.get("fit_check") or {}).get("unclear") or [])[:3],
        "link_status": job.get("link_status", "unknown"),
        # Additive. first_published was written by every fetcher and dropped
        # here, so no archived run could answer how old its cards were -- which
        # is exactly the question asked after a month-old posting was delivered
        # on 7 Sep 2026. age_days is None when the source published no date.
        "first_published": job.get("first_published", ""),
        "age_days": job.get("age_days"),
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
    pending = snapshot.get("application_state") == "needs_checking"
    footer = f"{snapshot['tier']} · " + ("Fit pending" if pending else f"fit {snapshot['score']}")
    age = snapshot.get("age_days")
    if type(age) is int and age >= 0:
        footer += f" · Posted {age}d ago"
    if not snapshot.get("ranking_verified"):
        # A keyword fallback score rendered identically to a model score, so a
        # run where the ranker was down looked exactly like a run where it
        # worked. Say which one produced the number.
        footer += " · Heuristic score, ranker unavailable"
    # What the archive actually holds, and what the button will do about it.
    #
    # "Job description needed before tailoring" was wrong twice on the run of
    # 2026-09-09. It read as a demand, when `interactive_runner._resolve_jd`
    # falls straight through to re-fetching the posting from its URL. And it
    # sat on the same card as three verified gaps quoted out of that very text,
    # so a card said "here is what the posting demands" and "send me the
    # posting" in the same breath. Partial text is still text.
    status = (snapshot.get("jd") or {}).get("status")
    if status == "missing":
        footer += " · No posting text archived; tailoring reads the link"
    elif status != "captured":
        footer += " · Partial posting archived; tailoring re-reads the link"
    elif snapshot_grade(snapshot) == GRADE_TEASER:
        footer += " · Short teaser, not the full posting"
    if snapshot.get("link_status") == "unknown":
        footer += " · Link check inconclusive"
    # A gap earns its line whatever the ceiling. It used to take a "low"
    # ceiling, which needs two blockers or one the model called critical -- so a
    # single verified missing bachelor's degree showed nothing at all, on a card
    # whose score had already been capped for it.
    #
    # "unknown" is the opposite case and needs saying out loud now that every
    # delivered job is checked rather than only the ones scoring 80+: a job past
    # the check budget renders identically to one checked and found clean, and
    # silence must read as unchecked rather than as clean.
    blockers = snapshot.get("blockers") or []
    if blockers:
        footer += f" · {len(blockers)} verified gap(s)"
    elif snapshot.get("ceiling") == "unknown":
        footer += " · Requirements not checked"
    unclear = snapshot.get("unclear") or []
    if unclear:
        footer += f" · {len(unclear)} question(s) to check"
    fields = [
        {"name": "Company", "value": snapshot["company"] or "Unknown", "inline": True},
        {"name": "Location", "value": snapshot["location"] or "Unknown", "inline": True},
    ]
    if snapshot.get("application_action"):
        fields.append({"name": "Next step", "value": snapshot["application_action"], "inline": False})
    if snapshot.get("pending_reasons"):
        fields.append({"name": "Needs checking", "value": "\n".join("• " + reason for reason in snapshot["pending_reasons"])[:1024], "inline": False})
    if snapshot.get("match_evidence"):
        fields.append({"name": "Background evidence",
                       "value": "\n".join(f"• {item['title']}: {item['resume_quote']}"
                                          for item in snapshot["match_evidence"])[:1024],
                       "inline": False})
    if blockers:
        # In a field rather than crammed into the footer: these are the whole
        # reason the score was capped, so they have to be readable.
        fields.append({"name": "Gaps the posting states",
                       "value": "\n".join(f"• {title}" for title in blockers)[:1024],
                       "inline": False})
    if unclear:
        fields.append({"name": "Questions to check",
                       "value": "\n".join(f"• {item.get('topic', 'Fit question')}: {item.get('reason', '')}"
                                          for item in unclear)[:1024],
                       "inline": False})
    return {
        "embeds": [{
            "title": snapshot["title"],
            "url": snapshot["url"],
            "description": snapshot["why"],
            "fields": fields,
            "footer": {"text": footer},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 1,
                "label": "Tailor CV",
                "custom_id": f"tailor:{hashlib.sha256(snapshot['job_key'].encode('utf-8')).hexdigest()[:32]}",
                "disabled": bool(snapshot.get("application_state")) and (status != "captured" or snapshot_grade(snapshot) != GRADE_FULL),
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
            if not snapshot.get("ranking_verified") or snapshot.get("score") is None:
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
