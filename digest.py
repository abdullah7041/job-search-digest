#!/usr/bin/env python3
"""
Daily job-search digest -> Discord.

Pattern (same as the AI-news digest):
  gather  -> JSearch (RapidAPI, Google for Jobs aggregator) for breadth
             + company ATS APIs (Greenhouse, Ashby, Workable, Lever) for depth
  filter  -> Saudi-only, senior+, title relevant, not seen before
  rank    -> one cheap OpenRouter call for score + one-line "why it fits"
             (deterministic keyword fallback if the ranker is missing or fails)
  deliver -> Discord webhook
Runs on GitHub Actions cron. No laptop needed.

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  DISCORD_WEBHOOK_URL   required
  RAPIDAPI_KEY          required for JSearch. Without it JSearch is skipped (ATS-only run).
  OPENROUTER_API_KEY    optional. Without it, scoring is keyword-only.

State:
  seen.json holds job ids already sent. The workflow commits it back after each
  run, so every posting is delivered once and a missed day is caught next run.
  First run has an empty seen.json, so the first digest can be large (capped).
  stats.json holds one row per run (fetched/matched/new/avg fit score) so a
  silently-stale profile shows up as a trend instead of looking healthy.

Profile:
  profile.json holds the candidate blurb, skill keywords, and target titles fed
  to the ranking model and the keyword fallback. Refresh it with PROFILE_REFRESH_PROMPT.md
  (manually, or via the monthly nudge) as your background/skills/target roles
  change. Missing or corrupt profile.json falls back to DEFAULT_PROFILE below.
"""

import os
import re
import hashlib
import sys
import json
import time
import datetime

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from discord_delivery import post_interactive_cards
from jd_fetch import ats_reference, ats_url, bounded_get, extract_posting, strip_html
from job_archive import (GRADE_TEASER, jd_grade, jd_signal,
                         make_snapshot, score_instability, write_snapshots)
from openrouter import chat_json, configured_chain

# ------------------------------- CONFIG -------------------------------------
# Each company names its ATS in "source":
#   "greenhouse" + region "us"/"eu"   -> boards-api[.eu].greenhouse.io
#   "ashby"                           -> api.ashbyhq.com/posting-api
#   "workable"                        -> apply.workable.com/api/v3 (POST, paginated)
#   "lever"                           -> api.lever.co/v0/postings
#   "pinpoint"                        -> <token>.pinpointhq.com/postings.json
#
# VERIFIED LIVE (24 Jun 2026), fetched from the real APIs during the build:
#   HALA    greenhouse/us  hala       -> AI Engineer, Automation Eng, SRE, Backend (Riyadh)
#   Careem  greenhouse/us  careem     -> AI/founding-engineer roles (UAE-heavy, few Saudi)
#   Tamara  greenhouse/eu  tamara     -> Senior Product Engineer Backend, Data, PM (Riyadh)
#   Lean    ashby          LeanTech   -> Senior Solutions Eng, Staff DevOps Eng (Riyadh)
#   Foodics workable       foodics    -> 32 roles, several Riyadh (filter strips the interns)
#
# Every fetch is fail-soft: a dead token or down API logs a WARN and is skipped,
# never crashes the run. Add companies below; see CANDIDATES at the bottom of
# this file for ones whose ATS still needs confirming in Claude Code.
COMPANIES = [
    {"name": "HALA",   "source": "greenhouse", "token": "hala",     "region": "us"},
    {"name": "Careem", "source": "greenhouse", "token": "careem",   "region": "us"},
    {"name": "Tamara", "source": "greenhouse", "token": "tamara",   "region": "eu"},
    {"name": "Lean",   "source": "ashby",      "token": "LeanTech"},
    {"name": "Foodics", "source": "workable",  "token": "foodics"},  # verified live 24 Jun 2026, 32 roles
    {"name": "Salla",  "source": "workable",   "token": "salla"},    # verified live 23 Jul 2026, 25 roles
    {"name": "Tabby",  "source": "pinpoint",   "token": "tabby"},    # verified live 23 Jul 2026, 63 roles / 26 KSA
    # Dormant: real Workable accounts, 0 open roles as of 23 Jul 2026. Kept on the
    # Careem/Foodics precedent — one cheap fail-soft call a day, and a senior role
    # surfaces automatically when one lands.
    {"name": "Mozn",     "source": "workable", "token": "mozn"},
    {"name": "Unifonic", "source": "workable", "token": "unifonic"},
    {"name": "Jahez",    "source": "workable", "token": "jahez"},
    {"name": "Zid",      "source": "workable", "token": "zid"},
    {"name": "Nana",     "source": "workable", "token": "nana"},
    {"name": "Rasan",    "source": "workable", "token": "rasan"},
    {"name": "Sary",     "source": "workable", "token": "sary"},
]

# Saudi only. A job's location must contain one of these (case-insensitive).
# Arabic terms included because JSearch returns Saudi city names in Arabic
# (الرياض = Riyadh, جدة = Jeddah, الخبر = Khobar, etc.); the country name is
# expanded from the ISO code so "saudi" still matches even when the city is Arabic.
LOCATION_INCLUDE = [
    "saudi", "ksa", "riyadh", "jeddah", "dammam", "khobar",
    "dhahran", "mecca", "makkah", "medina", "madinah",
    "السعودية", "الرياض", "جدة", "الدمام", "الخبر", "مكة", "المدينة",
]

# Expand ISO-3166 alpha-2 codes (JSearch returns these as job_country) to English
# names so the English LOCATION_INCLUDE entries match. Gulf + Egypt cover the region
# these boards post in; an unknown code falls through to its raw value.
COUNTRY_CODE_TO_NAME = {
    "SA": "Saudi Arabia", "AE": "United Arab Emirates", "BH": "Bahrain",
    "KW": "Kuwait", "QA": "Qatar", "OM": "Oman", "EG": "Egypt",
}

# Title must match at least one of these to count as relevant.
TITLE_INCLUDE = [
    "engineer", "developer", "software", "backend", "back-end", "frontend",
    "front-end", "full stack", "fullstack", "full-stack", "ai ", " ai",
    "ml ", " ml", "machine learning", "data scien", "data engineer", "mlops",
    "devops", "sre", "site reliability", "platform", "automation", "architect",
    "product manager", "technical product", "head of engineering", "solutions engineer",
]

# Seniority filter: drop anything junior AND anything above senior IC.
# The 2026-07-31 digest scored Lead/Staff/Team-Leader roles 85-98 because this
# list only bounded seniority from below and profile.json said "senior or
# above". Target is senior individual contributor; leadership tracks are out.
TITLE_EXCLUDE = [
    "junior", "intern", "internship", "graduate", "trainee", "tamheer",
    "associate", "entry", "fresh", "co-op", "apprentice", "student",
    "lead ", " lead", "leader", "staff", "principal", "head of", "director",
    "chief", "vice president", " vp", "vp ", "engineering manager",
]

# Candidate profile (blurb, skill keywords, target titles) drives both the
# Gemini prompt and the keyword-fallback scoring. It lives in profile.json so
# it can be refreshed (manually or via the monthly nudge) without touching
# this file. DEFAULT_PROFILE is the fail-soft fallback if that file is
# missing or corrupt -- it holds the exact values this project shipped with.
PROFILE_FILE = "profile.json"
DEFAULT_PROFILE = {
    "blurb": (
        "Candidate: solo founder and senior engineer in Saudi Arabia. Strong in AI, "
        "automation, LLM apps, Python, backend, and cloud SaaS. Wants senior or above "
        "AI/ML, software, product, or data roles based in Saudi Arabia. Values simple, "
        "reliable, high-ownership work."
    ),
    "skill_keywords": [
        "ai", "ml", "machine learning", "llm", "automation", "python", "backend",
        "api", "cloud", "aws", "gcp", "data", "saas", "platform", "devops",
        "founding", "applied ai", "architect", "solutions",
    ],
    "target_titles": [],
}

# JSearch (RapidAPI / Google for Jobs aggregator), endpoint /search-v2.
# 3 queries/day = ~93 requests/month -> safely inside the free tier (200/month).
# Endpoint + response shape + field names verified live 28 Jun 2026.
AGGREGATOR_QUERIES = [
    "software engineer Saudi Arabia",
    "data engineer Riyadh",
    "machine learning engineer Saudi Arabia",
]
JSEARCH_HOST = "jsearch.p.rapidapi.com"

# The plan allows 200 requests a MONTH -- read off the response headers on
# 3 Sep 2026, which is also the first time anything here knew the number. That
# is 6.6 a day, so page two is a rationed extra rather than a free one: three
# page-one reads plus two deep reads is 150 a month and leaves the rest for
# probes, manual re-runs and a bad day. Override with a repository variable.
def _request_budget(raw, default=5):
    """The day's JSearch request allowance, never below one page per query."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= len(AGGREGATOR_QUERIES) else default


JSEARCH_DAILY_REQUESTS = _request_budget(os.environ.get("JSEARCH_DAILY_REQUESTS"))
# Never spend the last of the month on an extra page: page one is the digest's
# whole aggregator input, and an exhausted plan takes it out until the reset.
JSEARCH_QUOTA_RESERVE = 20

# Salary floor (SAR / month). SOFT signal only. Boards almost never list salary,
# so this NEVER rejects a posting. Benchmark 24 Jun 2026: senior eng Riyadh
# ~25k-35k SAR/mo, AI/ML 28k-35k.
SALARY_FLOOR_SAR_MONTH = 25000

# Ranking model, read from the OpenRouter catalogue on 2026-08-20.
#
#   google/gemini-3.7-flash — $0.375/M in, $1.875/M out, carrying a 0.75 discount
#   on its Vertex endpoints, which makes it cheaper than gemini-3.6-flash while
#   scoring materially higher (Artificial Analysis intelligence 56 vs 51.6).
#
# Scoring is not the classification-over-short-records job it was sized for when
# this ran on Flash-Lite: the ranker now reads the requirements section of each
# posting and has to weigh it against a profile. That is worth a real model, and
# at 25 jobs a day it still costs under a quarter a month.
#
# Both ids are overridable through repository variables of the same name.
DEFAULT_RANK_MODEL = "google/gemini-3.7-flash"
DEFAULT_RANK_FALLBACKS = "google/gemini-3.6-flash,openai/gpt-5.6-luna"
RANK_MODEL, RANK_FALLBACKS = configured_chain(
    "RANK_MODEL", "RANK_FALLBACK_MODELS", DEFAULT_RANK_MODEL, DEFAULT_RANK_FALLBACKS)

# Ranking carries the user's target roles and profile blurb alongside public job
# data, so provider-side collection is explicitly disabled while retaining the
# JSON-schema requirement.
RANK_PROVIDER = {"data_collection": "deny", "require_parameters": True}

# The daily run has no user waiting on it, but it does have a ten-minute
# workflow timeout it shares with every fetch above.
RANK_BUDGET_SECONDS = 120

MAX_JOBS_IN_DIGEST = 25
STRONG_MATCH_MIN_SCORE = 80

def _score_floor(raw: str, default: int = 50) -> int:
    """Read the delivery floor from the environment, tolerating a bad value.

    An unset repository variable renders as an empty string, and a mistyped one
    renders as whatever was typed. Neither is worth losing a day's digest over,
    so a value that is not a whole number in range falls back and says so.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        if raw:
            print(f"WARN MIN_DELIVER_SCORE={raw!r} is not a number; using {default}",
                  file=sys.stderr)
        return default
    if not 0 <= value <= 100:
        print(f"WARN MIN_DELIVER_SCORE={value} is out of range; using {default}",
              file=sys.stderr)
        return default
    return value


# Below this, a job is not delivered at all. The tiers only ever decided which
# heading a job appeared under, so every posting that cleared the hard filters
# was delivered no matter how poorly it scored, and the weak ones crowded out
# the review tier. Override with a repository variable of the same name.
#
# Only a model score can send a job here. A keyword fallback number is not
# evidence of a bad match — it is what the pipeline produces when the ranker is
# down — and silently delivering nothing on a ranker outage would be worse than
# delivering a noisy digest. See drop_below_floor().
MIN_DELIVER_SCORE = _score_floor(os.environ.get("MIN_DELIVER_SCORE") or "")

# Per-job resume tailoring is an explicit Discord interaction handled by
# interactive_runner.py. The daily run archives JD evidence and posts jobs
# only; it never spends tailoring credits or writes private resumes into the
# repository. These two bounds are what the daily run still needs from the JD.
JD_MAX_CHARS = 6000     # JD text stored per job in the snapshot archive
JD_RANK_CHARS = 2000    # JD window per job inside the one ranking call

