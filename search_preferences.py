"""Enforce the canonical job-search preferences without inventing missing facts."""

import re
from pathlib import Path
from urllib.parse import urlsplit


PREFERENCES_PATH = Path(__file__).with_name("JOB_SEARCH_PREFERENCES.md")
PRIMARY_TRACKS = ("AI and LLM product engineering", "Software engineering, front and back", "Data and analytics")


def load_preferences():
    return PREFERENCES_PATH.read_text(encoding="utf-8").strip()


def employment_status(job):
    """Explicit non-full-time wins; missing or ambiguous evidence stays unknown."""
    raw = job.get("employment_type") or ""
    if isinstance(raw, list) and all(isinstance(value, str) for value in raw):
        raw = " ".join(raw)
    raw = raw.lower().replace("_", " ") if isinstance(raw, str) else ""
    description = job.get("description") or ""
    explicit = re.findall(r"(?im)^\s*(?:employment type|job type|employment|schedule)\s*:\s*([^\n.]+)", description)
    wording = " ".join([raw, job.get("title") or "", *explicit]).lower()
    if re.search(r"\b(?:not|non)[ -]+full[ -]?time\b", wording):
        return "excluded"
    if re.search(r"\b(?:part[ -]?time|contract(?:or|ual)?|temporary|temp|fixed[ -]?term|freelance|intern(?:ship)?|co[ -]?op|tamheer)\b", wording):
        return "excluded"
    if re.search(r"\bfull[ -]?time\b", wording):
        return "full_time"
    return "unknown"


def readiness_issues(job):
    issues = []
    if employment_status(job) != "full_time":
        issues.append("Full-time employment has not been confirmed.")
    if job.get("link_status") != "open":
        issues.append("Live vacancy status has not been confirmed; this is an unverified lead.")
    if job.get("employer_verified") is False:
        issues.append("The actual employing entity has not been verified on the vacancy page.")
    return issues


def description_relevant(job):
    """A nonstandard title may qualify through concrete work described in its JD."""
    text = job.get("description") or ""
    # ponytail: discovery hints only; model ranking and cited fit evidence still decide admission.
    return bool(re.search(r"(?i)\b(?:LLM|generative AI|artificial intelligence|Python|TypeScript|React|SQL|Power BI"
                          r"|software development|software engineering|data analys\w*|business intelligence"
                          r"|customer segmentation|sales analytics|sales operations)\b", text))


def track_priority(job):
    try:
        return PRIMARY_TRACKS.index(job.get("track"))
    except ValueError:
        return len(PRIMARY_TRACKS)


def discovery_source(job):
    if job.get("discovery_source"):
        return job["discovery_source"]
    source = job.get("source") or "Unknown"
    host = (urlsplit(job.get("discovery_url") or job.get("url") or "").hostname or "").lower()
    if source == "linkedin_api" or host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "LinkedIn" if source != "jsearch" else "JSearch via LinkedIn"
    if source == "indeed" or "indeed." in host:
        return "Indeed" if source != "jsearch" else "JSearch via Indeed"
    return "Official careers site" if source == "target" else source