# The window is taken from where the posting starts describing the job, not from
# the top. The old 900 leading characters of a McKinsey posting are entirely
# recruiting copy — the role begins at character 1931 — so the ranker reported,
# correctly, that the description named no technical requirements. It had never
# been shown them. 25 jobs at this width is roughly 14k input tokens per run.

# A teaser is a paragraph of marketing copy that clears the completeness checks
# without evidencing anything. It can be reviewed, and it can be tailored
# against with a warning, but it can never be a strong match: the score that
# says so would be built on nothing. 69 is the top of the rubric's "adjacent
# function, seniority mismatch, or several gaps" band, one point below the
# strong-match line.
TEASER_SCORE_CAP = 69

STATE_FILE = "seen.json"
STATE_TTL_DAYS = 120  # prune seen ids older than this to bound file size

# Sources that mint a fresh id for the same posting on every request. JSearch
# hands back an opaque per-request token, so seen.json keyed on it never matched
# twice and the same posting was delivered day after day: 65 of the 122
# aggregator cards in the first 11 archived days were repeats. One JAL frontend
# role arrived on 9 separate days under 11 different ids. Their URL does not
# move, so that is what the state file remembers them by.
#
# An ATS id is canonical and stable, so those keep it. That also leaves every
# existing key in seen.json matching exactly as before.
VOLATILE_ID_SOURCES = {"jsearch"}

# Query parameters that identify the click rather than the job. LinkedIn adds
# refId and trackingId per response, so leaving them in would recreate the churn
# this is here to remove. Everything else is kept: Indeed's ?jk= IS the job, and
# dropping the whole query string would collapse every Indeed posting onto one
# key.
TRACKING_PARAMS = {
    "refid", "trackingid", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id", "gh_src", "src", "ref", "source",
    "trk", "originalsubdomain",
}

# Hosts that relist an employer's posting behind their own funnel. JobLeads is
# the one that prompted this: it charges a subscription before the apply step,
# so 41 of the 178 links archived over 20 days cost money to act on while the
# employer's own posting was free. The rest are the same pattern without the
# price tag -- a wrapper around a posting whose original is a better link.
#
# LinkedIn, Indeed and Glassdoor are deliberately NOT here. They are free to act
# on, and LinkedIn alone is the largest single source of real Saudi postings
# this digest sees (65 of those 178 links). This list is about paying to reach a
# posting, not about preferring first-party links in general.
PAID_RELISTER_HOSTS = (
    "jobleads.com", "jooble.org", "jobrapido.com", "bebee.com",
    "learn4good.com", "thebigjobsite.com", "naukrigulf.com",
    "wuzzuf.net", "wzzff.com",
)

# seen.json holds two kinds of key. A job identity remembers the posting; this
# prefix remembers a description within its employer and title context, which
# catches an aggregator that relists the same job under a fresh URL. Both age
# out on the same TTL.
JD_SEEN_PREFIX = "jd:"
USER_AGENT = "job-digest/1.0 (personal job search)"

# Drift monitoring: stats.json tracks one row per run (fetched/matched/new/avg
# fit score) so a silently-stale profile shows up instead of looking healthy.
STATS_FILE = "stats.json"
STATS_TTL_DAYS = 120
DRIFT_WINDOW_DAYS = 7          # trailing window compared against the prior one
DRIFT_MIN_SCORED_RUNS = 14     # need two full windows before trusting a comparison
DRIFT_SCORE_DROP_THRESHOLD = 15  # point drop (7d avg vs prior 7d avg) that triggers a warning
# Drift watches the daily average, which averages away a ranker that gives one
# unchanged posting 40 one day and 75 the next. The archives already hold every
# score and every JD digest, so that instability is readable without new state.
ARCHIVE_INSTABILITY_DAYS = 14

# Link verification is evidence, not a binary gate. Third-party job sites can
# block or rate-limit GitHub Actions while still serving the applicant's browser.
LINK_OPEN = "open"
LINK_CLOSED = "closed"
LINK_UNKNOWN = "unknown"
# ----------------------------------------------------------------------------


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_profile():
    """Read profile.json; fall back to DEFAULT_PROFILE on any problem so a
    missing or half-edited file never breaks a run."""
    if not os.path.exists(PROFILE_FILE):
        log(f"WARN {PROFILE_FILE} not found, using default profile")
        return DEFAULT_PROFILE
    try:
        with open(PROFILE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        log(f"WARN {PROFILE_FILE} is corrupt, using default profile")
        return DEFAULT_PROFILE
    if not data.get("blurb") or not data.get("skill_keywords"):
        log(f"WARN {PROFILE_FILE} missing 'blurb' or 'skill_keywords', using default profile")
        return DEFAULT_PROFILE
    return data


PROFILE = load_profile()


def _get(url):
    # rescue_apply_link and verify_link fetch URLs the pipeline did not choose
    # -- aggregator links to whatever third-party page JSearch relayed -- so the
    # body is read through jd_fetch's bounded fetch instead of an unbounded GET.
    # See jd_fetch.MAX_RESPONSE_BYTES for why.
    return bounded_get(url, timeout=30)


def _post(url, body):
    # Workable's public job-board API is POST-only and cursor-paginated.
    return requests.post(
        url,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )


def fetch_greenhouse(c):
    # The Greenhouse job-board API is a SINGLE global host. There is no
    # boards-api.eu.greenhouse.io — that name doesn't resolve (confirmed live,
    # NXDOMAIN). The ".eu." prefix only exists on the front-end board URL
    # (job-boards.eu.greenhouse.io), never the API. EU-hosted boards like Tamara
    # are served from the same global API host, so "region" is ignored here.
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs?content=false"
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        log(f"WARN {c['name']}: greenhouse HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        log(f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "company": c["name"],
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url") or "",
            "first_published": j.get("first_published") or j.get("updated_at") or "",
            "source": "greenhouse", "token": c["token"],
            "description": "",  # fetched on demand for delivered jobs only
        })
    log(f"{c['name']}: {len(out)} jobs (greenhouse)")
    return out


def fetch_ashby(c):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}?includeCompensation=true"
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        log(f"WARN {c['name']}: ashby HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        log(f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({
            "company": c["name"],
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),  # ashby location is a plain string
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "first_published": j.get("publishedAt") or "",
            "source": "ashby", "token": c["token"],
            # descriptionPlain ships in the board payload (verified live 31 Jul 2026)
            "description": (j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or "")).strip(),
        })
    log(f"{c['name']}: {len(out)} jobs (ashby)")
    return out


def fetch_workable(c):
    # Workable v3 board API: POST, 10 jobs/page, cursor in "nextPage" -> echo back
    # as {"token": ...}. Verified live 24 Jun 2026 against apply.workable.com/foodics.
    # No public apply URL in the payload, so we build the canonical one from shortcode.
    url = f"https://apply.workable.com/api/v3/accounts/{c['token']}/jobs"
    out, body, pages = [], {}, 0
    MAX_PAGES = 30  # hard stop so a misbehaving cursor can never loop forever (300 jobs)
    while pages < MAX_PAGES:
        try:
            r = _post(url, body)
        except requests.RequestException as e:
            log(f"WARN {c['name']}: request failed: {e}")
            return out  # return what we already have; partial beats crashing
        if r.status_code != 200:
            log(f"WARN {c['name']}: workable HTTP {r.status_code} (token '{c['token']}'). Skipping.")
            return out
        try:
            data = r.json()
        except ValueError:
            log(f"WARN {c['name']}: non-JSON response. Skipping.")
            return out
        for j in data.get("results", []):
            if j.get("state") not in (None, "published"):
                continue
            loc = j.get("location") or {}
            # Combine city + country so LOCATION_INCLUDE matches "riyadh" AND "saudi".
            location = ", ".join(p for p in (loc.get("city"), loc.get("country")) if p)
            shortcode = j.get("shortcode") or ""
            out.append({
                "company": c["name"],
                "id": str(j.get("id")),
                "title": (j.get("title") or "").strip(),
                "location": location,
                "url": f"https://apply.workable.com/{c['token']}/j/{shortcode}/" if shortcode else "",
                "first_published": j.get("published") or "",
                "source": "workable", "token": c["token"], "shortcode": shortcode,
                "description": "",  # fetched on demand via the v1 widget API
            })
        cursor = data.get("nextPage")
        if not cursor:
            break
        body = {"token": cursor}
        pages += 1
        time.sleep(0.3)  # be polite across pages
    log(f"{c['name']}: {len(out)} jobs (workable)")
    return out


def fetch_lever(c):
    # Lever v0 postings API: GET, returns a flat JSON array. createdAt is epoch ms.
    # Verified shape live 24 Jun 2026. No confirmed KSA company on Lever yet, but the
    # reader is here so adding one is a single COMPANIES line.
    url = f"https://api.lever.co/v0/postings/{c['token']}?mode=json"
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        log(f"WARN {c['name']}: lever HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        log(f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    if not isinstance(data, list):
        log(f"WARN {c['name']}: unexpected lever payload. Skipping.")
        return []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        # Normalise epoch-ms to an ISO date so the seen.json TTL prune stays uniform.
        published = ""
        if isinstance(created, (int, float)):
            published = datetime.datetime.utcfromtimestamp(created / 1000).date().isoformat()
        out.append({
            "company": c["name"],
            "id": str(j.get("id")),
            "title": (j.get("text") or "").strip(),
            "location": (cats.get("location") or "").strip(),
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "first_published": published,
            "source": "lever", "token": c["token"],
            "description": (j.get("descriptionPlain") or strip_html(j.get("description") or "")).strip(),
        })
    log(f"{c['name']}: {len(out)} jobs (lever)")
    return out


def fetch_jsearch(api_key):
    """
    Query JSearch (RapidAPI Google-for-Jobs aggregator) for each AGGREGATOR_QUERIES entry.
    Returns jobs in the same shape as ATS fetchers so the shared filter/rank path works.
    Skipped entirely (returns []) if api_key is absent or subscription inactive.

    Endpoint /search-v2 (verified live 28 Jun 2026). The old /search path now 404s at the
    RapidAPI gateway ("Endpoint '/search' does not exist"). v2 nests results under
    data.jobs (a list) instead of returning data as a list directly, and paginates by a
    data.cursor token — we only take page one for breadth, so no cursor handling needed.

    Field names verified live: job_id, job_title, employer_name, job_apply_link,
    job_city, job_state, job_country, job_posted_at_datetime_utc. NOTE: job_city/job_state
    come back in Arabic for Saudi roles and job_country is the ISO code "SA", so we expand
    the code to "Saudi Arabia" below — otherwise the English LOCATION_INCLUDE never matches.

    Full key list read off a live response by `--probe-jsearch` on 3 Sep 2026:
    apply_options, employer_logo, employer_name, employer_reviews, employer_website,
    job_apply_is_direct, job_apply_link, job_benefits, job_benefits_strings, job_city,
    job_country, job_description, job_employment_type(s), job_google_link,
    job_highlights, job_id, job_is_remote, job_latitude, job_location, job_longitude,
    job_max_salary, job_min_salary, job_onet_job_zone, job_onet_soc, job_posted_at,
    job_posted_at_datetime_utc, job_posted_at_timestamp, job_publisher, job_salary,
    job_salary_period, job_salary_string, job_state, job_title, job_uid.
    """
    if not api_key:
        return []

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }
    out = []
    seen_ids = set()  # dedupe within JSearch results across queries
    apply_field_logged = False   # the discovered field is named once per run, not per job
    quota = {}       # last reported allowance, read off every response
    cursors = {}     # page-one cursor per query, spent below if the budget allows

    def read_page(query, cursor=""):
        """One request. Returns (jobs, cursor); any failure is ([], "") and a log line."""
        url = (
            f"https://{JSEARCH_HOST}/search-v2"
            f"?query={requests.utils.quote(query)}&country=sa&date_posted=month"
        )
        if cursor:
            url += f"&cursor={requests.utils.quote(str(cursor))}"
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            log(f"WARN JSearch [{query!r}]: request failed: {e}")
            return [], ""
        # RapidAPI reports the remaining monthly allowance on every response.
        # Recorded on the way past so the deep read below can yield before the
        # reserve, and so the balance reaches the run log at all.
        if r.headers.get("x-ratelimit-requests-remaining") is not None:
            quota["remaining"] = r.headers["x-ratelimit-requests-remaining"]
            quota["limit"] = r.headers.get("x-ratelimit-requests-limit", "?")
        if r.status_code != 200:
            log(f"WARN JSearch [{query!r}]: HTTP {r.status_code}: {r.text[:120]}")
            return [], ""
        try:
            data = r.json()
        except ValueError:
            log(f"WARN JSearch [{query!r}]: non-JSON response")
            return [], ""
        # v2 shape: {"status":..,"data":{"jobs":[...],"cursor":..}}. Be defensive in case
        # a future response hands back a bare list again.
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            return payload, ""
        return payload.get("jobs", []), payload.get("cursor") or ""

    def collect(jobs):
        """Turn one page of raw JSearch objects into digest jobs. Returns how many."""
        nonlocal apply_field_logged
        added = 0
        for j in jobs:
            jid = str(j.get("job_id") or "")
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)

            # Build a location string that LOCATION_INCLUDE can match. City/state may be
            # Arabic; expand the ISO country code to an English name so "saudi" matches.
            city    = (j.get("job_city")    or "").strip()
            state   = (j.get("job_state")   or "").strip()
            country = (j.get("job_country") or "").strip()
            country = COUNTRY_CODE_TO_NAME.get(country.upper(), country)
            location = ", ".join(p for p in (city, state, country) if p)

            # The primary link is whichever publisher JSearch ranked first, and
            # that is frequently a paid relister -- 100 of the 174 aggregator
            # postings archived to 2026-08-29 sit on hosts the paid-relister cut
            # now discards. Any free alternative in the same response costs no
            # extra request, so it is read here and recorded as apply_url.
            # NEVER as url: job_identity keys a jsearch posting on its URL, so
            # rewriting it would make the whole back catalogue look new.
            #
            # MEASURED 3 Sep 2026, `--probe-jsearch` over two queries, 20 jobs:
            # apply_options exists and is shaped as expected (apply_link,
            # is_direct, publisher) but held exactly ONE entry on every single
            # job, always the same publisher as job_apply_link. Not one paywalled
            # posting carried a free alternative. So on this subscription the read
            # below recovers nothing today and rescue_apply_link's page hop is the
            # only route to a free link -- which is precisely why the equality
            # check further down matters: without it every job would take
            # apply_url = url and suppress that hop for the whole aggregator.
            # Kept because it costs no request, and a plan or API change that
            # starts returning real alternatives is then picked up for free; the
            # "N from the API" figure in the run summary is what says so.
            company = (j.get("employer_name") or "Unknown").strip()
            apply_field, apply_options = jsearch_apply_options(j)
            free_link = best_free_link(apply_options, company)
            if apply_field and not apply_field_logged:
                publishers = sorted({o["publisher"] for o in apply_options if o["publisher"]})
                log(f"JSearch apply options read from {apply_field!r}"
                    + (f" (publishers: {', '.join(publishers)[:120]})" if publishers else ""))
                apply_field_logged = True

            job = {
                "company": company,
                "id":      "jsearch_" + jid,   # prefix avoids id collision with ATS ids
                "title":   (j.get("job_title") or "").strip(),
                "location": location,
                "url":     j.get("job_apply_link") or j.get("job_google_link") or "",
                "first_published": (j.get("job_posted_at_datetime_utc") or "")[:10],
                "source": "jsearch", "token": "",
                "description": (j.get("job_description") or "").strip(),
                # Google's knowledge-panel guess at the employer's site, present
                # on 7 of 10 jobs. Never a link the candidate is given -- it is a
                # token candidate for the board discovery, which checks it
                # against the employer name before believing it.
                "employer_website": (j.get("employer_website") or "").strip(),
            }
            # An option that is simply the primary link again is not an
            # alternative. Recording it would make `apply_url` present on nearly
            # every posting, and `rescue_apply_link` would then have nothing
            # left to improve for a job whose employer board is still unfound.
            if free_link and canonical_url(free_link) != canonical_url(job["url"]):
                job["apply_url"] = free_link
            out.append(job)
            added += 1
        return added

    for query in AGGREGATOR_QUERIES:
        jobs, cursor = read_page(query)
        cursors[query] = cursor
        log(f"JSearch [{query!r}]: {len(jobs)} returned, {collect(jobs)} unique")
        time.sleep(0.5)  # be polite between queries

    # Page one alone is thirty postings over a 30-day window, so the same
    # thirty recirculate and every one is already in seen.json -- five straight
    # days delivered nothing new. Page two is worth reading: probed 3 Sep 2026,
    # all ten of it were absent from page one. It is rationed rather than always
    # read because the plan allows 200 requests a MONTH, and it rotates so no
    # query is permanently the shallow one.
    for query in jsearch_page_plan(AGGREGATOR_QUERIES, JSEARCH_DAILY_REQUESTS,
                                   datetime.date.today().toordinal()):
        if not cursors.get(query):
            continue
        if _quota_below_reserve(quota):
            log(f"JSearch quota at {quota.get('remaining')}; skipping the deep read "
                f"so the daily page-one fetch survives to the reset")
            break
        jobs, _ = read_page(query, cursors[query])
        log(f"JSearch [{query!r}] page 2: {len(jobs)} returned, {collect(jobs)} unique")
        time.sleep(0.5)

    if quota:
        log(f"JSearch quota: {quota['remaining']} of {quota['limit']} requests remaining")
    log(f"JSearch total: {len(out)} unique jobs across {len(AGGREGATOR_QUERIES)} queries")
    return out


def _quota_below_reserve(quota):
    """True when the month's allowance is too thin to spend on an extra page."""
    try:
        return int(quota.get("remaining")) < JSEARCH_QUOTA_RESERVE
    except (TypeError, ValueError):
        return False


def jsearch_page_plan(queries, budget, day):
    """Which queries get a second page today, given the day's request budget.

    The allowance is monthly, so the budget is a daily slice of it rather than
    a per-run convenience. Whatever it does not cover rotates, so a query is
    never permanently the one read shallowly.
    """
    extra = max(0, budget - len(queries))
    if not queries or extra <= 0:
        return []
    start = day % len(queries)
    return [queries[(start + i) % len(queries)] for i in range(min(extra, len(queries)))]


# VERIFIED 3 Sep 2026: /search-v2 does return `apply_options`, a list of
# {apply_link, is_direct, publisher}. The name-based scan below is kept anyway --
# JSearch has renamed fields across API versions before, and the scan costs
# nothing on a response that carries the expected name.
#
# JSearch names its alternative apply links differently across API versions, and
# /search-v2's field list was never verified here -- fetch_jsearch's docstring
# records only the fields checked live, and this is not one of them. Rather than
# hardcode a guess, the known names are tried first and any OTHER field whose
# name says it carries apply links is read after that. A field that does not say
# so is never read, however link-shaped its contents: what comes out of here is
# handed to the candidate. An unrecognised response yields nothing and the
# caller keeps the behaviour it has today.
JSEARCH_APPLY_OPTION_KEYS = ("apply_options", "apply_links")

_LINK_KEY_HINTS = ("link", "url")
_PUBLISHER_KEYS = ("publisher", "name", "source", "site")


def _apply_options_from(value):
    """Normalise one candidate field into [{"publisher", "url"}], or []."""
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = ""
        for key, candidate in item.items():
            if isinstance(candidate, str) and any(h in str(key).lower() for h in _LINK_KEY_HINTS):
                url = candidate.strip()
                break
        if not url:
            continue
        publisher = ""
        for key in _PUBLISHER_KEYS:
            if isinstance(item.get(key), str):
                publisher = item[key].strip()
                break
        found.append({"publisher": publisher, "url": url})
    return found


def jsearch_apply_options(raw_job):
    """Every apply link one JSearch job object offers, and the field that held them.

    Returns (field_name, options). The field name travels back so the run log can
    name it once: that line is how the apply_options expectation gets confirmed
    from a real run without a separate probe workflow.
    """
    if not isinstance(raw_job, dict):
        return "", []
    for key in JSEARCH_APPLY_OPTION_KEYS:
        options = _apply_options_from(raw_job.get(key))
        if options:
            return key, options
    # Only a field that NAMES itself an apply field is read past the known keys.
    # An unrestricted scan accepted any list of link-bearing dicts anywhere in
    # the response -- a "people also viewed" rail, a publisher list, a promo
    # block -- and whatever it found became the link on the card and the URL a
    # later Tailor CV click re-reads the JD from. The name is still not
    # hardcoded, so a future `apply_destinations` is picked up either way.
    for key, value in raw_job.items():
        if key in JSEARCH_APPLY_OPTION_KEYS or "apply" not in str(key).lower():
            continue
        options = _apply_options_from(value)
        if options:
            return str(key), options
    return "", []


def best_free_link(options, company=""):
    """The first apply link that does not cost money to act on, or "".

    Order is the publisher order JSearch returned; nothing here re-ranks by
    quality, because the only property this needs to decide is free vs paywalled.

    An ATS-shaped link is held to the same test `record_apply_link` applies to
    one read off a page: the board token has to agree with the employer name.
    Both paths end at the same `apply_url`, and a link to another company's
    posting is not caught downstream -- `verify_link` confirms it open because
    it is a real posting, and the resume is then tailored against it.
    """
    for option in options or []:
        url = ((option or {}).get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if on_paid_relister(url):
            continue
        reference = ats_reference("", url)
        if reference and not employer_matches_token(company, reference["token"]):
            log(f"JSearch apply option on {link_host(url)} names "
                f"'{reference['token']}', not {company}; ignoring it")
            continue
        return url
    return ""


def fetch_pinpoint(c):
    # Pinpoint job-board API: GET, no key, no pagination — one flat "data" array
    # (63 postings returned in a single response for Tabby). Verified live 23 Jul 2026.
    #
    # Pinpoint publishes NO create/publish date (the only date field is deadline_at,
    # which is null), so first_published stays "". That costs nothing: the field is
    # written by every fetcher and read by none, and freshness comes from seen.json
    # membership, not from any date.
    url = f"https://{c['token']}.pinpointhq.com/postings.json"
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        log(f"WARN {c['name']}: pinpoint HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        log(f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    out = []
    for j in data.get("data", []):
        loc = j.get("location") or {}
        # Combine city + country like fetch_workable does, so LOCATION_INCLUDE can match
        # either. Tabby's country reads "KSA", already covered by LOCATION_INCLUDE.
        location = ", ".join(p for p in (loc.get("city"), loc.get("name")) if p)
        out.append({
            "company": c["name"],
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": location,
            "url": j.get("url") or "",
            "first_published": "",
            # First source to publish structured pay. Soft signal only — never filters.
            "salary": pinpoint_salary(j),
            "source": "pinpoint", "token": c["token"],
            # Pinpoint splits the JD across three fields (verified live 31 Jul 2026)
            "description": "\n".join(strip_html(j.get(f) or "") for f in (
                "description", "key_responsibilities", "skills_knowledge_expertise")).strip(),
        })
    log(f"{c['name']}: {len(out)} jobs (pinpoint)")
    return out


def pinpoint_salary(j):
    """'6000-7500 SAR/month' when Pinpoint publishes a visible range, else ''."""
    if not j.get("compensation_visible"):
        return ""
    lo, hi = j.get("compensation_minimum"), j.get("compensation_maximum")
    cur = j.get("compensation_currency") or ""
    freq = j.get("compensation_frequency") or ""
    if lo is None and hi is None:
        return ""
    nums = "-".join(f"{v:,.0f}" for v in (lo, hi) if v is not None)
    return f"{nums} {cur}/{freq}".strip().rstrip("/")


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "lever": fetch_lever,
    "pinpoint": fetch_pinpoint,
}


def fetch_company(c):
    fn = FETCHERS.get(c["source"])
    if not fn:
        log(f"WARN {c['name']}: unknown source '{c['source']}'. Skipping.")
        return []
    return fn(c)


def location_conflict(title):
    """True when the TITLE says the job is really somewhere else, whatever the
    location metadata claims. The 2026-07-31 digest delivered Agoda's 'Staff
    Data Engineer (Bangkok based, relocation provided)' as Riyadh because
    JSearch's metadata said الرياض and matches_filters never read the title.
    A title that mentions being 'based' somewhere is trusted only if that
    somewhere is Saudi; any relocation offer means the job is not local."""
    t = title.lower()
    if "relocat" in t:
        return True
    if "based" in t and not any(k in t for k in LOCATION_INCLUDE):
        return True
    return False


def matches_filters(job):
    title = job["title"].lower()
    loc = job["location"].lower()
    if not any(k in loc for k in LOCATION_INCLUDE):
        return False
    if location_conflict(job["title"]):
        return False
    if any(k in title for k in TITLE_EXCLUDE):
        return False
    if not any(k in title for k in TITLE_INCLUDE):
        return False
    return True


def canonical_url(url):
    """A posting URL reduced to the parts that identify the posting."""
    split = urlsplit((url or "").strip())
    if not split.netloc:
        return ""
    kept = [(key, value) for key, value in parse_qsl(split.query)
            if key.lower() not in TRACKING_PARAMS]
    return urlunsplit((split.scheme.lower(), split.netloc.lower(),
                       split.path.rstrip("/"), urlencode(sorted(kept)), ""))


def job_identity(job):
    """The key seen.json remembers one posting by, stable across runs.

    Only a source with a volatile id changes shape here. An ATS job still keys
    on its bare id, which is what every entry already in seen.json uses, so no
    previously delivered role reappears because of this.
    """
    if job.get("source") in VOLATILE_ID_SOURCES:
        url = canonical_url(job.get("url"))
        if url:
            return f"url:{url}"
    return job.get("id") or f"url:{canonical_url(job.get('url'))}"


def link_host(url):
    """A posting URL's host, lowercased and without a leading 'www.'."""
    host = urlsplit((url or "").strip()).netloc.lower()
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]  # drop credentials and port
    return host[4:] if host.startswith("www.") else host


def on_paid_relister(url):
    """True when this link only reaches the posting through a paid wrapper."""
    host = link_host(url)
    return any(host == blocked or host.endswith("." + blocked)
               for blocked in PAID_RELISTER_HOSTS)


# Legal forms a board appends or omits at will. Stripped only from the end, and
# never down to nothing, so "Co" as a company name survives.
COMPANY_LEGAL_SUFFIXES = ("co", "ltd", "llc", "inc", "corp", "plc", "gmbh",
                          "limited", "company", "sdn", "bhd", "jsc", "wll")


def company_key(name):
    """An employer name reduced to what a job board cannot decorate.

    JSearch returns "Zid | زد" and "Qiddiya | القدية" where the employer's own
    ATS returns "Zid" and "Qiddiya", and "JAL International Co. Ltd." where
    another board would write "JAL International". The Arabic half, the
    separator and the legal form all vary by board; the latin core does not.
    """
    latin = "".join(ch for ch in (name or "").split("|")[0] if ch.isascii())
    words = re.findall(r"[a-z0-9]+", latin.casefold())
    while len(words) > 1 and words[-1] in COMPANY_LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


def employer_matches_token(company, token):
    """True when an ATS board token plausibly belongs to THIS employer.

    A board token is the employer's own slug, so it is a prefix, a suffix or
    the whole of the name far more often than not: Foodics is `foodics`, Lean
    is `LeanTech`, Example Labs is `examplelabs`. Containment either way is
    therefore the test, with a three-character floor so a one-letter name
    cannot match everything.

    A wrong answer here is asymmetric, which is why the test is loose rather
    than exact: a false negative costs one rescue and the candidate keeps the
    link they already had, while a false positive hands over another company's
    job. Anything the token cannot vouch for is refused.
    """
    name = company_key(company)
    slug = "".join(re.findall(r"[a-z0-9]+", (token or "").casefold()))
    if len(name) < 3 or len(slug) < 3:
        return False
    return name in slug or slug in name


def posting_key(job):
    """The employer-and-role identity two different boards agree on.

    Empty when either half is missing, because a key of "::" would group every
    incomplete card in the run into one posting.
    """
    company = company_key(job.get("company"))
    title = " ".join(re.findall(r"[a-z0-9]+", (job.get("title") or "").casefold()))
    return f"{company}::{title}" if company and title else ""


# The sources that ARE the employer: a link from one of these is the company's
# own posting, which is the link this digest should always prefer to hand over.
EMPLOYER_SOURCES = frozenset(FETCHERS)


def prefer_employer_postings(jobs):
    """Split off aggregator copies of a posting this run read from the employer.

    Returns (kept, dropped). The URL dedupe in main() cannot catch these:
    Foodics' "Senior Data Scientist / Senior ML Engineer" arrived from Workable
    on 2026-08-11 as apply.workable.com/foodics/j/E2707EDFCE/, then from JSearch
    on 08-17, 08-18 and 08-19 as a jobleads.com link -- byte-identical title,
    different URL, so nothing joined them and the paid copy went out three more
    times. The employer's own board is the better link and this run already
    holds it, so the relisted copy is redundant, not new.
    """
    employer_keys = {posting_key(job) for job in jobs
                     if job.get("source") in EMPLOYER_SOURCES}
    employer_keys.discard("")
    kept, dropped = [], []
    for job in jobs:
        key = posting_key(job)
        if key and key in employer_keys and job.get("source") not in EMPLOYER_SOURCES:
            dropped.append(job)
        else:
            kept.append(job)
    return kept, dropped


def description_key(job):
    """The seen.json key for a description within one employer and role."""
    text = (job.get("description") or "").strip()
    company = " ".join(str(job.get("company") or "").casefold().split())
    title = " ".join(str(job.get("title") or "").casefold().split())
    if not text or not company or not title:
        return ""
    fingerprint = "\n".join((company, title, text))
    return JD_SEEN_PREFIX + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def drop_repeat_descriptions(jobs, seen):
    """Split off postings whose role-scoped description was already delivered.

    A stable URL catches almost everything, but an aggregator can relist the
    same job under a new URL of its own: Jooble did it once in the first 11
    archived days, minting /jdp/-514809098470354554 and /jdp/6609179533026884564
    for one posting. The description plus normalized company and title is the
    last identity left that does not move. It runs after enrichment, when all
    three fields exist.
    """
    kept, repeats = [], []
    for job in jobs:
        key = description_key(job)
        (repeats if key and key in seen else kept).append(job)
    return kept, repeats


def keyword_score(job):
    title = job["title"].lower()
    hits = sum(1 for k in PROFILE["skill_keywords"] if k in title)
    return min(100, 40 + hits * 15)  # 40 base for passing filters, +15 per skill hit


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["id", "score", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scores"],
    "additionalProperties": False,
}


def rank_jobs(jobs, api_key):
    """One call. Returns {id: {score, why}} or None on any failure (caller falls back).

    The 2026-07-31 digest handed Lead/Staff roles 92-98 because the old prompt
    had no rubric: it asked for a 0-100 with no anchor, so everything AI-flavored
    scored 90+. The rubric below pins the bands and makes seniority-above-target
    a hard penalty.

    What the rubric could not fix on its own is what it was scoring. The excerpt
    used to be the first 900 characters of the posting, which on a McKinsey
    posting is the culture section and nothing else, and the model duly reported
    that the description named no technical requirements — across seven days and
    a 35-point spread on a byte-identical description. It reads the
    requirements section now, and the listing is sorted so a score no longer
    depends on which other jobs happened to be new that day.
    """
    profile = PROFILE["blurb"]
    targets = PROFILE.get("target_titles")
    if targets:
        profile += " Currently targeting: " + ", ".join(targets) + "."
    listing = [
        {"id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"],
         "description_kind": jd_grade(j.get("description") or ""),
         "jd_excerpt": jd_signal(j.get("description") or "", JD_RANK_CHARS)}
        for j in sorted(jobs, key=lambda j: j["id"])
    ]
    prompt = (
        f"{profile}\n\n"
        "Score each job 0-100 for fit to this candidate. Anchor every score to this rubric:\n"
        "- 90-100: senior-level INDIVIDUAL CONTRIBUTOR role in a core area (AI/LLM apps, "
        "software/backend, full-stack, data) in Saudi Arabia, and the JD's hard requirements "
        "are clearly covered by the profile. Reserve 95+ for near-perfect matches.\n"
        "- 70-89: senior IC role in a core area with one or two real gaps against the JD.\n"
        "- 40-69: adjacent function, seniority mismatch, or several gaps.\n"
        "- 0-39: leadership/staff/principal/management track, wrong location (including a title "
        "that says based elsewhere or offers relocation), or wrong function.\n"
        "jd_excerpt starts where the posting begins describing the job, so it is the "
        "requirements and responsibilities, not the company's recruiting copy. Score against "
        "what it states. A requirement the excerpt does not mention is unknown, not a gap: "
        "never lower a score for something the posting simply did not say.\n"
        "description_kind tells you what you are reading. 'full' is a complete posting. "
        "'teaser' is a short aggregator summary and 'incomplete' is a fragment — for those two, "
        "judge on title and function alone and say so in your reason. Never score above 85 when "
        "jd_excerpt is empty: a title alone cannot prove fit.\n"
        "Give a one-line reason of at most 15 words naming the deciding factor.\n"
        f"Jobs:\n{json.dumps(listing, ensure_ascii=False)}"
    )
    try:
        answer = chat_json(prompt, RANK_SCHEMA, "job_fit_scores", api_key,
                           RANK_MODEL, RANK_FALLBACKS,
                           time.monotonic() + RANK_BUDGET_SECONDS,
                           provider=RANK_PROVIDER)
        return {
            str(it["id"]): {"score": int(it["score"]), "why": str(it["why"])[:140]}
            for it in answer["scores"]
        }
    except Exception as e:
        log(f"WARN ranking failed: {e}")
        return None


_workable_details_cache = {}


def get_description(job):
    """Best-effort JD text for one DELIVERED job, capped at JD_MAX_CHARS.

    Ashby, Lever, Pinpoint and JSearch ship the JD inside the board payload the
    fetcher already read, so those cost nothing. Greenhouse needs one GET per
    job (boards-api .../jobs/<id>, 'content' field); Workable needs one GET per
    company via the v1 widget API (?details=true), cached across jobs in the
    same run. Only delivered jobs reach here, so the extra requests stay
    bounded by the digest cap. Any failure returns whatever we have: tailoring
    degrades to title-only, the run never breaks."""
    if job.get("description"):
        # The text is settled, the link is not. This return is the path every
        # JSearch job takes, so leaving it here is what kept the apply-link
        # rescue from ever running on the postings it exists for.
        rescue_apply_link(job)
        return job["description"][:JD_MAX_CHARS]
    src, token = job.get("source"), job.get("token")
    try:
        if src == "greenhouse":
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job['id']}?questions=false")
            if r.status_code == 200:
                job["description"] = strip_html(r.json().get("content") or "")
            else:
                log(f"WARN JD fetch: greenhouse HTTP {r.status_code} for {job['title']}")
        elif src == "workable":
            if token not in _workable_details_cache:
                r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
                jobs = r.json().get("jobs", []) if r.status_code == 200 else []
                _workable_details_cache[token] = {
                    w.get("shortcode"): strip_html(w.get("description") or "") for w in jobs
                }
            job["description"] = _workable_details_cache.get(token, {}).get(job.get("shortcode"), "")
        else:
            job["description"] = fetch_posting_page(job)
    except (requests.RequestException, ValueError) as e:
        log(f"WARN JD fetch failed for {job['company']} — {job['title']}: {e}")
    return (job.get("description") or "")[:JD_MAX_CHARS]


def fetch_posting_page(job):
    """Last-resort JD text for a posting only an aggregator linked to.

    Aggregators like Jooble hand over a title and a URL and nothing else, so
    these jobs used to reach the ranker with an empty jd_excerpt. The rubric
    then correctly refuses to score a title above 85 without evidence, and a
    real match lands mid-band: the Jobgether ML role scored 55 on 2026-08-05
    while its own JD supported 85.

    Stripping tags off the landing page closed only part of that gap, because
    the description is usually not in the body text at all: it is in the page's
    JobPosting metadata, or on the employer's own ATS that the aggregator is
    relisting. `jd_fetch.extract_posting` tries both before falling back to the page
    text this used to be, and applies the same completeness gate, so a
    bot-blocked page still scores exactly as it does today rather than on noise.

    It also reports the employer's own posting URL when the page names one, and
    that is recorded on the job here. This branch gets it for free -- the hop is
    being made for the description anyway -- which is why the description-less
    jobs come through here and `rescue_apply_link` exists separately for the
    ones that pay for it.
    """
    posting = extract_posting(job.get("url", ""), get=_get, limit=JD_MAX_CHARS, log=log)
    record_apply_link(job, posting["employer_url"], posting["employer_token"])
    return posting["text"]


def record_apply_link(job, employer_url, employer_token):
    """Accept the employer posting a page named, if it is THIS employer's.

    `ats_reference` matches any ATS-shaped href on the page, and a relister's
    "people also viewed" rail is full of them, so an unguarded rescue can hand
    over a live posting at a different company. Nothing downstream would catch
    it: `verify_link` confirms it open because it is a real posting, and
    `make_snapshot` records it as the URL a later "Tailor CV" click re-reads
    the JD from -- so the resume would be tailored against the wrong job. The
    board token has to agree with the employer name before the link is
    believed.

    Setting `apply_url` rather than overwriting `url` is deliberate:
    `job_identity` keys an aggregator posting on its URL, so rewriting it would
    make every rescued job look new on the next run and redeliver it daily for
    as long as the rescue kept working.
    """
    if not employer_url or canonical_url(employer_url) == canonical_url(job.get("url")):
        return
    if not employer_matches_token(job.get("company"), employer_token):
        log(f"Apply link on {link_host(employer_url)} names '{employer_token}', not "
            f"{job['company']}; keeping {link_host(job.get('url'))}")
        return
    job["apply_url"] = employer_url
    log(f"Apply link rescued from {link_host(job.get('url'))} to "
        f"{link_host(employer_url)}: {job['company']} — {job['title']}")


def rescue_apply_link(job):
    """Find the employer's own posting for a job that already has its text.

    `fetch_posting_page` only runs when the board shipped no description, and
    JSearch always ships one -- so the rescue reached none of the 171 aggregator
    postings archived to 2026-08-28, including the 48 that arrived as a teaser
    plus a relister link, which is the case it was built for. This is the same
    lookup run for its link rather than its text.

    Unlike the hop inside `fetch_posting_page` this is a NEW request: one GET
    per aggregator job, bounded by the fresh-job count. It is skipped for jobs
    read straight from an employer's board, whose URL is already the answer.
    Any failure leaves the job with the link it arrived with.
    """
    url = job.get("url", "")
    if not url or job.get("source") in EMPLOYER_SOURCES:
        return
    if job.get("apply_lookup_done"):
        # This run already made the request. get_description calls this function
        # too, so without the guard a job rescued before the paid-relister cut
        # pays for a second identical GET a few steps later. The guard is on the
        # LOOKUP, not on `apply_url`: a link JSearch handed over is not the
        # employer's board, and keying on it would skip the lookup for every
        # aggregator posting that arrived with one -- which is most of them.
        return
    job["apply_lookup_done"] = True
    try:
        response = _get(url)
    except (requests.RequestException, ValueError) as e:
        log(f"WARN apply-link lookup failed for {job['company']} — {job['title']}: {e}")
        return
    page_html = ""
    if response.status_code == 200:
        page_html = response.text
    else:
        # Probed live on 2026-08-29: Glassdoor, Jooble and learn4good all answer
        # 403 to a datacenter IP, and a runner fares no better than the laptop
        # that tried it. Without this line a blocked page and a page that simply
        # names no board are the same silence in the log, and there is no way to
        # tell a rescue that found nothing from one that never ran.
        log(f"WARN apply-link page HTTP {response.status_code} for {link_host(url)}: "
            f"{job['company']} — {job['title']}")
    # Still worth a look with no page in hand: an aggregator URL is sometimes
    # already an ATS posting, which ats_reference reads off the URL itself.
    reference = ats_reference(page_html, url)
    record_apply_link(job, ats_url(reference), (reference or {}).get("token") or "")


def apply_link(job):
    """The link the candidate should be handed: the employer's, if we found it."""
    return job.get("apply_url") or job.get("url", "")


def settle_link(job):
    """Check the link this job would actually be delivered on, and record it.

    Verification used to run on `job["url"]` while the paid-relister cut kept
    the job on `apply_link`, so a posting could be judged on a URL the candidate
    is never handed. It checks `apply_link` now.

    A rescued `apply_url` that answers a definite 404 is discarded rather than
    believed: it is built from a token and a posting id, so a negative is as
    likely to mean the shape was wrong as that the posting closed. The link the
    job arrived with is then checked in its own right, because it may never have
    been -- and the caller has to look at it again, since it can be the paid
    relister the cut let through only on the strength of the rescue.
    """
    status = verify_link(apply_link(job))
    if status == LINK_CLOSED and job.get("apply_url"):
        log(f"Rescued link did not resolve, keeping the original: {job['apply_url']}")
        job.pop("apply_url")
        status = verify_link(apply_link(job))
    job["link_status"] = status
    job["link_checked"] = apply_link(job)
    return status


def enrich_descriptions(jobs):
    """Populate bounded JD text for jobs that survived the hard filters."""
    for job in jobs:
        job["description"] = get_description(job)


def apply_ranking(jobs, ranking):
    """Attach validated model scores or deterministic fallbacks to every job."""
    for job in jobs:
        result = ranking.get(job["id"]) if isinstance(ranking, dict) else None
        try:
            score = int(result["score"])
            why = str(result["why"]).strip()[:140]
            valid = 0 <= score <= 100 and bool(why)
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            job.update(score=score, why=why, ranking_verified=True)
        else:
            job.update(
                score=keyword_score(job),
                why="matches your target titles and skills",
                ranking_verified=False,
            )


def cap_teaser_scores(jobs):
    """Hold a job scored off a marketing blurb out of the strong tier.

    An aggregator teaser clears every completeness check — 400 characters, 60
    words, the word "experience" — while evidencing nothing. The ranker is told
    what it is reading and asked to judge on title alone, but a title can look
    like a perfect match, and "strong match" has to mean the description backed
    it up. Returns the jobs it capped."""
    capped = []
    for job in jobs:
        if jd_grade(job.get("description") or "") != GRADE_TEASER:
            continue
        if job["score"] <= TEASER_SCORE_CAP:
            continue
        job["score"] = TEASER_SCORE_CAP
        job["why"] = f"{job['why']} (capped: description is a short teaser)"[:140]
        capped.append(job)
    return capped


def remember(seen, job, today):
    """Record a posting so no later run reconsiders it.

    Both the identity and the description are keyed, because an aggregator
    relists the same posting under a fresh URL and only the description
    fingerprint catches that.
    """
    seen[job_identity(job)] = today
    key = description_key(job)
    if key:
        seen[key] = today


def drop_below_floor(jobs):
    """Split off jobs the ranker scored below the delivery floor.

    Returns (kept, dropped). A job is only ever dropped on a verified model
    score: `apply_ranking` falls back to `keyword_score` whenever the ranker is
    missing or its answer fails validation, and those numbers are calibrated for
    ordering, not for a pass/fail line. Dropping on them would turn a ranker
    outage into an empty digest with no error anywhere.
    """
    kept, dropped = [], []
    for job in jobs:
        if job.get("ranking_verified") and job["score"] < MIN_DELIVER_SCORE:
            dropped.append(job)
        else:
            kept.append(job)
    return kept, dropped


def split_tiers(jobs):
    """Separate high-confidence JD-backed model matches from review candidates."""
    strong = [
        job for job in jobs
        if job["ranking_verified"]
        and job["score"] >= STRONG_MATCH_MIN_SCORE
        and bool(job.get("description"))
    ]
    review = [job for job in jobs if job not in strong]
    return strong, review


# Substrings that a closed/expired posting page shows. Checked case-insensitively
# against the fetched body so a link that 200s but says "no longer accepting" is
# still treated as dead. Kept broad on purpose: better to re-check a live job than
# to deliver a dead one.
CLOSED_MARKERS = [
    "this job is no longer",
    "no longer accepting applications",
    "position is no longer available",
    "job is no longer available",
    "this position has been filled",
    "posting is not available",
    "job posting not found",
    "no longer open",
    "position has been closed",
]


def verify_link(url):
    """
    Confirm a posting URL still resolves to an OPEN posting before it's delivered.
    Returns LINK_OPEN, LINK_CLOSED, or LINK_UNKNOWN.

    Fail-soft on the network: a request error returns True (keep the job). The
    fetch that produced this URL succeeded moments ago, so a transient error here
    is far more likely than the posting vanishing in that window — dropping on a
    blip would hide a real, live role. Only a definite negative (non-200, or a
    closed marker in the body) drops the job.
    """
    if not url:
        return LINK_CLOSED
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN verify {url}: request error ({e}); status unknown")
        return LINK_UNKNOWN
    if r.status_code not in (200, 404, 410):
        log(f"WARN verify {url}: HTTP {r.status_code}; status unknown")
        return LINK_UNKNOWN
    if r.status_code != 200:
        log(f"DROP dead link HTTP {r.status_code}: {url}")
        return LINK_CLOSED
    # Several ATSs (Greenhouse confirmed) DON'T 404 a pulled posting — they 302 it
    # off the job page back to the board root or an ?error=true page, still 200. So
    # a redirect that drops the job's own id from the final URL means the posting is
    # gone. Live postings don't redirect, or keep their id. No redirect -> skip this.
    final = (r.url or "").lower()
    job_tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
    # Greenhouse (?error=true) and Workable (?not_found=true) both bounce a pulled
    # posting off the job page, dropping its id from the final URL.
    if r.history and ("error=true" in final or "not_found=true" in final
                      or (job_tail and job_tail not in final)):
        log(f"DROP redirected off posting -> {r.url}: {url}")
        return LINK_CLOSED
    body = (r.text or "").lower()
    # Ashby doesn't redirect a pulled posting — it serves a 200 board shell with no
    # posting data. A real Ashby posting embeds a JobPosting JSON-LD schema; the
    # empty shell doesn't. Scoped to the host so this positive-proof gate never
    # touches other ATSs' pages (which legitimately lack that schema).
    if "ashbyhq.com" in final and "jobposting" not in body:
        log(f"DROP ashby posting gone (no JobPosting schema): {url}")
        return LINK_CLOSED
    for m in CLOSED_MARKERS:
        if m in body:
            log(f"DROP closed posting ({m!r}): {url}")
            return LINK_CLOSED
    return LINK_OPEN


def load_seen():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except ValueError:
        log("WARN seen.json is corrupt, starting fresh")
        return {}


def save_seen(seen):
    cutoff = (datetime.date.today() - datetime.timedelta(days=STATE_TTL_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)


# ---------------------- EMPLOYER BOARD DISCOVERY ----------------------------
# The paid relisters block this runner outright -- 403 from jooble and
# jobrapido, 404 from jobleads on the 3 Sep 2026 run -- so rescue_apply_link's
# page hop cannot reach these postings, and JSearch's apply_options carries no
# alternative either (probed the same day: one option per job, always the same
# publisher as the primary link). Eight matching postings a day were being
# discarded with no route to a free link at all.
#
# What the response does carry is the employer's name and, on 7 of 10 jobs, a
# website. Probed against the five supported ATSs those reach the employer
# without touching the relister. A confirmed board is remembered here and read
# by fetch_company from the next run on -- at which point prefer_employer_postings
# drops the relisted copy on its own, and the JD arrives complete instead of as
# a teaser. Nothing about the link path changes; the posting simply arrives
# from a better source.
BOARDS_FILE = "boards.json"
MAX_BOARD_PROBES = 5          # employers probed per run; the rest wait for tomorrow
BOARD_MISS_RETRY_DAYS = 30    # a company with no board in September may have one later


def load_boards():
    if not os.path.exists(BOARDS_FILE):
        return {}
    try:
        with open(BOARDS_FILE, encoding="utf-8") as f:
            found = json.load(f)
        return found if isinstance(found, dict) else {}
    except ValueError:
        log("WARN boards.json is corrupt, starting fresh")
        return {}


def save_boards(boards):
    with open(BOARDS_FILE, "w", encoding="utf-8") as f:
        json.dump(boards, f, indent=2, sort_keys=True, ensure_ascii=False)


def board_token_candidates(job):
    """The tokens this employer's own board might live under.

    The name first, because it is what the digest already trusts elsewhere; then
    its first word, because a board token is the short name and not the trading
    one -- the live probe on 3 Sep 2026 found exequt MENA under 'exequt' and
    Silver Edge Arabia under 'silveredge'; then the registrable label of
    employer_website, for the employers whose token follows the domain instead.
    """
    candidates = []
    name = company_key(job.get("company"))
    if len(name) >= 3:
        candidates.append(name)
    words = re.findall(r"[a-z0-9]+", (job.get("company") or "").split("|")[0].casefold())
    if words and len(words[0]) >= 3 and words[0] not in candidates:
        candidates.append(words[0])
    host = link_host(job.get("employer_website"))
    label = "".join(re.findall(r"[a-z0-9]+", host.split(".")[0].casefold())) if host else ""
    if len(label) >= 3 and label not in candidates:
        candidates.append(label)
    return candidates


# Greenhouse and Workable publish the board owner's name; Ashby's posting API
# returns only {jobs, apiVersion}, and Lever and Pinpoint are read here for
# postings rather than identity. Where a name exists it settles the question a
# token guess cannot.
BOARD_NAME_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}",
}


def board_identity(source, token):
    """The name a board publishes for itself, or "" when it publishes none."""
    url = BOARD_NAME_URLS.get(source)
    if not url:
        return ""
    try:
        response = _get(url.format(token=token))
        if response.status_code != 200:
            return ""
        return str(response.json().get("name") or "").strip()
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return ""


def _board_is_this_employer(job, source, token):
    """Whether a probe hit can be believed as THIS employer's board.

    A token that merely contains, or is contained by, the employer name is a
    guess. Live on 3 Sep 2026 that guess sent 'latitude' to a greenhouse board
    of automotive service technicians and 'silver' to an Argentine recruiter on
    ashby, both from the first word of a longer employer name.

    So the board has to corroborate it: the name it publishes for itself must
    agree with the employer, and where it publishes none the token has to BE
    the employer's whole key rather than a shortening of it.
    """
    company = job.get("company")
    name = board_identity(source, token)
    if name:
        return employer_matches_token(company, name)
    return token == company_key(company)


def _probe_employer_board(job):
    """The first ATS board a candidate token reaches, or None."""
    for token in board_token_candidates(job):
        # employer_website is Google's knowledge-panel guess, not a verified
        # field: it gave siccode.com for "Business Codes" and infinitiusa.com
        # for "Infinniti". Checked BEFORE probing, so a token the employer name
        # cannot vouch for costs no requests either.
        if not employer_matches_token(job.get("company"), token):
            continue
        for source in PROBES:
            found, count = _probe(source, token)
            if found and _board_is_this_employer(job, source, token):
                return {"source": source, "token": token, "roles": count}
            if found:
                log(f"Board {source}/{token} does not belong to "
                    f"{job.get('company')}; ignoring it")
            time.sleep(0.2)  # be polite across ATSs
    return None


def discover_boards(jobs, boards, today):
    """Look for an employer board behind every posting the paid cut discarded.

    Records the answer either way. A miss is remembered so the same fruitless
    five requests are not spent on the same company every morning -- these
    postings are never written to seen.json, so they return every single day.
    """
    stale = (datetime.date.fromisoformat(today)
             - datetime.timedelta(days=BOARD_MISS_RETRY_DAYS)).isoformat()
    probed = 0
    for job in jobs:
        key = company_key(job.get("company"))
        if not key:
            continue
        known = boards.get(key)
        if known and (known.get("source") or known.get("miss", "") > stale):
            continue
        if probed >= MAX_BOARD_PROBES:
            break
        probed += 1
        company = (job.get("company") or "").strip()
        board = _probe_employer_board(job)
        if board:
            boards[key] = dict(board, company=company, found=today)
            state = f"{board['roles']} open roles" if board["roles"] else "0 open roles"
            log(f"Board found for {company}: {board['source']}/{board['token']} "
                f"({state}); its postings arrive from the source next run")
        else:
            boards[key] = {"company": company, "miss": today}
    return probed


def confirmed_boards(boards):
    """Every discovered board as a COMPANIES entry, minus the ones already there."""
    already = {(c["source"], c["token"]) for c in COMPANIES}
    out = []
    for record in boards.values():
        if not record.get("source"):
            continue
        pair = (record["source"], record["token"])
        if pair in already:
            continue
        already.add(pair)
        out.append({"name": record.get("company") or record["token"],
                    "source": record["source"], "token": record["token"]})
    return out
# ----------------------------------------------------------------------------


def load_stats():
    if not os.path.exists(STATS_FILE):
        return []
    try:
        with open(STATS_FILE) as f:
            data = json.load(f)
    except ValueError:
        log("WARN stats.json is corrupt, starting fresh")
        return []
    return data if isinstance(data, list) else []


def save_stats(stats, today, fetched, matched, new, avg_score, **metrics):
    """Record today's run and prune rows older than STATS_TTL_DAYS.
    avg_score is None on a no-new-jobs day (nothing was scored).

    One row per DATE, not per run. The workflow has a manual dispatch button,
    and 2026-08-08 was re-run four times, so appending gave a single day three
    of the seven slots in check_drift's trailing window. A re-run now replaces
    that day's row."""
    row = {
        "date": today, "fetched": fetched, "matched": matched,
        "new": new, "avg_score": avg_score,
    }
    row.update(metrics)
    cutoff = (datetime.date.today() - datetime.timedelta(days=STATS_TTL_DAYS)).isoformat()
    pruned = [s for s in stats if s["date"] >= cutoff and s["date"] != today]
    pruned.append(row)
    pruned.sort(key=lambda s: s["date"])
    with open(STATS_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)


def check_drift(stats):
    """Compare the trailing DRIFT_WINDOW_DAYS avg fit score against the prior
    window of the same size, using history recorded BEFORE today's run. Returns
    a warning string, or None if there isn't enough history yet or fit hasn't
    dropped past the threshold. Fail-soft: never raises, never blocks delivery.

    Collapses any duplicate dates already on disk (written before save_stats
    enforced one row per date) so an old, heavily re-run day cannot fill the
    window on its own."""
    by_date = {}
    for s in stats:
        if s.get("avg_score") is not None:
            by_date[s["date"]] = s
    scored = sorted(by_date.values(), key=lambda s: s["date"])
    if len(scored) < DRIFT_MIN_SCORED_RUNS:
        return None
    recent = scored[-DRIFT_WINDOW_DAYS:]
    prior = scored[-2 * DRIFT_WINDOW_DAYS:-DRIFT_WINDOW_DAYS]
    recent_avg = sum(s["avg_score"] for s in recent) / len(recent)
    prior_avg = sum(s["avg_score"] for s in prior) / len(prior)
    if prior_avg - recent_avg >= DRIFT_SCORE_DROP_THRESHOLD:
        return (
            f"Fit trending down ({DRIFT_WINDOW_DAYS}d avg {recent_avg:.0f} vs "
            f"prior {prior_avg:.0f}) — profile may be stale. Check profile.json."
        )
    return None


def instability_warning(unstable):
    """One line naming the worst scores that moved on an unchanged posting.

    Reported rather than silently corrected: the fix for an unstable ranker is
    a better prompt or a better model, and this is how you find out whether the
    last change to either actually worked."""
    if not unstable:
        return None
    worst = unstable[0]
    scored = "-".join(str(s) for s in sorted(worst["scores"]))
    return (
        f"{len(unstable)} posting(s) scored differently on an unchanged description "
        f"in the last {ARCHIVE_INSTABILITY_DAYS}d — worst: {worst['title'][:60]} "
        f"({scored}). The ranker, not the market."
    )


def post_heartbeat(webhook, worker_url, worker_secret, today, scanned, matched, warnings):
    """Confirm a quiet day out loud.

    A green workflow run with no new roles used to exit silently, which is
    indistinguishable from a broken job. matched-but-seen is shown so you know
    the pipeline reached real jobs and just had nothing new to add.
    """
    lines = [
        f"**Job digest — {today}** · no new roles today. "
        f"Scanned {scanned} postings across {len(COMPANIES)} companies, "
        f"{matched} matched your filters (already sent), 0 new."
    ]
    lines.extend(f"⚠️ {warning}" for warning in warnings if warning)
    if worker_url:
        post_interactive_cards(
            worker_url,
            worker_secret,
            [],
            heartbeat={"key": f"heartbeat:{today}", "content": "\n".join(lines)},
        )
    else:
        post_discord(webhook, lines)


def post_discord(webhook, lines):
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > 1900:  # Discord hard limit is 2000
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        chunks.append(cur)
    for ch in chunks:
        r = requests.post(webhook, json={"content": ch}, timeout=30)
        if r.status_code not in (200, 204):
            log(f"ERROR Discord HTTP {r.status_code}: {r.text[:200]}")
            sys.exit(1)  # fail loud
        time.sleep(1)


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    worker_url = os.environ.get("INTERACTIVE_WORKER_URL")
    worker_secret = os.environ.get("WORKER_CALLBACK_SECRET")
    if not webhook and not worker_url:
        log("FATAL: DISCORD_WEBHOOK_URL or INTERACTIVE_WORKER_URL must be set")
        sys.exit(1)
    if worker_url and not worker_secret:
        log("WARN INTERACTIVE_WORKER_URL set without WORKER_CALLBACK_SECRET; using plain webhook")
        worker_url = ""
    rank_key = os.environ.get("OPENROUTER_API_KEY")  # optional

    seen = load_seen()
    boards = load_boards()
    stats = load_stats()
    drift_warning = check_drift(stats)  # uses history only; today's row isn't recorded yet
    # Same rule: read only what previous runs committed, before today's archive
    # is written, so the warning describes history rather than this run.
    ranker_warning = instability_warning(score_instability(window_days=ARCHIVE_INSTABILITY_DAYS))
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")

    all_jobs = []

    # Breadth: JSearch aggregator (LinkedIn/Indeed/Glassdoor/company sites via Google for Jobs)
    all_jobs.extend(fetch_jsearch(rapidapi_key))

    # Depth: direct company ATS feeds (Greenhouse, Ashby, Workable, Lever).
    # COMPANIES is the hand-curated list; the boards after it were discovered
    # from employers the paid-relister cut discarded on an earlier run. Reading
    # them here is the entire payoff of that discovery: the posting arrives from
    # the employer with a free link and a complete JD, and
    # prefer_employer_postings drops the relisted copy without being told.
    for c in COMPANIES + confirmed_boards(boards):
        all_jobs.extend(fetch_company(c))
        time.sleep(0.5)

    # dedupe within the run (same posting could appear twice)
    uniq = {}
    for j in all_jobs:
        uniq[canonical_url(j.get("url")) or job_identity(j)] = j
    all_jobs = list(uniq.values())

    matched = [j for j in all_jobs if matches_filters(j)]

    # Two link-quality passes. Both run here, before the freshness check, so a
    # posting dropped for its link never reaches verification, the ranker or
    # Discord -- and both run on `matched` rather than all_jobs, so a relisted
    # copy is never dropped in favour of an employer copy the filters rejected.
    matched, relisted_copies = prefer_employer_postings(matched)
    for job in relisted_copies:
        log(f"Relisted copy on {link_host(job['url'])}: {job['company']} — "
            f"{job['title']}; this run already read it from the employer's board")
    # Last chance before the cut below discards the job. This used to run inside
    # enrich_descriptions, three steps after the drop, so the postings it exists
    # for -- an aggregator link to a posting that lives on a free employer board
    # -- were already gone. It runs on `matched` rather than `fresh` because the
    # cut precedes the freshness check; the cost is bounded by the number of
    # paywalled jobs in a run (7 on 2026-08-31).
    # Counted as recoveries, not as links: a free alternative beside a link that
    # was never paywalled rescued nothing, and counting it made a run where the
    # feature did no work read like a run where it did plenty.
    api_rescued = sum(1 for j in matched
                      if j.get("apply_url") and on_paid_relister(j.get("url")))
    page_rescued = 0
    for job in matched:
        if not on_paid_relister(apply_link(job)):
            continue
        rescue_apply_link(job)
        if not on_paid_relister(apply_link(job)):
            page_rescued += 1

    # Judge the link the candidate is actually handed, not the one the
    # aggregator happened to rank first. apply_link prefers apply_url, which is
    # where both the API's free alternative and the page rescue put their result
    # -- testing j["url"] here discarded jobs whose free link was already known.
    paid_only = [j for j in matched if on_paid_relister(apply_link(j))]
    matched = [j for j in matched if not on_paid_relister(apply_link(j))]
    for job in paid_only:
        log(f"Paid relister {link_host(job['url'])}: {job['company']} — {job['title']}")
    log(f"apply links: {api_rescued} from the API, {page_rescued} rescued from the page, "
        f"{len(paid_only)} still paywalled and dropped")

    # Neither rescue can reach these: the relister blocks the runner and the API
    # offers no alternative. Their employers can still be reached directly, so
    # look for a board now and read it from the next run on. Runs before the
    # freshness check because a paid-relister drop is never remembered -- these
    # same employers are back tomorrow whatever happens here.
    if discover_boards(paid_only, boards, datetime.date.today().isoformat()):
        save_boards(boards)

    # A paid-relister drop is deliberately NOT remembered in seen.json. Unlike
    # the delivery floor, this is a verdict on the link, not on the role: the
    # same posting appearing on the employer's own board tomorrow should be
    # delivered tomorrow.
    fresh = [j for j in matched if job_identity(j) not in seen]
    log(f"{len(all_jobs)} fetched, {len(matched)} match filter, "
        f"{len(relisted_copies)} relisted copies, {len(paid_only)} paid-relister only, "
        f"{len(fresh)} new")

    # Verification is evidence, not a boolean gate: a third-party site may block
    # this runner while still serving a job seeker. Only confirmed closure drops.
    for j in fresh:
        settle_link(j)
    checked = len(fresh)  # both filters below shrink `fresh`; the ratio needs this
    closed = [j for j in fresh if j["link_status"] == LINK_CLOSED]
    fresh = [j for j in fresh if j["link_status"] != LINK_CLOSED]
    # settle_link may have dropped a rescue that did not resolve, and a job the
    # cut above kept ONLY because that rescue was free is back on the paywalled
    # link the cut rejected. Delivering it there is the outcome the cut exists
    # to prevent, so the job goes with its rescue. Like every paid-relister
    # drop it is not remembered: the same posting on a free board tomorrow is
    # still worth delivering.
    lost_rescue = [j for j in fresh if on_paid_relister(apply_link(j))]
    fresh = [j for j in fresh if not on_paid_relister(apply_link(j))]
    for job in lost_rescue:
        log(f"Paid relister {link_host(job['url'])} again after the rescued link "
            f"failed: {job['company']} — {job['title']}")
    unknown_count = sum(j["link_status"] == LINK_UNKNOWN for j in fresh)
    if closed:
        log(f"{len(closed)} of {checked} new jobs confirmed closed")

    today = datetime.date.today().isoformat()

    warnings = [drift_warning, ranker_warning]

    if not fresh:
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings)
        save_stats(
            stats, today, len(all_jobs), len(matched), 0, None,
            link_closed=len(closed), link_unknown=unknown_count,
            strong=0, review=0, tailored=0, tailor_failed=0,
        )
        log("No new matching jobs. Heartbeat sent.")
        return

    enrich_descriptions(fresh)

    # A rescued apply link is BUILT from a token and a posting id, not copied
    # from a page, so it has never been fetched at the point enrichment records
    # it. Verify before handing it over. Only a link enrichment just introduced
    # is checked here: settle_link already looked at the one the job carried
    # into the pass above, and re-checking it would spend a request per job to
    # learn what `link_checked` already records.
    for job in fresh:
        if not job.get("apply_url") or job["apply_url"] == job.get("link_checked"):
            continue
        settle_link(job)
    # Same rule as after the first pass: enrichment can only have improved the
    # link, so a job that is back on a paid relister here lost a rescue that the
    # cut had accepted.
    lost_late_rescue = [j for j in fresh if on_paid_relister(apply_link(j))]
    fresh = [j for j in fresh if not on_paid_relister(apply_link(j))]
    for job in lost_late_rescue:
        log(f"Paid relister {link_host(job['url'])} again after the rescued link "
            f"failed: {job['company']} — {job['title']}")

    # Recounted, because the loop above just rewrote the statuses it counts. The
    # figure taken before enrichment is right for the no-new-jobs return above
    # and stale for every save_stats below it: a card whose rescued link came
    # back inconclusive says so in its footer, and the day's row said 0.
    unknown_count = sum(j["link_status"] == LINK_UNKNOWN for j in fresh)

    # Second pass, now that the text exists: a posting relisted under a new URL
    # is still the same posting. Runs before ranking so a repeat never costs a
    # model call.
    fresh, relisted = drop_repeat_descriptions(fresh, seen)
    for job in relisted:
        log(f"Skipping {job['company']} — {job['title']}: description already delivered")
    if not fresh:
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings)
        save_stats(
            stats, today, len(all_jobs), len(matched), 0, None,
            link_closed=len(closed), link_unknown=unknown_count,
            strong=0, review=0, tailored=0, tailor_failed=0,
            relisted=len(relisted),
        )
        log(f"Nothing left to deliver: {len(relisted)} relists, "
            f"{len(lost_late_rescue)} lost the rescued link enrichment found. "
            "Heartbeat sent.")
        return

    ranking = rank_jobs(fresh, rank_key) if rank_key else None
    if rank_key and ranking is None:
        log("Ranker unavailable, using keyword scoring for this run")
    apply_ranking(fresh, ranking)
    for job in cap_teaser_scores(fresh):
        log(f"Capped {job['company']} — {job['title']} at {job['score']}: teaser, not a full JD")

    # Captured before the delivery floor and the MAX_JOBS_IN_DIGEST cut below, so
    # a stale profile shows up in the drift trend even on days the real drop is
    # buried past the top 25 — or below the floor. check_drift compares this
    # average against the prior window, and measuring it after a cut that
    # removes exactly the low scores would make every run look healthy.
    new_count = len(fresh)
    avg_score = round(sum(j["score"] for j in fresh) / new_count, 1)

    fresh, below_floor = drop_below_floor(fresh)
    for job in below_floor:
        log(f"Below floor {job['company']} — {job['title']}: fit {job['score']} < {MIN_DELIVER_SCORE}")
        # A verified score under the floor is a decision, not a delivery
        # failure, so it is remembered here rather than left to be re-fetched,
        # re-scored and re-rejected every day until it ages out.
        remember(seen, job, today)

    if not fresh:
        save_seen(seen)
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings)
        save_stats(
            stats, today, len(all_jobs), len(matched), new_count, avg_score,
            link_closed=len(closed), link_unknown=unknown_count,
            strong=0, review=0, tailored=0, tailor_failed=0,
            relisted=len(relisted), below_floor=len(below_floor),
        )
        log(f"All {len(below_floor)} new postings scored below {MIN_DELIVER_SCORE}. Heartbeat sent.")
        return

    strong, review = split_tiers(fresh)
    strong.sort(key=lambda x: x["score"], reverse=True)
    review.sort(key=lambda x: x["score"], reverse=True)
    strong = strong[:MAX_JOBS_IN_DIGEST]
    review = review[:MAX_JOBS_IN_DIGEST - len(strong)]
    for j in strong:
        j["tier"] = "Strong matches"
    for j in review:
        j["tier"] = "Worth reviewing"
    delivered = strong + review
    fresh = delivered

    # Persist full job/JD evidence before delivery. A later button click can
    # therefore tailor the actual captured posting rather than its title.
    snapshots = [make_snapshot(job, f"{today}T00:00:00Z") for job in fresh]
    write_snapshots(fresh, today)

    lines = [f"**Job digest — {today}**  ({len(fresh)} new in Saudi Arabia)"]
    lines.extend(f"⚠️ {warning}" for warning in warnings if warning)
    by_company = {}
    for j in fresh:
        by_company.setdefault((j["tier"], j["company"]), []).append(j)
    last_tier = None
    for (tier, comp), jobs in by_company.items():
        if tier != last_tier:
            lines.append(f"\n**{tier}**")
            last_tier = tier
        lines.append(f"\n__**{comp}**__")
        for j in jobs:
            # Only Pinpoint publishes structured pay today; everything else omits it.
            pay = f" · {j['salary']}" if j.get("salary") else ""
            resume_line = ""
            flags = []
            if j.get("link_status") == LINK_UNKNOWN:
                flags.append("link check inconclusive")
            if not j.get("description"):
                flags.append("title-only assessment")
            if flags:
                resume_line += f"\n  _{'; '.join(flags)}_"
            lines.append(
                f"- **{j['title']}** — {j['location']} · fit {j['score']}{pay}\n"
                f"  {j['why']}\n  <{apply_link(j)}>{resume_line}"
            )

    interactive_delivery_claimed = False
    delivery_failed = False
    delivered_ids = set()
    if worker_url:
        try:
            delivery = post_interactive_cards(worker_url, worker_secret, snapshots)
            accepted_keys = set(delivery.get("posted", []) + delivery.get("existing", []))
            uncertain_keys = set(delivery.get("in_progress", []) + delivery.get("retryable", []))
            failed = delivery.get("failed", [])
            interactive_delivery_claimed = bool(accepted_keys or uncertain_keys or failed)
            delivered_ids = {snapshot["source_id"] for snapshot in snapshots if snapshot["job_key"] in accepted_keys}
            log(
                f"Interactive delivery accepted {len(accepted_keys)} of {len(snapshots)} cards "
                f"({len(failed)} failed)"
            )
            # Nothing reached Discord and at least one card definitively failed.
            # Falling back to the webhook would risk double-posting the jobs the
            # Worker may still hold, so the run stays single-channel and instead
            # fails loudly: seen.json is untouched, so tomorrow retries them.
            if not accepted_keys and not uncertain_keys and failed:
                delivery_failed = True
                log(f"ERROR no job card reached Discord; {len(failed)} of {len(snapshots)} cards failed")
                for item in failed[:5]:
                    log(f"  failed card {item.get('job_key')}: {item.get('error')}")
        except Exception as error:
            log(f"WARN interactive Discord delivery failed: {error}; falling back to webhook")
    if not interactive_delivery_claimed:
        if not webhook:
            log("FATAL interactive delivery failed and DISCORD_WEBHOOK_URL is not set")
            sys.exit(1)
        post_discord(webhook, lines)
        # Reaching this line means Discord accepted every chunk: post_discord
        # exits non-zero on any status outside 200/204. The jobs are therefore
        # delivered, whichever channel carried them, and have to be remembered.
        # They were not, when a Worker was configured and its call raised: the
        # webhook posted the digest, seen.json stayed empty, and the identical
        # digest went out again the next morning -- re-fetching every
        # description and paying for a second ranking call -- for as long as
        # the Worker stayed unreachable.
        delivered_ids = {job["id"] for job in fresh}

    for j in fresh:
        if j["id"] in delivered_ids:
            remember(seen, j, today)
    save_seen(seen)
    save_stats(
        stats, today, len(all_jobs), len(matched), new_count, avg_score,
        link_closed=len(closed), link_unknown=unknown_count,
        strong=len(strong), review=len(review), tailored=0, tailor_failed=0,
        delivered=len(delivered_ids), relisted=len(relisted),
        below_floor=len(below_floor),
    )
    log(f"Delivered {len(delivered_ids)} jobs. seen.json now holds {len(seen)} ids.")
    if delivery_failed:
        log("FATAL digest produced jobs but none were delivered; failing the run so it is visible")
        sys.exit(1)


# ----------------------------- CANDIDATES -----------------------------------
# Run `python digest.py --discover <name>...` first — it probes every ATS below
# and prints a ready-to-paste COMPANIES line. Fall back to reading a careers page
# only when the token differs from the company name (Lean is "LeanTech"):
#   jobs.ashbyhq.com/<token>        -> source "ashby"
#   boards[.eu].greenhouse.io/<tk>  -> source "greenhouse" (set region)
#   apply.workable.com/<token>      -> source "workable"
#   jobs.lever.co/<token>           -> source "lever"
#   <token>.pinpointhq.com          -> source "pinpoint"
#
# All of the original candidate list is now RESOLVED (23 Jul 2026):
#   Foodics, Salla                  -> DONE, workable, live roles
#   Tabby                           -> DONE, pinpoint (careers.tabby.ai redirects there)
#   Mozn / Unifonic / Jahez / Zid /
#   Nana / Rasan / Sary             -> DONE, workable accounts confirmed real but
#                                      dormant (0 open roles). Already in COMPANIES.
#
# RULED OUT — big employers, no free public JSON API. Reaching them means scraping
# HTML, which this project deliberately refuses (see README). Don't re-litigate:
#   stc     -> SuccessFactors (sapsf.com)
#   NEOM    -> own-host SPA, no public feed found
#   Aramco  -> not Greenhouse. The "greenhouse" string on its careers page is
#              "greenhouse gas" — it's an oil company. Easy false positive.
#   SmartRecruiters as a discovery route is useless: its API answers
#   200 {"totalFound":0} for ANY string, so it confirms companies that don't exist.
#   13 KSA employer ids were probed, all empty.
#
# UNPROBED — employers the aggregator keeps surfacing that this list does not
# reach directly, counted over the 20 snapshots archived to 2026-08-28. Every
# one of these arrives through a relister today, which is the whole reason the
# link-quality passes in main() exist. Probe before adding: a guessed token is
# what the control probe above is here to stop.
#
#   JAL International (12)   Qualcomm (11)    QuantumBlack / McKinsey (11)
#   exequt MENA (10)         Mindstream (8)   Pasqal (6)
#   Advanced Analytics (5)   Qiddiya (5)      Colada (4)
#   Infosys (4)              flynas (3)       Elm (2)
#   HungerStation (2)        KAUST (2)        Kanz (2)        Raqmiyat (2)
#
# Run the "Discover company ATS boards" workflow (.github/workflows/discover.yml,
# workflow_dispatch) with those names, or `python digest.py --discover <names>`
# on any machine with network egress, and paste whatever it confirms into
# COMPANIES. A confirmed account with zero open roles still goes in — that is
# the standing precedent for the seven dormant Workable entries above.
# ----------------------------------------------------------------------------


# ------------------------------ DISCOVERY -----------------------------------
# `python digest.py --discover salla tabby mozn` probes a company name as a token
# against every token-based ATS and prints what's confirmed live.
#
# Each probe hits the SAME endpoint its fetcher uses. That matters: Workable's v1
# widget undercounts against the v3 endpoint fetch_workable reads, so probing the
# convenient endpoint gives an answer about an API the engine never calls.
CONTROL_TOKEN = "zzznotarealcompany999"


def _probe_greenhouse(t):
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=false")
    return (r.status_code == 200, len(r.json().get("jobs", [])) if r.status_code == 200 else 0)


def _probe_ashby(t):
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true")
    return (r.status_code == 200, len(r.json().get("jobs", [])) if r.status_code == 200 else 0)


def _probe_lever(t):
    r = _get(f"https://api.lever.co/v0/postings/{t}?mode=json")
    return (r.status_code == 200, len(r.json()) if r.status_code == 200 else 0)


def _probe_workable(t):
    r = _post(f"https://apply.workable.com/api/v3/accounts/{t}/jobs", {})
    return (r.status_code == 200, r.json().get("total", 0) if r.status_code == 200 else 0)


def _probe_pinpoint(t):
    r = _get(f"https://{t}.pinpointhq.com/postings.json")
    return (r.status_code == 200, len(r.json().get("data", [])) if r.status_code == 200 else 0)


PROBES = {
    "greenhouse": _probe_greenhouse,
    "ashby": _probe_ashby,
    "lever": _probe_lever,
    "workable": _probe_workable,
    "pinpoint": _probe_pinpoint,
}


def _probe(source, token):
    """(found, count) for one ATS. Any error is a miss — a probe never raises."""
    try:
        return PROBES[source](token)
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return (False, 0)


def discover(names):
    """
    Probe each name against every ATS and report confirmed hits.

    A probe is only trustworthy if the same probe MISSES a known-bad token. Some
    job APIs answer 200 for any string they're handed — SmartRecruiters returns
    200 {"totalFound":0} for literal nonsense — so an unguarded prober invents
    companies with total confidence. Any ATS whose control probe "succeeds" is
    reported UNRELIABLE and its per-name results are suppressed, not shown.
    """
    log(f"Control probe with bogus token '{CONTROL_TOKEN}'...")
    reliable = {}
    for source in PROBES:
        found, _ = _probe(source, CONTROL_TOKEN)
        reliable[source] = not found
        if found:
            log(f"  UNRELIABLE {source}: answers for a nonsense token; results suppressed")
    usable = [s for s in PROBES if reliable[s]]
    log(f"Usable probes: {', '.join(usable) or 'NONE'}")

    for name in names:
        token = name.strip().lower()
        if not token:
            continue
        hits = []
        for source in usable:
            found, count = _probe(source, token)
            if found:
                hits.append((source, count))
            time.sleep(0.2)  # be polite across ATSs
        print(f"\n{name}:")
        if not hits:
            print(f"  no ATS found for token '{token}'")
            # A miss is not proof of absence: this guesses the name as the token, so
            # any company whose token differs (Lean is "LeanTech") is invisible here.
            print("  Not proof it has no ATS - the token may just differ from the name.")
            print("  Open the careers page, follow Apply, and read the final host.")
            continue
        for source, count in hits:
            state = f"{count} open roles" if count else "account exists, 0 open roles"
            print(f'  {source}: {state}')
            print(f'    {{"name": "{name}", "source": "{source}", "token": "{token}"}},')
# ----------------------------------------------------------------------------


# ------------------------------ JSEARCH PROBE -------------------------------
# `python digest.py --probe-jsearch "data engineer Riyadh"` prints the SHAPE of one
# live /search-v2 response. It exists because fetch_jsearch's docstring lists only
# the fields this repository has actually seen, and the alternative apply links the
# paid-relister recovery depends on are not among them: 7-8 postings a day are
# discarded for sitting on a paid wrapper, and whether JSearch already offers a free
# link for them has never been checked against a real payload.
#
# It reads. It never writes, never delivers and never touches seen.json. It prints
# field names and link HOSTS -- never the API key, never a whole payload.
def probe_jsearch(query, api_key):
    """Report which fields one live JSearch job object carries, and what they link to."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    url = (f"https://{JSEARCH_HOST}/search-v2"
           f"?query={requests.utils.quote(query)}&country=sa&date_posted=month")
    r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                   "X-RapidAPI-Host": JSEARCH_HOST}, timeout=30)
    print(f"HTTP {r.status_code} for query {query!r}")
    # RapidAPI reports the subscription's monthly allowance on every response.
    # Reading page two of every query multiplies the daily spend, so the budget
    # has to be a measured number rather than an assumption: exhausting it takes
    # the aggregator down completely until the plan resets.
    quota = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"quota headers: {quota or '(none reported)'}")
    if r.status_code != 200:
        print(f"body[:200]: {r.text[:200]}")
        return 1
    payload = r.json().get("data") or {}
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    print(f"top-level data keys: {sorted(payload) if isinstance(payload, dict) else 'list'}")
    print(f"{len(jobs)} jobs returned\n")

    # Page one is all fetch_jsearch has ever read; data.cursor is documented in
    # its docstring and never used. Whether the cursor actually pages, and how
    # much of page two is genuinely new, decides whether widening the fetch is
    # worth a second request per query.
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    if cursor:
        page2 = requests.get(f"{url}&cursor={requests.utils.quote(str(cursor))}",
                             headers={"X-RapidAPI-Key": api_key,
                                      "X-RapidAPI-Host": JSEARCH_HOST}, timeout=30)
        print(f"cursor page 2: HTTP {page2.status_code}")
        if page2.status_code == 200:
            more = (page2.json().get("data") or {}).get("jobs", [])
            first = {j.get("job_id") for j in jobs}
            fresh = [j for j in more if j.get("job_id") not in first]
            print(f"  {len(more)} jobs, {len(fresh)} of them not on page one")
            for j in fresh[:3]:
                print(f"    {(j.get('employer_name') or '?')[:30]} — "
                      f"{(j.get('job_title') or '?')[:45]}")
        else:
            print(f"  body[:160]: {page2.text[:160]}")
    else:
        print("no cursor on this response; page one is all there is")
    print()
    if not jobs:
        return 0

    print("job object keys (first job):")
    for key in sorted(jobs[0]):
        print(f"  {key}")
    print()

    # Any field holding a list of dicts with a link-ish key is a candidate for the
    # alternative apply links. Reported by NAME so the real field can be recorded
    # rather than guessed, whatever JSearch decided to call it.
    rescuable = 0
    with_site = 0
    paid_count = 0
    paid_with_site = 0
    for job in jobs:
        primary = job.get("job_apply_link") or ""
        paid = on_paid_relister(primary)
        paid_count += paid
        # The relisters block this runner (403 from jooble and jobrapido, 404 from
        # jobleads, measured 3 Sep 2026), so the page hop cannot reach a paywalled
        # posting at all. These two fields are the only routes to the employer that
        # do not go through the relister, so report what they actually hold.
        site = (job.get("employer_website") or "").strip()
        google = (job.get("job_google_link") or "").strip()
        if site:
            with_site += 1
            paid_with_site += paid
        print(f"- {(job.get('employer_name') or '?')[:40]} — {(job.get('job_title') or '?')[:50]}")
        print(f"    primary link host: {link_host(primary) or '(none)'}"
              f"{'  [PAID RELISTER]' if paid else ''}")
        print(f"    employer_website:  {link_host(site) or (site or '(empty)')}")
        print(f"    job_google_link:   {link_host(google) or '(empty)'}")
        print(f"    publisher/direct:  {job.get('job_publisher') or '?'} / "
              f"job_apply_is_direct={job.get('job_apply_is_direct')!r}")
        found_any = False
        for key, value in job.items():
            if not isinstance(value, list):
                continue
            entries = [v for v in value if isinstance(v, dict)
                       and any(isinstance(inner, str) and ("link" in str(k).lower()
                                                           or "url" in str(k).lower())
                               for k, inner in v.items())]
            if not entries:
                continue
            found_any = True
            print(f"    field {key!r}: {len(entries)} link-bearing entries, "
                  f"item keys {sorted(entries[0])}")
            free_hosts = []
            for entry in entries:
                link = next((v for k, v in entry.items()
                             if isinstance(v, str) and ("link" in str(k).lower()
                                                        or "url" in str(k).lower())), "")
                publisher = entry.get("publisher") or entry.get("name") or "?"
                host = link_host(link) or "(unparseable)"
                verdict = "PAID" if on_paid_relister(link) else "free"
                print(f"      {publisher[:24]:<24} {host:<32} {verdict}")
                if verdict == "free":
                    free_hosts.append(host)
            if paid and free_hosts:
                rescuable += 1
        if not found_any:
            print("    no link-bearing list field on this job")
        print()

    print(f"{with_site} of {len(jobs)} jobs name an employer_website.")
    print(f"{paid_count} sit on a paid relister; {paid_with_site} of those name a website.")

    print(f"Of {len(jobs)} jobs, {rescuable} sit on a paid relister "
          f"AND carry a free alternative in the same response.")
    return 0
# ----------------------------------------------------------------------------


# Adding a query costs 30 requests a month out of 200 whether or not it brings
# anything back, and the three in AGGREGATOR_QUERIES were never measured against
# each other. This reads page one of several queries in one go and reports, per
# query, how many postings NO other query returned and how many of those pass
# the digest's own title and location filters -- which is the only number that
# decides whether a query earns its place.
def probe_queries(queries, api_key):
    """Report what each query brings in that the others do not."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    pages = {}
    for query in queries:
        url = (f"https://{JSEARCH_HOST}/search-v2"
               f"?query={requests.utils.quote(query)}&country=sa&date_posted=month")
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"{query!r}: HTTP {r.status_code} — skipped")
            pages[query] = []
            continue
        payload = r.json().get("data") or {}
        pages[query] = payload.get("jobs", []) if isinstance(payload, dict) else payload
        print(f"{query!r}: {len(pages[query])} returned")
        time.sleep(0.5)
    remaining = r.headers.get("x-ratelimit-requests-remaining")
    print(f"quota after probe: {remaining} remaining")
    print()

    def as_job(j):
        city = (j.get("job_city") or "").strip()
        state = (j.get("job_state") or "").strip()
        country = COUNTRY_CODE_TO_NAME.get((j.get("job_country") or "").strip().upper(),
                                           (j.get("job_country") or "").strip())
        return {"title": (j.get("job_title") or "").strip(),
                "location": ", ".join(p for p in (city, state, country) if p)}

    owners = {}
    for query, jobs in pages.items():
        for j in jobs:
            owners.setdefault(str(j.get("job_id") or ""), set()).add(query)

    for query, jobs in pages.items():
        only = [j for j in jobs if len(owners[str(j.get("job_id") or "")]) == 1]
        keeps = [j for j in only if matches_filters(as_job(j))]
        print(f"{query!r}: {len(jobs)} returned, {len(only)} returned by no other "
              f"query, {len(keeps)} of those pass the filters")
        for j in keeps[:6]:
            print(f"    {(j.get('employer_name') or '?')[:28]:<28} "
                  f"{(j.get('job_title') or '?')[:48]}")
        print()
    return 0
# ----------------------------------------------------------------------------


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        discover(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-queries":
        sys.exit(probe_queries([q.strip() for q in sys.argv[2:] if q.strip()],
                               os.environ.get("RAPIDAPI_KEY")))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-jsearch":
        query = " ".join(sys.argv[2:]) or AGGREGATOR_QUERIES[0]
        sys.exit(probe_jsearch(query, os.environ.get("RAPIDAPI_KEY")))
    else:
        main()
