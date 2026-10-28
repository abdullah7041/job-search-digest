#!/usr/bin/env python3
"""
Daily job-search digest -> Discord.

Pattern (same as the AI-news digest):
  gather  -> JSearch (RapidAPI, Google for Jobs aggregator) for breadth
             + company ATS APIs (Greenhouse, Ashby, Workable, Lever) for depth
  filter  -> Saudi-only, track-specific IC levels, strong matches within 7 days
             and evidence-backed review candidates within 30 days
  rank    -> one cheap OpenRouter call for score + one-line "why it fits"
             (deterministic keyword fallback if the ranker is missing or fails)
  deliver -> up to ten jobs via interactive Discord cards or webhook
Runs on GitHub Actions cron. No laptop needed.

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  DISCORD_WEBHOOK_URL   required
  RAPIDAPI_KEY          required for JSearch. Without it JSearch is skipped (ATS-only run).
  OPENROUTER_API_KEY    required for verified fit; without it, status only.

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
  change. Missing or corrupt evidence suppresses job cards and reports status.
"""

import os
import re
import hashlib
import functools
import sys
import json
import time
import datetime
from itertools import groupby

import requests
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from discord_delivery import post_interactive_cards
from jd_fetch import ats_reference, ats_url, bounded_get, extract_posting, jsonld_posting_countries, strip_html
from job_archive import (GRADE_FULL, GRADE_TEASER, jd_completeness_failures, jd_grade, jd_requirements, jd_signal,
                         job_key, make_snapshot, score_instability, write_snapshots)
import fit_check
from fit_check import check_jobs
from openrouter import chat_json, configured_chain
from resume_source import MATCH_CATEGORIES

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
    {"name": "MinIO",    "source": "greenhouse", "token": "minio"},
    {"name": "Scale AI", "source": "greenhouse", "token": "scaleai"},
    {"name": "Lucidya",  "source": "workable", "token": "lucidya"},
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
    "devops", "sre", "site reliability", "platform", "automation",
    "data analyst", "analytics", "business intelligence", "power bi",
    # NOT the bare word "architect": in Saudi Arabia that is a building
    # architect far more often than a software one. Name the software ones.
    "solutions architect", "software architect", "data architect",
    "cloud architect", "enterprise architect", "technical architect",
    "security architect",
    "solutions engineer",
]

# "Engineer" is not a software word here. Of 177 Saudi postings Bayt returned
# inside a week on 7 Sep 2026, 33 passed these filters -- and they were
# Structural Design, Architectural, Firefighting, Civil & Transportation, HSE,
# Mechanical Design, Electrical, Maintenance and Estimation engineers. Saudi
# Arabia runs on construction and industry, so a filter built on the bare word
# matches that market far more often than this candidate's.
#
# It went unnoticed because LOCATION_INCLUDE and the ranker sit downstream and
# absorbed it -- at the cost of a model call per irrelevant role.
TITLE_EXCLUDE_DISCIPLINE = [
    "civil", "structural", "structure", "architectural", "mechanical",
    "electrical", "firefighting", "fire protection", "hvac", "mep ", " mep",
    "hse", "biomedical", "chemical", "petroleum", "drilling", "geotechnical",
    "surveyor", "quantity survey", "landscape", "interior", "irrigation",
    "arboriculture", "agriculture", "piping", "welding", "metallurg",
    "instrumentation", "marine", "mining", "estimation", "maintenance",
    "planning engineer", "sales engineer", "bridge", "construction",
]

# Role levels are track-specific: early-career software/AI and experienced analytics.
# The 2026-07-31 digest scored Lead/Staff/Team-Leader roles 85-98 because this
# list only bounded seniority from below and profile.json said "senior or
# above". Target is mid-level and senior IC; leadership tracks are out.
TITLE_EXCLUDE = [
    "intern", "internship", "graduate", "trainee", "tamheer",
    "co-op", "apprentice", "student",
    "lead ", " lead", "leader", "staff", "principal", "head of", "director",
    "chief", "vice president", " vp", "vp ", "manager", "management",
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
    "tracks": [],
}

# JSearch (RapidAPI / Google for Jobs aggregator), endpoint /search-v2.
# Two rotating page-one queries/day use about 60 requests/month, leaving a
# reserve for manual diagnostics and unexpected reruns.
# Endpoint + response shape + field names verified live 28 Jun 2026.
# Twelve candidate-directed role families, rotated within the existing request
# budget. Broader search is not a measured yield improvement until live source
# receipts and delivered jobs confirm it.
AGGREGATOR_QUERIES = [
    "sales operations analyst Saudi Arabia",
    "commercial analyst Saudi Arabia",
    "data analyst Saudi Arabia",
    "business intelligence analyst Saudi Arabia",
    "AI automation specialist Saudi Arabia",
    "AI implementation specialist Saudi Arabia",
    "AI product engineer Saudi Arabia",
    "LLM application engineer Saudi Arabia",
    "full stack developer Saudi Arabia",
    "customer insights analyst Saudi Arabia",
    "senior sales specialist Saudi Arabia",
    "AI solutions engineer Saudi Arabia",
]
JSEARCH_HOST = "jsearch.p.rapidapi.com"

# JSearch is breadth, not the source of truth for freshly posted roles. Two
# rotating page-one reads use about 60 requests per month and leave room for
# bounded diagnostics; direct employer ATS feeds continue to run every day.
def _request_budget(raw):
    """The number of rotating page-one JSearch queries for this run."""
    default = 2
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    # Adding target families must not silently permit a larger daily spend.
    return value if 0 <= value <= min(6, len(AGGREGATOR_QUERIES)) else default


JSEARCH_DAILY_REQUESTS = _request_budget(os.environ.get("JSEARCH_DAILY_REQUESTS"))

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

# The daily run has no user waiting on it, but it does have a workflow timeout
# it shares with every fetch above.
RANK_BUDGET_SECONDS = 120

# The network phase: everything before ranking. A job timeout is NOT graceful --
# it cancels the job, so the state artifact is never packaged and seen.json never
# records cards Discord already holds. The only defence is for digest.py to
# finish on its own, so the phases have to sum under the wall:
#
#   RUN_BUDGET_SECONDS   600   fetch, link settling, JD enrichment
#   RANK_BUDGET_SECONDS  120   one ranking call
#   CHECK_BUDGET_SECONDS 120   the reality checks
#   ------------------------------------------------------------------
#   840 of the execute job's 1200, leaving delivery and persistence unbounded.
#
# 600 and not 720 because the reality check now runs on every delivered job
# rather than the two or three scoring 80+, so both model budgets should be
# assumed spent rather than averaged.
#
# Deliberately never checked at or after delivery. Once cards reach Discord,
# persisting seen.json is mandatory: the same rule CHECK_BUDGET_SECONDS states,
# which is to spend a deadline on losing work and never on losing the digest.
RUN_BUDGET_SECONDS = 600

MAX_JOBS_IN_DIGEST = 10
# Discovered boards read per run, on top of the hand-curated COMPANIES list,
# which is never rationed. Board discovery adds entries every week and has no
# ceiling of its own, so without this the company loop grows without bound
# against RUN_BUDGET_SECONDS. Overflow is served least-recently-read first on
# following runs, so this remains a time backstop rather than a starvation rule.
MAX_BOARDS_PER_RUN = 150
# One employer must not be able to fill a morning on its own. Board discovery
# works well enough that it can: Innovationteam, found 3 Sep 2026, supplied 9 of
# that evening's 12 delivered cards from a board of 40 roles, with 7 more of its
# postings dropped below the floor. split_tiers sorts on score alone, so without
# this a board that posts a batch crowds every other employer out of the day.
MAX_JOBS_PER_COMPANY = 3
STRONG_MATCH_MIN_SCORE = 80
# The floor of the rubric's "competitive with gaps" band. A job here is a real
# match with something the posting states that the resume cannot show, which is
# a different thing from a role that is merely adjacent -- and with the scoring
# made honest it is where most genuinely relevant postings now land.
COMPETITIVE_MIN_SCORE = 60

def _score_floor(raw: str, default: int = 80) -> int:
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


# Strong-match threshold. Review candidates fill remaining shortlist places.
# Scores below 40
# and out-of-scope roles receive cooldowns; incomplete evidence remains retryable.
MIN_DELIVER_SCORE = _score_floor(os.environ.get("MIN_DELIVER_SCORE") or "")

# Per-job resume tailoring is an explicit Discord interaction handled by
# interactive_runner.py. The daily run archives JD evidence and posts jobs
# only; it never spends tailoring credits or writes private resumes into the
# repository. These two bounds are what the daily run still needs from the JD.
JD_MAX_CHARS = 6000     # JD text stored per job in the snapshot archive
JD_RANK_CHARS = 2600    # JD window per job inside the one ranking call

# The window is taken from where the posting starts describing the job, not from
# the top. The old 900 leading characters of a McKinsey posting are entirely
# recruiting copy — the role begins at character 1931 — so the ranker reported,
# correctly, that the description named no technical requirements. It had never
# been shown them. 25 jobs at this width is roughly 14k input tokens per run.

# A teaser is a paragraph of marketing copy that clears the completeness checks
# without evidencing anything. It can be reviewed, and it can be tailored
# against with a warning, but it can never be a strong match: the score that
# says so would be built on nothing. 69 is the top of the rubric's "adjacent
# function, seniority mismatch, or several gaps" band, below the strong line.
TEASER_SCORE_CAP = 69

# A job whose stated hard requirements the resume cannot evidence is held down
# to what those gaps allow, exactly as a teaser is held out of the strong tier
# and for the same reason: "strong match" has to mean the evidence backed it up.
# The ceiling is no longer one flat number -- see fit_check.SEVERITY_CEILING and
# blocked_score_ceiling, which cap a stated-degree gap on its own. plans/018-019.

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

# seen.json holds three kinds of key. A job identity remembers the posting; this
# prefix remembers a description within its employer and title context, which
# catches an aggregator that relists the same job under a fresh URL. Both age
# out on the same TTL.
JD_SEEN_PREFIX = "jd:"

# The third kind, and the one that ages out on its own clock. A posting the
# ranker put below MIN_DELIVER_SCORE is skipped for a week rather than written
# under the 120-day TTL, because scores move: the archives hold one description
# scored 40, 40, 40, 45, 50, 50 and 75 unchanged. Long enough not to re-rank the
# same reject every morning, short enough that one bad number is not a verdict.
# Previous cooldowns also rejected jobs awaiting positive fit evidence. Start a new
# namespace so those roles can be checked again without forgetting deliveries.
FLOOR_SEEN_PREFIX = "floor:verified:"
FLOOR_COOLDOWN_DAYS = 7
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


SOURCE_FAILURES = set()
THROTTLED_SOURCES = {}
BOARD_COVERAGE = {}
NETWORK_DEADLINE = 0


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def source_failure(source, message):
    """Retain failed source reads so an empty result cannot imply full coverage."""
    SOURCE_FAILURES.add(source)
    log(message)


def load_profile():
    """Read profile.json; fall back to DEFAULT_PROFILE on any problem so a
    missing or half-edited file never breaks a run."""
    if not os.path.exists(PROFILE_FILE):
        log(f"WARN {PROFILE_FILE} not found, using default profile")
        return DEFAULT_PROFILE
    try:
        with open(PROFILE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log(f"WARN {PROFILE_FILE} is corrupt, using default profile")
        return DEFAULT_PROFILE
    if (not isinstance(data, dict)
            or not isinstance(data.get("blurb"), str) or not data["blurb"].strip()
            or not isinstance(data.get("skill_keywords"), list) or not data["skill_keywords"]
            or not all(isinstance(k, str) and k.strip() for k in data["skill_keywords"])
            or ("tracks" in data and (not isinstance(data["tracks"], list)
                or not data["tracks"] or any(
                    not isinstance(t, dict)
                    or not all(isinstance(t.get(k), str) and t[k].strip() for k in ("name", "evidence"))
                    or not isinstance(t.get("titles"), list) or not t["titles"]
                    or not all(isinstance(title, str) and title.strip() for title in t["titles"])
                    for t in data["tracks"])))
            or ("title_priorities" in data and (
                not isinstance(data["title_priorities"], dict)
                or not data["title_priorities"]
                or any(not isinstance(title, str) or not title.strip()
                       or type(priority) is not int or not 1 <= priority <= 12
                       for title, priority in data["title_priorities"].items())))):
        log(f"WARN {PROFILE_FILE} missing 'blurb' or 'skill_keywords', using default profile")
        return DEFAULT_PROFILE
    return data


PROFILE = load_profile()


def riyadh_today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).date()


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
        source_failure(c['name'], f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        source_failure(c['name'], f"WARN {c['name']}: greenhouse HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        source_failure(c['name'], f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "company": c["name"],
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url") or "",
            # NOT `or updated_at`. Once the age gate reads this field, a role
            # posted six months ago and edited yesterday would pass a 7-day
            # cutoff as fresh -- worse than an undated posting, which at least
            # says so on the card. A real publication date or nothing.
            "first_published": j.get("first_published") or "",
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
        source_failure(c['name'], f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        source_failure(c['name'], f"WARN {c['name']}: ashby HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        source_failure(c['name'], f"WARN {c['name']}: non-JSON response. Skipping.")
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
    if "workable" in THROTTLED_SOURCES:
        return []
    # Workable v3 board API: POST, 10 jobs/page, cursor in "nextPage" -> echo back
    # as {"token": ...}. Verified live 24 Jun 2026 against apply.workable.com/foodics.
    # No public apply URL in the payload, so we build the canonical one from shortcode.
    url = f"https://apply.workable.com/api/v3/accounts/{c['token']}/jobs"
    out, body, pages = [], {}, 0
    retried = False
    MAX_PAGES = 30  # hard stop so a misbehaving cursor can never loop forever (300 jobs)
    while pages < MAX_PAGES:
        try:
            r = _post(url, body)
            if r.status_code == 429 and not retried:
                try:
                    delay = int(r.headers.get("Retry-After", ""))
                except (ValueError, TypeError):
                    delay = -1
                if 0 <= delay <= 5 and time.monotonic() + delay + 30 < NETWORK_DEADLINE:
                    retried = True
                    time.sleep(delay)
                    r = _post(url, body)
        except requests.RequestException as e:
            source_failure(c['name'], f"WARN {c['name']}: request failed: {e}")
            return out  # return what we already have; partial beats crashing
        if r.status_code != 200:
            if r.status_code == 429:
                THROTTLED_SOURCES["workable"] = str(r.headers.get("Retry-After") or "next run")[:60]
            source_failure(c['name'], f"WARN {c['name']}: workable HTTP {r.status_code} (token '{c['token']}'). Skipping.")
            return out
        try:
            data = r.json()
        except ValueError:
            source_failure(c['name'], f"WARN {c['name']}: non-JSON response. Skipping.")
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
        source_failure(c['name'], f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        source_failure(c['name'], f"WARN {c['name']}: lever HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        source_failure(c['name'], f"WARN {c['name']}: non-JSON response. Skipping.")
        return []
    if not isinstance(data, list):
        source_failure(c['name'], f"WARN {c['name']}: unexpected lever payload. Skipping.")
        return []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        published = _epoch_ms_date(j.get("createdAt"))
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


def fetch_jsearch(api_key, paywalled=None, quota=None, paused_feeds=None):
    """
    Query JSearch (RapidAPI Google-for-Jobs aggregator) for each AGGREGATOR_QUERIES entry.
    Returns jobs in the same shape as ATS fetchers so the shared filter/rank path works.
    Skipped entirely (returns []) if api_key is absent or subscription inactive.

    A posting whose apply link is on a paid relister is refused here rather than
    carried through the run and cut later. It used to be fetched, deduped,
    filtered, verified, counted and logged a line each before being discarded
    every morning -- 11 of 48 on 3 Sep 2026 -- and neither rescue could reach
    it: the relisters answer 403 or 404 to this runner, and apply_options has
    never carried an alternative. Its EMPLOYER is still worth having, so one
    that passes the same title and location filters the digest applies is
    appended to `paywalled` when the caller provides a list. That is what board
    discovery converts, and it is scoped by the filters so five ATS requests are
    never spent on a role this candidate would not be shown.

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

    quota = quota if quota is not None else {}
    if out_of_allowance("jsearch", quota, paused_feeds=paused_feeds):
        return []

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }
    out = []
    seen_ids = set()  # dedupe within JSearch results across queries
    apply_field_logged = False   # the discovered field is named once per run, not per job
    dropped = []     # paywalled postings, refused before they enter the run

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
            source_failure('jsearch', f"WARN JSearch [{query!r}]: request failed: {e}")
            return [], ""
        recorded = _record_quota(quota, r.headers)
        _debit_unreported_quota(quota, recorded)
        if r.status_code != 200:
            source_failure('jsearch', f"WARN JSearch [{query!r}]: HTTP {r.status_code}: {r.text[:120]}")
            return [], ""
        try:
            data = r.json()
        except ValueError:
            source_failure('jsearch', f"WARN JSearch [{query!r}]: non-JSON response")
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
            # job_country is the country this query ASKED for, not the
            # employer's, on every row -- remote and onsite alike. Measured
            # live 8 Sep 2026 on `software engineer Saudi Arabia`, sent with
            # country=sa: Quest Diagnostics and Liquid XR came back
            # job_city='Los Ángeles، CA' with job_country='SA' and
            # job_is_remote=False, alongside remote rows reading
            # 'Anywhere، CA' or carrying no city at all.
            #
            # Expanding that 'SA' to "Saudi Arabia" put the word "saudi" into
            # every one of those strings, so a Los Angeles role read as local.
            # Nineteen reached the 8 Sep 07:00 run and nine more the run after;
            # both times the AGE gate caught them, by luck, rather than the
            # location filter by design.
            #
            # So the country is evidence only when the city agrees. A real
            # Saudi row carries a Saudi region code where an American one
            # carries a state code -- 'Riyadh، RIY' and 'Al Khubar، EAS'
            # against 'Los Ángeles، CA'. That code is what keeps the real rows
            # whose transliteration LOCATION_INCLUDE has never heard of.
            place = " ".join(p for p in (city, state) if p)
            if _names_somewhere_saudi(place):
                location = ", ".join(p for p in (city, state, country) if p)
            else:
                # Drop the country rather than the row: matches_filters is the
                # one place that decides, and a row with a real city elsewhere
                # in Saudi Arabia can still match on the city alone.
                location = place

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
            "first_published": str(publication_day(j.get("job_posted_at_datetime_utc")) or ""),
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

            # Judged on the link the candidate would be handed, so the free
            # alternative above still saves a posting whose primary link is
            # paywalled. Nothing past this point ever sees the rest.
            if on_paid_relister(apply_link(job)):
                dropped.append(job)
                if paywalled is not None and matches_filters(job):
                    paywalled.append(job)
                continue
            out.append(job)
            added += 1
        return added

    selected_queries = focused_query_plan(
        AGGREGATOR_QUERIES, JSEARCH_DAILY_REQUESTS,
        riyadh_ordinal())
    bootstrap_allowed = True
    for query in selected_queries:
        if out_of_allowance("jsearch", quota, allow_unknown=bootstrap_allowed,
                            paused_feeds=paused_feeds):
            log("JSearch: quota-paused; continuing without the aggregator")
            break
        jobs, _ = read_page(query)
        bootstrap_allowed = False
        log(f"JSearch [{query!r}]: {len(jobs)} returned, {collect(jobs)} unique")
        time.sleep(0.5)  # be polite between queries

    if dropped:
        log(f"JSearch refused {len(dropped)} paywalled postings on "
            f"{', '.join(sorted({link_host(j['url']) for j in dropped}))}"
            + (f"; {len(paywalled)} of them matched the filters and go to board "
               f"discovery" if paywalled else ""))
    if "requests_remaining" in quota:
        log(f"JSearch quota: {quota['requests_remaining']} of 200 requests remaining")
    log(f"JSearch total: {len(out)} unique jobs across {len(selected_queries)} queries")
    return out


def jsearch_query_plan(queries, budget, day):
    """Return a fair rotating slice of page-one queries for this Riyadh day."""
    count = min(max(0, budget), len(queries))
    if not queries or not count:
        return []
    start = (day * count) % len(queries)
    return [queries[(start + i) % len(queries)] for i in range(count)]


def focused_query_plan(queries, budget, day):
    count = min(max(0, budget), 6, len(queries))
    if len(queries) <= 4:
        return jsearch_query_plan(queries, count, day)
    primary, remaining = queries[:4], queries[4:]
    plan = [primary[day % len(primary)], remaining[day % len(remaining)]]
    for query in jsearch_query_plan(queries, len(queries), day):
        if query not in plan:
            plan.append(query)
    return plan[:count]


def primary_title_filter(day=None):
    priority = (riyadh_ordinal() if day is None else day) % 4 + 1
    titles = [title for title, value in PROFILE.get("title_priorities", {}).items() if value == priority]
    return " | ".join("'" + re.sub(r"[^a-z0-9 ]", " ", title.lower()) + "'" for title in titles) or ACTIVE_ATS_TITLE


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
        source_failure(c['name'], f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        source_failure(c['name'], f"WARN {c['name']}: pinpoint HTTP {r.status_code} (token '{c['token']}'). Skipping.")
        return []
    try:
        data = r.json()
    except ValueError:
        source_failure(c['name'], f"WARN {c['name']}: non-JSON response. Skipping.")
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


# Relisters that post OTHER companies' roles through a real ATS. The host is
# jobs.lever.co, so PAID_RELISTER_HOSTS never sees them and, worse, the source
# reads as an employer ATS -- which would make prefer_employer_postings favour
# the relisted copy over the employer's own link. Three of the ten rows in the
# first filtered probe (7 Sep 2026) were Jobgether. Refused server-side so they
# do not cost monthly allowance either.
ACTIVE_ATS_EXCLUDE_ORGS = ("Jobgether",)
EXCLUDED_ORGS_PARAM = ",".join(ACTIVE_ATS_EXCLUDE_ORGS)

# What each feed's FREE plan actually allows in a month, and how much of it to
# keep back. Every one of these is smaller than a daily run over a 30-day month,
# so none of them can simply be spent as it arrives:
#
#   Active Jobs DB   250 jobs, 25 requests   (two separate hard caps)
#   LinkedIn feed    250 jobs, 25 requests   (same vendor, metered separately)
#   Indeed feed       50 requests            (no job cap published)
#   JSearch          200 requests            (rationed by JSEARCH_DAILY_REQUESTS)
#
# The reserve is what stays unspent so a manual re-run or a probe does not take
# a source out for the rest of the month. Values are floors, not budgets: the
# real spend is one request per feed per run.
FREE_TIER = {
    "jsearch":      {"requests": 200, "jobs": None, "reserve_requests": 40, "reserve_jobs": None},
    "active_ats":   {"requests": 25, "jobs": 250, "reserve_requests": 5, "reserve_jobs": 30},
    "linkedin_api": {"requests": 25, "jobs": 250, "reserve_requests": 5, "reserve_jobs": 30},
    "indeed":       {"requests": 50, "jobs": None, "reserve_requests": 10, "reserve_jobs": None},
}


def rapidapi_enabled(raw):
    """Whether this run may spend from a RapidAPI subscription."""
    if raw is None:
        return True
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def riyadh_ordinal(now=None):
    """The calendar day used for source cadence, regardless of runner timezone."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    riyadh = datetime.timezone(datetime.timedelta(hours=3))
    return now.astimezone(riyadh).date().toordinal()


def fresh_api_feeds(day=None):
    """Stagger the two 25-request feeds; each runs every other Riyadh day."""
    if day is None:
        day = riyadh_ordinal()
    return ("active_ats",) if day % 2 == 0 else ("linkedin_api",)


def can_spend(feed, quota, cost=1, job_cost=0):
    """Whether a manual probe may make `cost` paid requests safely.

    Scheduled digests are allowed one bootstrap response when their persisted
    state is missing, because that response is how they learn the balance.
    A probe cannot make that assumption: it is discretionary, so unknown or
    expired state fails closed before it spends anything.
    """
    plan = FREE_TIER.get(feed)
    if plan is None or not isinstance(quota, dict):
        return False
    if not isinstance(cost, int) or cost < 1:
        return False
    reset_at = quota.get("reset_at")
    if (not isinstance(reset_at, (int, float)) or time.time() >= reset_at):
        return False
    requests_remaining = quota.get("requests_remaining")
    if not isinstance(requests_remaining, int):
        return False
    if requests_remaining - cost < plan["reserve_requests"]:
        return False
    jobs_remaining = quota.get("jobs_remaining")
    reserve_jobs = plan["reserve_jobs"]
    if reserve_jobs is not None:
        if (not isinstance(jobs_remaining, int)
                or not isinstance(job_cost, int) or job_cost < 0
                or jobs_remaining - job_cost < reserve_jobs):
            return False
    return True


def run_paid_probe(feed, cost, probe, *args, job_cost=0, **kwargs):
    """Run an explicitly approved probe without crossing a known reserve.

    Probes are optional diagnostics, unlike the scheduled digest. They therefore
    fail closed when the state is absent, stale, or cannot cover their complete
    predicted request count. The probe receives the mutable per-feed state so
    every response can record its quota headers, then no state except that
    quota ledger is saved.
    """
    print(f"RapidAPI {feed} probe predicted cost: {cost} request(s)")
    confirmed = os.environ.get("CONFIRM_RAPIDAPI_SPEND", "").strip().lower()
    if confirmed not in {"1", "true", "yes", "on"}:
        print("RapidAPI spend is unconfirmed; refusing the probe before any request.")
        return 1
    quotas = load_api_quotas()
    quota = quotas.get(feed)
    if not can_spend(feed, quota, cost, job_cost):
        print(f"RapidAPI {feed} probe would cross its reserve or has unknown state; refusing.")
        return 1
    result = probe(*args, quota=quota, **kwargs)
    save_api_quotas(quotas)
    return result


def out_of_allowance(feed, quota, request_cost=1, job_cost=0,
                     allow_unknown=True, paused_feeds=None):
    """True when this feed should sit today's run out.

    Unknown reads as available, never as empty: a first run or a lost state
    file would otherwise disable the only sources that can answer "posted this
    week", permanently, on no evidence at all.

    A feed under its reserve stops calling -- which means it stops learning,
    including that the plan has rolled over. Left there it would stay off for
    good. RapidAPI reports seconds-until-reset, stored as an absolute instant
    when it arrives, so a quota past that instant counts as unknown again.
    """
    plan = FREE_TIER.get(feed)
    if plan is None:
        # An unnamed feed has no known plan, so nothing here can say it is safe
        # to spend. Refusing is the conservative direction.
        log(f"WARN {feed}: no free-tier plan recorded; not spending")
        if paused_feeds is not None and feed not in paused_feeds:
            paused_feeds.append(feed)
        return True
    reset_at = quota.get("reset_at")
    if isinstance(reset_at, (int, float)) and time.time() >= reset_at:
        paused = not allow_unknown
        if paused and paused_feeds is not None and feed not in paused_feeds:
            paused_feeds.append(feed)
        return paused
    unknown = False
    for counter, floor, cost in (("requests_remaining", plan["reserve_requests"], request_cost),
                                 ("jobs_remaining", plan["reserve_jobs"], job_cost)):
        if floor is None:
            continue
        remaining = quota.get(counter)
        if isinstance(remaining, int) and remaining - cost < floor:
            log(f"{feed}: {remaining} {counter.split('_')[0]} left this month, "
                f"below the {floor} reserve; skipping")
            if paused_feeds is not None and feed not in paused_feeds:
                paused_feeds.append(feed)
            return True
        if not isinstance(remaining, int):
            unknown = True
    if unknown and not allow_unknown:
        log(f"{feed}: quota response omitted a remaining counter; pausing")
        if paused_feeds is not None and feed not in paused_feeds:
            paused_feeds.append(feed)
        return True
    return False

# The allowance has to be known BEFORE the request that would exceed it, and the
# headers only arrive on a response -- so what one run learned is written down
# for the next. Its own file rather than a stats.json row: stats rows are pruned
# on a TTL and a run can end at any of four save_stats call sites, while this has
# to survive both.
ACTIVE_ATS_STATE_FILE = "active_ats.json"
ACTIVE_ATS_PAGE = 10


def load_api_quotas():
    """Last known monthly allowance per feed. Unreadable reads as no knowledge.

    Never as no allowance: assuming zero would disable the sources that can
    answer "posted this week", permanently, on nothing more than a lost file.

    Keyed by feed because RapidAPI meters PER API -- one shared counter would
    make each feed's reserve fire on the other's spending.
    """
    if not os.path.exists(ACTIVE_ATS_STATE_FILE):
        return {}
    try:
        with open(ACTIVE_ATS_STATE_FILE) as handle:
            quotas = json.load(handle)
    except (OSError, ValueError) as e:
        log(f"WARN API quota state unreadable ({e}); treating it as unknown")
        return {}
    if not isinstance(quotas, dict):
        return {}
    # The file started as a single feed's quota, before there were two. An old
    # one holds that feed's counters at the top level; read it as such rather
    # than discarding a still-valid allowance.
    if "jobs_remaining" in quotas or "requests_remaining" in quotas:
        return {"active_ats": quotas}
    return {k: v for k, v in quotas.items() if isinstance(v, dict)}


def save_api_quotas(quotas):
    if not quotas:
        return
    with open(ACTIVE_ATS_STATE_FILE, "w") as handle:
        json.dump(quotas, handle, indent=0, sort_keys=True)


def fetch_active_ats(api_key, quota=None, max_age_days=None, paused_feeds=None):
    """Read one page of Saudi technical roles from the employers' own ATSs.

    Measured 7 Sep 2026, same day and same country: JSearch returned ZERO jobs
    at date_posted=week and a corpus aged 18-159 days, while this feed returned
    ten technical roles aged 0-1 days, every one on the employer's own board.
    That is the entire reason it exists -- the aggregator cannot answer "posted
    this week" at all, which is what the digest is asked for.
    """
    _, since = _age_window(max_age_days)
    query = ("?source=" + requests.utils.quote(ACTIVE_ATS_SOURCES)
             + "&location=" + requests.utils.quote("Saudi Arabia")
             + "&title_advanced=" + requests.utils.quote(primary_title_filter())
             + "&exclude_organization=" + requests.utils.quote(EXCLUDED_ORGS_PARAM)
             + "&date_posted_gte=" + since + "T00:00:00"
             + "&time_frame=7d"
             + "&description_format=text&limit=" + str(ACTIVE_ATS_PAGE))
    return _fetch_fantastic("Active ATS", ACTIVE_ATS_HOST, "/active-ats", query,
                            "active_ats", api_key, quota, paused_feeds)


def fetch_linkedin(api_key, quota=None, max_age_days=None, paused_feeds=None):
    """Read one page of Saudi technical roles as LinkedIn itself lists them.

    Probed 7 Sep 2026: ten rows, all posted that day, nine of them genuinely
    technical -- Tableau Developer at TCS, ROS2 and Embedded Firmware at Atwad,
    Data Science at stc, Backend Golang. JSearch, asked for the same country on
    the same day, returned nothing at all inside a week. This is the same
    LinkedIn the digest already delivers, minus the staleness of reaching it
    through Google for Jobs.

    Its rows are relays, not the employer's own posting, so this source stays
    OUT of EMPLOYER_SOURCES: prefer_employer_postings must keep favouring a real
    board when both have the role.

    Three of the ten probe rows were Morocco despite location=Saudi Arabia, so
    location_conflict is what catches the leak; it already exists for exactly
    this.
    """
    _, since = _age_window(max_age_days)
    query = ("?location=" + requests.utils.quote("Saudi Arabia")
             + "&title_advanced=" + requests.utils.quote(primary_title_filter())
             + "&exclude_organization=" + requests.utils.quote(EXCLUDED_ORGS_PARAM)
             + "&date_posted_gte=" + since + "T00:00:00"
             + "&time_frame=7d"
             + "&description_format=text&limit=" + str(ACTIVE_ATS_PAGE))
    return _fetch_fantastic("LinkedIn", LINKEDIN_API_HOST, "/active-jb", query,
                            "linkedin_api", api_key, quota, paused_feeds)


def _age_window(max_age_days):
    """The publication lookback and its exact date cutoff.

    The feeds use a fixed 7d indexed-time window independently of this
    publication cutoff. A 30-day fallback policy must not become the
    unsupported time_frame=30d; older postings can still arrive from boards
    or when newly indexed by the feeds.
    """
    days = max_age_days if isinstance(max_age_days, int) and max_age_days > 0 else 7
    return days, (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


# Indeed, which nothing else in this stack reaches. A separate endpoint per
# platform rather than one blended index.
INDEED_HOST = "jobs-api14.p.rapidapi.com"


# What this feed is asked for. Indeed has no taxonomy filter here and no
# working date filter, so the query carries the terms and everything else is
# judged locally.
def fetch_indeed(api_key, quota=None, max_age_days=None, paused_feeds=None):
    """Read one page of Saudi roles from Indeed, which no other feed reaches.

    Probed 7 Sep 2026: 20 rows, 8 passing the filters, 7 of those published
    within a week, and Saudi employers the ATS feeds do not carry -- Rasan and
    Consulum among them.

    Two things this endpoint does not give, both confirmed live rather than
    assumed. There is no server-side date filter: it answered "Parameter
    'datePosted' is unknown and was ignored", so `max_age_days` cannot be
    pushed to the server and split_by_age does the whole job. And there is no
    single date -- it returns the employer's publish time and Indeed's own
    index time, which disagreed by 76 days on the first row sampled.

    Fail-soft like every other fetcher: any error is [] and a log line.
    """
    if not api_key:
        return []
    quota = quota if quota is not None else {}
    if out_of_allowance("indeed", quota, paused_feeds=paused_feeds):
        return []
    url = ("https://" + INDEED_HOST + "/v2/indeed/search"
           + "?query=" + requests.utils.quote(focused_query_plan(AGGREGATOR_QUERIES, 2, riyadh_ordinal())[-1])
           + "&location=" + requests.utils.quote("Saudi Arabia")
           + "&countryCode=sa")
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": INDEED_HOST}, timeout=30)
    except requests.RequestException as e:
        source_failure('indeed', "WARN Indeed: request failed: " + str(e))
        return []
    recorded = _record_quota(quota, r.headers)
    _debit_unreported_quota(quota, recorded)
    if r.status_code != 200:
        source_failure('indeed', "WARN Indeed: HTTP " + str(r.status_code) + ": " + r.text[:120])
        return []
    try:
        payload = r.json()
    except ValueError:
        source_failure('indeed', "WARN Indeed: non-JSON response")
        return []
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        company = row.get("company")
        if isinstance(company, dict):
            company = company.get("name")
        where = row.get("location")
        if isinstance(where, dict):
            where = ", ".join(str(v) for v in
                              (where.get("location"), where.get("country")) if v)
        out.append({
            "company": str(company or "").strip(),
            "id": str(row.get("id") or ""),
            "title": str(row.get("title") or "").strip(),
            "location": str(where or "").strip(),
            "url": row.get("applyUrl") or "",
            # The EMPLOYER's clock. dateOnIndeedTimestamp is when Indeed
            # noticed the posting, which for the first row probed was today
            # against a publish date in June -- reading it would let a
            # months-old role pass a seven-day cutoff as same-day. No date at
            # all is better than the wrong one: an undated posting is withheld.
            "first_published": _epoch_ms_date(row.get("datePublishedTimestamp")),
            "source": "indeed", "token": "",
            "description": str(row.get("description") or "").strip(),
        })
    log("Indeed: " + str(len(out)) + " jobs ("
        + str(quota.get("requests_remaining", "?")) + " requests left this month)")
    return out


def _epoch_ms_date(stamp):
    """An epoch-milliseconds timestamp as an ISO date, or "" when unusable."""
    if not isinstance(stamp, (int, float)):
        return ""
    try:
        return datetime.datetime.fromtimestamp(
            stamp / 1000, datetime.timezone(datetime.timedelta(hours=3))).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _record_quota(quota, headers):
    """Copy what the response says about the plan into the run's state.

    The reset header is seconds-until-reset, which means nothing once the run
    has ended, so it is stored as an absolute instant instead. That is what
    lets a feed held back under its reserve start again after the rollover
    rather than staying off for good.
    """
    recorded = set()
    for header, field in (("x-ratelimit-jobs-remaining", "jobs_remaining"),
                          ("x-ratelimit-requests-remaining", "requests_remaining")):
        try:
            quota[field] = int(headers[header])
            recorded.add(field)
        except (KeyError, TypeError, ValueError):
            pass
    quota["observed_at"] = time.time()
    try:
        quota["reset_at"] = time.time() + int(headers["x-ratelimit-requests-reset"])
    except (KeyError, TypeError, ValueError):
        pass
    return recorded


def _debit_unreported_quota(quota, recorded, request_cost=1, job_cost=0):
    """Conservatively charge a request when RapidAPI omitted its counters."""
    for field, cost in (("requests_remaining", request_cost),
                        ("jobs_remaining", job_cost)):
        if field in recorded or not isinstance(cost, int) or cost <= 0:
            continue
        remaining = quota.get(field)
        if isinstance(remaining, int):
            quota[field] = max(0, remaining - cost)


def _fetch_fantastic(name, host, path, query, source, api_key, quota,
                     paused_feeds=None):
    """One page from a Fantastic.jobs feed, shaped into this pipeline's job dict.

    Both feeds share a vendor, a row shape and a plan: 250 JOBS and 25 REQUESTS
    a month, two separate hard limits, metered PER API. So the filtering runs
    server-side -- every row the API rejects is allowance kept -- and each feed
    carries its own `quota`, read and updated in place the way fetch_jsearch
    tracks its headers. Sharing one counter would make each feed's reserve fire
    on the other's spending.

    Fail-soft like every other fetcher: any error is [] and a log line.
    """
    if not api_key:
        return []
    quota = quota if quota is not None else {}
    if out_of_allowance(source, quota, job_cost=ACTIVE_ATS_PAGE,
                        paused_feeds=paused_feeds):
        return []
    url = "https://" + host + path + query
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": host}, timeout=30)
    except requests.RequestException as e:
        source_failure(source, "WARN " + name + ": request failed: " + str(e))
        return []
    recorded = _record_quota(quota, r.headers)
    _debit_unreported_quota(quota, recorded, job_cost=ACTIVE_ATS_PAGE)
    if r.status_code != 200:
        source_failure(source, "WARN " + name + ": HTTP " + str(r.status_code) + ": " + r.text[:120])
        return []
    try:
        rows = r.json()
    except ValueError:
        source_failure(source, "WARN " + name + ": non-JSON response")
        return []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("jobs") or []
    refused = {org.casefold() for org in ACTIVE_ATS_EXCLUDE_ORGS}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = (row.get("organization") or "").strip()
        # exclude_organization is exact and case-sensitive on their side, so a
        # spelling they treat as different still reaches us. Refuse it here too.
        if company.casefold() in refused:
            continue
        # The index saying the posting has expired. Free -- it arrives on a row
        # already paid for -- and liveness is exactly what failed on 7 Sep.
        # Usually absent, so this only ever adds a drop; an unparseable value is
        # ignored rather than trusted, because guessing "expired" wrong hides a
        # live role.
        expires = str(row.get("date_valid_through") or "")[:10]
        if expires:
            try:
                if datetime.date.fromisoformat(expires) < datetime.date.today():
                    log("DROP expired " + expires + ": " + company
                        + " - " + str(row.get("title")))
                    continue
            except ValueError:
                pass
        locations = row.get("locations_derived") or []
        if not isinstance(locations, list):
            locations = [locations]
        out.append({
            "company": company,
            "id": str(row.get("id") or ""),
            "title": (row.get("title") or "").strip(),
            "location": ", ".join(str(part) for part in locations if part),
            "url": row.get("url") or "",
            # date_posted is when the employer published; date_created is when
            # Fantastic.jobs indexed it. The gate reads the employer's clock.
            "first_published": str(publication_day(row.get("date_posted")) or ""),
            "source": source, "token": "",
            "description": (row.get("description_text") or "").strip(),
        })
    log(name + ": " + str(len(out)) + " jobs ("
        + str(quota.get("jobs_remaining", "?")) + " jobs, "
        + str(quota.get("requests_remaining", "?")) + " requests left this month)")
    return out


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


# The regions JSearch reports for Saudi postings, as the code trailing the city
# ('Riyadh، RIY', 'Al Khubar، EAS'). Kept because the transliterations vary far
# more than the codes do: LOCATION_INCLUDE knows "khobar" but not "Al Khubar",
# and nothing would recognise "al-'Aqrabiyyah" or "Najd" without this.
SAUDI_REGION_CODES = frozenset({
    "RIY",  # Riyadh
    "MAK",  # Makkah
    "MAD",  # Al Madinah
    "EAS",  # Eastern Province
    "ASI",  # 'Asir
    "TAB",  # Tabuk
    "HAI",  # Ha'il
    "NBO",  # Northern Borders
    "JAZ",  # Jazan
    "NAJ",  # Najran
    "BAH",  # Al Bahah
    "JAW",  # Al Jawf
    "QAS",  # Al Qassim
})


def _names_somewhere_saudi(place):
    """True when the city/state text itself says Saudi Arabia.

    Deliberately does NOT read the country: JSearch echoes the requested
    country onto every row, so it cannot distinguish Riyadh from Los Angeles.
    """
    if not place:
        return False
    lowered = place.casefold()
    if any(k in lowered for k in LOCATION_INCLUDE):
        return True
    # The region code trails the city after an Arabic comma.
    tail = re.split(r"[،,]", place)[-1].strip().upper()
    return tail in SAUDI_REGION_CODES


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
    if posting_location_conflict(job):
        return False
    if not any(k in loc for k in LOCATION_INCLUDE):
        return False
    if location_conflict(job["title"]):
        return False
    if any(k in title for k in TITLE_EXCLUDE):
        return False
    if any(k in title for k in TITLE_EXCLUDE_DISCIPLINE):
        return False
    if (any(k in title for k in ("junior", "associate", "entry", "fresh"))
            and any(k in title for k in ("data analyst", "analytics", "business intelligence", "power bi"))):
        return False
    if not (any(k in title for k in TITLE_INCLUDE)
            or keyword_hits(title, [t.lower() for t in PROFILE.get("title_priorities", {})])):
        return False
    return True


def posting_location_conflict(job):
    countries = job.get("posting_countries") or []
    return bool(countries and not any(
        country.upper() == "SA" or "saudi" in country.lower() or "السعودية" in country
        for country in countries))


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
# active_ats is not in FETCHERS -- it is one feed, not a per-company board -- but
# every row it returns IS the employer's own ATS posting, so it belongs to the
# set prefer_employer_postings favours over an aggregator relay.
EMPLOYER_SOURCES = frozenset(FETCHERS) | {"active_ats"}


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


@functools.lru_cache(maxsize=None)
def _keyword_pattern(keyword):
    """A keyword matches at a word start, with the suffix left free.

    'agent' must still match 'Agentic' and 'data' must still match
    'Datacentre', so there is no trailing boundary. The leading one is what
    stops 'api' matching 'Scraping' and 'ml' matching 'AML'. Measured on the
    91 archived titles at ab0eb45: 6 spurious hits removed, 0 real hits lost;
    requiring both boundaries would have cost 2 real ones. See plans/017.

    re.escape is required, not optional: 'full-stack', 'full stack' and
    'forward deployed' are all live keywords holding regex metacharacters.
    """
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword))


def keyword_hits(title, keywords):
    """How many of `keywords` start a word in `title`."""
    lowered = title.lower()
    return sum(1 for k in keywords if _keyword_pattern(k).search(lowered))


def keyword_score(job):
    hits = keyword_hits(job["title"], PROFILE["skill_keywords"])
    return min(100, 40 + hits * 15)  # 40 base for passing filters, +15 per skill hit


# The four dimensions the ranker reports alongside its number, with the rubric
# weights Watheq uses. Ported from its MATCH_SCORING_RUBRIC, and education is
# the one that matters most here: it is the dimension a candidate with no
# bachelor's degree keeps being scored against, and it was never asked about.
RANK_CATEGORIES = MATCH_CATEGORIES

_CATEGORY_SCORE = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "max": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["score", "max", "note"],
    "additionalProperties": False,
}

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
                    "track": {"type": "string"},
                    "categories": {
                        "type": "object",
                        "properties": {name: _CATEGORY_SCORE for name in RANK_CATEGORIES},
                        "required": list(RANK_CATEGORIES),
                        "additionalProperties": False,
                    },
                },
                "required": ["id", "score", "why", "track", "categories"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scores"],
    "additionalProperties": False,
}


RESUME_EVIDENCE_CHARS = 8000


def load_resume_evidence(path="resume.md", limit=RESUME_EVIDENCE_CHARS):
    """The candidate's resume as scoring evidence, or "" when there is none.

    Deliberately never raises. `interactive_runner._master_document` raises on
    a missing resume because a person clicked a button and is waiting for an
    answer; this runs unattended every morning, so a bad read has to degrade to
    the previous blurb-only behaviour rather than take the whole digest down.

    The limit is headroom against an unbounded file, not a working truncation:
    resume.md is ~4.3 KB today.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()[:limit]
    except OSError:
        return ""


def describe_tracks(tracks):
    """The candidate's target roles, grouped by what evidences each of them.

    A flat title list said what he wants and nothing about which part of the
    resume backs it. A Senior Data Analyst posting was then judged against a
    profile that opens with LLM systems, and an AI posting against six years of
    Power BI. Grouping lets the ranker pick the track first and score the job
    against that track's evidence.

    Each track POINTS at the resume rather than restating it, and that is the
    whole discipline here. This text lands inside <candidate_preferences>, which
    the prompt tells the model proves nothing about capability -- so a paragraph
    asserting what the candidate has built would either be discounted, doing
    nothing, or believed, putting back exactly the self-authored capability
    claims plan 018 pulled out of the blurb. Naming which resume entry to judge
    a track by is routing, not evidence. A negative disclosure ("the resume
    shows no Spark or Kafka anywhere") is a constraint and belongs here too.

    One resume, several tracks. A second resume file would split the evidence
    in half and go stale independently; the per-job document is what the Tailor
    CV button produces at apply time.
    """
    if not tracks:
        return ""
    lines = []
    for track in tracks:
        titles = ", ".join(track.get("titles") or [])
        lines.append(f"- {track.get('name', '')}: {titles}.\n"
                     f"  Where to look: {track.get('evidence', '')}")
    return ("\n\nTarget tracks. Pick the ONE this posting belongs to, name it in "
            "`track`, and judge it against the part of the candidate_evidence "
            "block that track points at rather than against the whole resume at "
            "once. These lines say where to look; they prove nothing "
            "themselves:\n" + "\n".join(lines))


def rank_jobs(jobs, api_key, evidence=None):
    """One call. Returns {id: {score, why, track, categories}} or None on failure.

    The 2026-07-31 digest handed Lead/Staff roles 92-98 because the old prompt
    had no rubric: it asked for a 0-100 with no anchor, so everything AI-flavored
    scored 90+. The rubric below pins the bands and makes seniority-above-target
    a hard penalty.

    What the rubric could not fix on its own is what it was scoring. The excerpt
    used to be the first 900 characters of the posting, which on a McKinsey
    posting is the culture section and nothing else. It reads from the first
    heading that introduces the work now.

    That still walked away from the OTHER end. A posting states its Minimum
    Qualifications last, so on the 5187-character Ma'aden posting archived
    2026-09-08 the 2000-character window covered 217-2217: every
    responsibility, no degree, no year count, no fine-tuning. The model scored
    what it was shown, 91, while Watheq scored the same posting 67 on the full
    text. 42% of the postings in the archive state a degree this candidate does
    not hold.

    So every job now carries `hard_requirements`, read line by line from the
    whole description, and `coverage`, which says whether the excerpt is the
    posting or a window of it. `description_kind` could not answer that: it is
    computed on the stored text, so it reported "full" while the model was
    handed a third of one, on 48 of the 49 postings whose requirements the
    window cut. See plans/019.
    """
    profile = PROFILE["blurb"] + describe_tracks(PROFILE.get("tracks"))
    # The blurb is a paragraph the candidate wrote about himself, so every
    # keyword in it read as demonstrated capability. The resume goes in as
    # separately labelled evidence now, and the blurb keeps the job it is
    # actually good at: stating constraints and wants. See plans/018.
    evidence = load_resume_evidence() if evidence is None else evidence
    listing = []
    for job in sorted(jobs, key=lambda j: j["id"]):
        description = job.get("description") or ""
        excerpt = jd_signal(description, JD_RANK_CHARS)
        listing.append({
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "description_kind": jd_grade(description),
            # Computed on what is actually SENT. That is the whole point: the
            # grade above is computed on the stored text and cannot answer it.
            "coverage": "complete" if len(excerpt) >= len(description) else "windowed",
            "jd_excerpt": excerpt,
            "hard_requirements": jd_requirements(description),
        })
    # Omitted entirely rather than sent empty: a bare
    # <candidate_evidence></candidate_evidence> invites the model to read "no
    # evidence" as "no capability", which would tank every score on a day the
    # file went missing.
    evidence_block = (
        f"<candidate_evidence>\n{evidence}\n</candidate_evidence>\n"
        "The evidence block is the candidate's resume. It is the only thing "
        "here that proves capability, so judge every hard requirement against it.\n"
        "A term whose only appearance is under ## SUMMARY or ## SKILLS is a claim, "
        "not evidence -- anyone can type any keyword there. A term counts as "
        "demonstrated only when a ## EXPERIENCE or ## PROJECTS entry shows the "
        "candidate using it. A resume that lists every required tool and "
        "demonstrates none is a weak match on skills, not a strong one.\n\n"
    ) if evidence else ""
    weights = ", ".join(f"{name.replace('_', ' ')} {maximum}"
                        for name, maximum in RANK_CATEGORIES.items())
    prompt = (
        f"{evidence_block}"
        f"<candidate_preferences>\n{profile}\n</candidate_preferences>\n"
        "The preferences block states what the candidate wants and the constraints "
        "that rule a role out. It proves nothing about capability.\n\n"
        "Score each job 0-100 for fit to this candidate.\n"
        "Use the exact target track name in track; use an empty track for unrelated work. "
        "Junior and associate AI/application/software roles are valid targets when their "
        "requirements fit the candidate. Analytics and commercial targets are mid-level "
        "and senior IC. Sales and customer-advisory roles are also valid targets. "
        "Judge each role against the relevant track's evidence, not the resume headline.\n"
        f"Use this strict evidence-based rubric: {weights}. "
        "80+ means strong evidence-backed fit, 60-79 means competitive with gaps, below 60 "
        "means significant gaps. Never score above 90 unless every requirement "
        "the posting states is met with evidence from the candidate_evidence "
        "block. Reserve 95+ for a posting whose every stated requirement is met.\n"
        "Score 0-39 regardless of the rubric when the role is a "
        "leadership/staff/principal/management track, is based outside Saudi "
        "Arabia (including a title that says based elsewhere or offers "
        "relocation), or is the wrong function.\n"
        "Report each rubric category as an object with score, max and a note of "
        "at most 12 words. Set max to that category's weight above and score to "
        "an integer from 0 up to that same max. The four categories do NOT have "
        "to sum to the overall score: report each honestly and let the overall "
        "score be your own judgement.\n"
        "hard_requirements lists the lines where THIS posting states what it "
        "demands: a degree, a span of years, a certification, a must-have. They "
        "are read from the whole posting, including the parts jd_excerpt does not "
        "reach, so treat them as the authority on what the role requires. Weigh a "
        "stated degree, licence or year count as a requirement in its own right "
        "rather than folding it into the function judgement. Count years from what "
        "the evidence block actually dates, not from a total the preferences block "
        "claims. A stated credential the evidence does not show is a real gap and "
        "belongs in your reason; a requirement written as 'preferred', 'nice to "
        "have' or 'or equivalent experience' is not a hard one and must not be "
        "scored as a gap.\n"
        "jd_excerpt starts where the posting begins describing the job, so it is the "
        "requirements and responsibilities, not the company's recruiting copy. Score against "
        "what it states. A requirement neither jd_excerpt nor hard_requirements mentions is "
        "unknown, not a gap: never lower a score for something the posting simply did not say.\n"
        "coverage says whether jd_excerpt is the whole posting ('complete') or a window of it "
        "('windowed'). On a windowed posting, hard_requirements is what you know about its "
        "demands; do not assume the unseen part is either favourable or damning.\n"
        "description_kind tells you what you are reading. 'full' is a complete posting. "
        "'teaser' is a short aggregator summary and 'incomplete' is a fragment - for those two, "
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
            str(it["id"]): {"score": it["score"], "why": it["why"],
                            "track": str(it.get("track") or "")[:60],
                            "categories": it.get("categories")}
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
        # A provider snippet is not a completed fetch. Recover the full posting
        # before the completeness gate holds an otherwise relevant job forever.
        if jd_grade(job["description"]) != GRADE_FULL and job.get("url"):
            recovered = fetch_posting_page(job)
            if jd_grade(recovered) == GRADE_FULL:
                job["description"] = recovered
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
    job["posting_countries"] = posting.get("posting_countries") or job.get("posting_countries", [])
    record_apply_link(job, posting["employer_url"], posting["employer_token"])
    # A related-jobs link can point at a different employer. Refusing that
    # apply link must also refuse text recovered through the same ATS hop.
    if (posting.get("text_source") == "ats" and posting["employer_url"]
            and canonical_url(posting["employer_url"]) != canonical_url(job.get("url"))
            and not employer_matches_token(job.get("company"), posting["employer_token"])):
        return ""
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
        job["posting_countries"] = jsonld_posting_countries(page_html)
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


def valid_categories(raw):
    """The rubric categories the model reported, or {} if they do not check out.

    Two rules, both Watheq's, and the second is the load-bearing one.

    Validate score-within-max, never a specific max. Watheq's own evaluation
    harness records that the model emits each category on its own 0-100 scale
    about as often as on the stated weight, and the display only ever uses the
    ratio, so pinning the max would reject honest answers.

    Never reconcile the four against the overall score. Watheq documents that
    the models' sums run 30+ points above their stated overall on partial
    matches, so forcing agreement is what pushes a weak resume into the
    hireable band. They are reported alongside the number, not folded into it.
    """
    if not isinstance(raw, dict):
        return {}
    checked = {}
    for name in RANK_CATEGORIES:
        part = raw.get(name)
        if not isinstance(part, dict):
            return {}
        try:
            score, maximum = int(part["score"]), int(part["max"])
        except (KeyError, TypeError, ValueError):
            return {}
        if not 0 < maximum <= 100 or not 0 <= score <= maximum:
            return {}
        checked[name] = {"score": score, "max": maximum,
                         "note": str(part.get("note") or "").strip()[:80]}
    return checked


def apply_ranking(jobs, ranking):
    """Attach validated model scores or deterministic fallbacks to every job."""
    for job in jobs:
        result = ranking.get(job["id"]) if isinstance(ranking, dict) else None
        try:
            score = result["score"]
            why = result["why"]
            valid = (type(score) is int and 0 <= score <= 100
                     and isinstance(why, str) and bool(why.strip()))
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            # Categories are additive evidence, so a malformed set costs the
            # breakdown and never the score: the number is the model's own
            # judgement and does not depend on them.
            job.update(score=score, why=why.strip()[:140], ranking_verified=True,
                       track=str(result.get("track") or "").strip()[:60],
                       categories=valid_categories(result.get("categories")))
        else:
            job.update(
                score=keyword_score(job),
                why="matches your target titles and skills",
                ranking_verified=False,
                track="",
                categories={},
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
        # Same reason as cap_blocked_scores: trim the reason, keep the suffix.
        suffix = " (capped: description is a short teaser)"
        job["why"] = job["why"][:140 - len(suffix)].rstrip() + suffix
        capped.append(job)
    return capped


def cap_blocked_scores(jobs):
    """Hold a job with verified requirement gaps down to what they allow.

    The mirror of cap_teaser_scores. That one says the description could not
    back the score up; this one says the description DID, and the resume could
    not meet it. Returns the jobs it capped.

    The ceiling comes from `blocked_score_ceiling`, so a single verified missing
    degree caps on its own. It used to take a "low" ceiling -- two blockers, or
    one the model happened to call critical -- which is why a Ma'aden posting
    stating a bachelor's degree the candidate does not hold was delivered at 91
    with nothing on the card saying so.

    A check that failed, timed out or never ran reports `unknown` and no
    blockers, so it produces no ceiling and caps nothing. Silence means "no
    verdict", never "bad verdict", or a provider outage would quietly demote
    every strong match in the digest.
    """
    capped = []
    for job in jobs:
        check = job.get("fit_check") or {}
        blockers = check.get("blockers") or []
        ceiling = fit_check.blocked_score_ceiling(blockers)
        if ceiling is None or job["score"] <= ceiling:
            continue
        job["score"] = ceiling
        # Trim the reason, not the suffix. apply_ranking already truncates `why`
        # to exactly 140, so appending and slicing the result silently ate the
        # explanation on a full-length reason -- leaving a card that reads
        # "fit 69" with a reason written for an 88 and nothing saying why.
        suffix = f" (capped: {len(blockers)} verified requirement gap"
        suffix += ")" if len(blockers) == 1 else "s)"
        job["why"] = job["why"][:140 - len(suffix)].rstrip() + suffix
        capped.append(job)
    return capped


def remember(seen, job, today):
    """Record a posting so no later run reconsiders it.

    Both the identity and the description are keyed, because an aggregator
    relists the same posting under a fresh URL and only the description
    fingerprint catches that.
    """
    prefix = "pending:" if job.get("application_state") == "needs_checking" else ""
    seen[prefix + job_identity(job)] = today
    if apply_link(job):
        seen[prefix + "url:" + canonical_url(apply_link(job))] = today
    key = description_key(job)
    if key:
        seen[prefix + key] = today


def defer_below_floor(seen, job, today):
    """Skip this posting for a week rather than retiring it for four months.

    A below-floor drop used to go through `remember`, which writes the job under
    STATE_TTL_DAYS. Given the score instability the archives already record --
    one QuantumBlack posting scored 40, 40, 40, 45, 50, 50 and 75 on a
    byte-identical description -- one bad morning retired a real job until
    January.

    A cooldown rather than no memory at all. Not remembering it would work, but
    the same 15-20 rejects would be re-fetched, re-enriched and re-ranked every
    single morning for a verdict already reached, which costs description
    fetches and ranking tokens daily. A week gives the second chance without
    paying for it every day.

    Both keys, like `remember`, and for the same reason. An aggregator relists
    the same posting under a fresh URL, which gives it a fresh identity -- 65 of
    the first 122 aggregator cards were repeats and one role arrived under 11
    ids in 9 days. Keying only on identity would let a below-floor posting come
    back tomorrow as a stranger, be enriched and re-ranked, and cost exactly the
    daily bill this cooldown exists to avoid.
    """
    for key in _floor_keys(job):
        seen[key] = today


def _floor_keys(job):
    """The cooldown keys for one posting: its identity, and its description.

    Both carry FLOOR_SEEN_PREFIX so `save_seen` prunes them on the seven-day
    clock rather than the state TTL. `description_key` is falsy for a job with
    no description text, which is why this is a list rather than a pair.
    """
    keys = [FLOOR_SEEN_PREFIX + job_identity(job)]
    described = description_key(job)
    if described:
        keys.append(FLOOR_SEEN_PREFIX + described)
    return keys


def still_cooling(seen, job, today):
    """Is this posting inside the cooldown a below-floor score gave it?

    Either key answering is enough: a relisted copy matches on its description
    while its identity has changed.
    """
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=FLOOR_COOLDOWN_DAYS)).isoformat()
    return any(seen.get(key, "") >= cutoff for key in _floor_keys(job))


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
        if job.get("ranking_verified") and job["score"] < max(MIN_DELIVER_SCORE, STRONG_MATCH_MIN_SCORE):
            dropped.append(job)
        else:
            kept.append(job)
    return kept, dropped


def delivery_priority(job):
    """Strong matches first; then recency, requested role priority, and fit."""
    priorities = PROFILE.get("title_priorities", {})
    role_priority = min((priority for title, priority in priorities.items()
                         if keyword_hits(job.get("title", ""), [title.lower()])), default=99)
    state_order = 2 if job.get("application_state") == "needs_checking" else (0 if is_strong(job) else 1)
    return (state_order, (job.get("age_days") or 0) > MAX_JOB_AGE_DAYS, role_priority,
            -job.get("score", 0), str(job.get("id", "")))


def cap_per_company(jobs, limit=MAX_JOBS_PER_COMPANY):
    """Keep at most `limit` roles per employer, recent matches before older ones.

    Returns (kept, deferred). The deferred ones are NOT remembered, on the same
    reasoning as a paid-relister drop: this is a verdict on the shape of today's
    digest, not on the role. A fourth good role at one employer is worth seeing
    tomorrow, when the batch that buried it is no longer new.

    Employers are counted by `company_key`, because a board decorating the same
    name two ways -- "Zid" and "Zid | زد" -- would otherwise each get
    their own allowance and defeat the cap.
    """
    kept, deferred, counts = [], [], {}
    for job in sorted(jobs, key=delivery_priority):
        key = company_key(job.get("company")) or (job.get("company") or "")
        counts[key] = counts.get(key, 0) + 1
        (kept if counts[key] <= limit else deferred).append(job)
    return kept, deferred


TIER_STRONG = "Strong matches"
TIER_COMPETITIVE = "Competitive with gaps"
TIER_REVIEW = "Worth reviewing"
TIER_CLOSEST = "Closest matches to review"


def is_strong(job):
    """A high-confidence, JD-backed model match with nothing verified against it.

    Extracted from split_tiers so the reality check and the tier split cannot
    drift apart. The blocker clause is what stops the label contradicting the
    card under it: cap_blocked_scores has already pulled a gapped job's score to
    its ceiling, and every ceiling sits below STRONG_MATCH_MIN_SCORE, so this is
    belt and braces rather than a second opinion.
    """
    check = job.get("fit_check") or {}
    return bool(job.get("ranking_verified")
                and job["score"] >= max(MIN_DELIVER_SCORE, STRONG_MATCH_MIN_SCORE)
                and 0 <= (job.get("age_days") or 0) <= MAX_JOB_AGE_DAYS
                and jd_grade(job.get("description") or "") == GRADE_FULL
                and check.get("ceiling") == "high"
                and not check.get("note")
                and not check.get("unclear")
                and not check.get("blockers"))


def is_viable(job):
    """An evidence-backed review candidate; qualification gaps stay visible."""
    check = job.get("fit_check") or {}
    tracks = {track["name"] for track in PROFILE.get("tracks", [])}
    return bool(job.get("ranking_verified")
                and job["score"] >= 40
                and 0 <= (job.get("age_days") or 0) <= FALLBACK_MAX_JOB_AGE_DAYS
                and job.get("track") in tracks
                and jd_grade(job.get("description") or "") == GRADE_FULL
                and check.get("ceiling") in ("high", "mid", "low")
                and not check.get("note")
                and not any(b.get("type") in ("location_work_authorization", "role_scope_mismatch")
                            for b in check.get("blockers") or [])
                and any(e.get("source") == "resume" and e.get("snippet")
                        for strength in check.get("strengths") or []
                        for e in strength.get("evidence") or []))


def pending_reasons(job):
    reasons = []
    if jd_grade(job.get("description") or "") != GRADE_FULL:
        reasons.append("Full job description needs verification.")
    check = job.get("fit_check") or {}
    if check.get("note"):
        reasons.append("Requirements check unavailable: " + str(check["note"])[:160])
    elif check.get("ceiling", "unknown") == "unknown":
        reasons.append("Requirements and qualification gaps have not been checked.")
    if not any(e.get("source") == "resume" and e.get("snippet")
               for strength in check.get("strengths") or [] for e in strength.get("evidence") or []):
        reasons.append("No positive match to demonstrated resume work has been verified.")
    return reasons


def is_pending(job):
    return bool(job.get("ranking_verified") and job.get("score", 0) >= 40
                and job.get("track") in {track["name"] for track in PROFILE.get("tracks", [])}
                and type(job.get("age_days")) is int and 0 <= job["age_days"] <= FALLBACK_MAX_JOB_AGE_DAYS
                and apply_link(job) and not on_paid_relister(apply_link(job))
                and job.get("link_status") != LINK_CLOSED and matches_filters(job)
                and not any(b.get("type") in ("location_work_authorization", "role_scope_mismatch")
                            for b in (job.get("fit_check") or {}).get("blockers") or [])
                and pending_reasons(job))


def pending_was_delivered(seen, job):
    return any("pending:" + key in seen for key in (
        job_identity(job), description_key(job), "url:" + canonical_url(apply_link(job))) if key)


def split_tiers(jobs):
    """Three tiers, matching the bands the ranker is now scored against.

    Two tiers stopped working the moment the scoring became honest. `is_strong`
    needs 80, and the new rubric plus the gap ceilings put a degree-gated role
    at 69 or below -- 42% of the archive states a degree this candidate does not
    hold, so "Strong matches" would have been a permanently empty heading above
    every card.

    The middle tier is the rubric's own "60-79, competitive with gaps" band. It
    is where a real match with a real gap belongs, and naming it is what stops a
    69 reading as a rejection.
    """
    strong, competitive, review = [], [], []
    for job in jobs:
        if is_strong(job):
            strong.append(job)
        elif job["score"] >= COMPETITIVE_MIN_SCORE:
            competitive.append(job)
        else:
            review.append(job)
    return strong, competitive, review


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

# LinkedIn's own markup for a closed posting. Its country subdomains localise every
# string on the page but not the class name, so this is the only marker that holds
# in every locale. Pinned here so a LinkedIn markup change fails a test loudly
# rather than silently reopening the hole it closed.
LINKEDIN_CLOSED_CLASS = "closed-job__flavor--closed"


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
    # LinkedIn serves a closed posting in the viewer's language, so CLOSED_MARKERS
    # never fires on the country subdomains JSearch relays for Saudi roles --
    # sa.linkedin.com says "لم نعد نقبل طلبات التقدم", not "no longer accepting
    # applications". Confirmed live 7 Sep 2026: the SAME posting read closed on
    # www.linkedin.com and open on sa.linkedin.com, and link_closed had been 0 on
    # every recorded day. The markup class is locale-independent and present on
    # both hosts, so match on that instead of on any English text. Scoped to the
    # host so no other board is judged by LinkedIn's class names.
    if link_host(final).endswith("linkedin.com") and LINKEDIN_CLOSED_CLASS in body:
        log(f"DROP linkedin posting closed: {url}")
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
    """Prune each kind of key on its own clock.

    A single cutoff was right while every key meant "delivered". A floor: key
    means "scored badly once", which is a much weaker claim and has to expire
    much sooner, or the cooldown is a 120-day retirement under a new name.
    """
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=STATE_TTL_DAYS)).isoformat()
    floor_cutoff = (today - datetime.timedelta(days=FLOOR_COOLDOWN_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items()
              if v >= (floor_cutoff if k.startswith("floor:") else cutoff)}
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


def board_discovery_seeds(paywalled, still_paid, matched):
    """The postings worth spending a board probe on, best candidate first.

    Discovery used to be seeded only from what the paid-relister cut discarded,
    which meant it only ever asked about employers whose links happened to sit
    on jobleads or jooble. By 7 Sep 2026 that had produced a ledger of Qualcomm,
    Lucid Motors, Delivery Hero and FileCloud -- 8 of the 10 boards found had
    ZERO open roles -- because a paid relister carries global postings that have
    nothing to do with hiring in Saudi. No amount of tuning the probe fixes a
    seed list that never contained a Saudi employer.

    A card that passed the filters is the opposite kind of evidence: a real
    Saudi role this candidate would be shown, relayed by an aggregator that has
    no employer board behind it yet. Its employer is precisely who to ask.

    Refused postings still come first. Only MAX_BOARD_PROBES run in a day, and
    finding a board for a posting the cut threw away recovers a role that would
    otherwise never be delivered, while an aggregator card was delivered anyway
    and only stands to get a better link tomorrow.
    """
    seen_keys = set()
    out = []
    for job in list(paywalled) + list(still_paid) + list(matched):
        # An employer-sourced job came FROM the board, so there is nothing to find.
        if job.get("source") in EMPLOYER_SOURCES:
            continue
        key = company_key(job.get("company"))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(job)
    return out


def confirmed_boards(boards, limit=MAX_BOARDS_PER_RUN):
    """Every discovered board as a COMPANIES entry, minus the ones already there.

    Least recently attempted first, then capped, with legacy last_read as a
    fallback. Failed attempts must rotate too: otherwise one throttled account
    would always precede boards that never got a request. Successful last_read
    remains separate from the attempt timestamp.

    The cap is a growth backstop rather than a ration -- board discovery adds
    entries every week with no ceiling of its own, and the company loop it feeds
    spends a request and half a second per entry against RUN_BUDGET_SECONDS.
    COMPANIES is never rationed.
    """
    already = {(c["source"], c["token"]) for c in COMPANIES}
    out = []
    for key in sorted(boards, key=lambda k: (boards[k].get("last_attempt") or boards[k].get("last_read") or "", k)):
        record = boards[key]
        if not record.get("source") or record.get("curated"):
            continue
        pair = (record["source"], record["token"])
        if pair in already:
            continue
        already.add(pair)
        out.append({"name": record.get("company") or record["token"],
                    "source": record["source"], "token": record["token"]})
    return out[:limit] if limit is not None else out


def board_read_queue(boards):
    by_pair = {(r.get("source"), r.get("token")): r for r in boards.values() if r.get("source")}
    return sorted(COMPANIES + confirmed_boards(boards), key=lambda c:
                  by_pair.get((c["source"], c["token"]), {}).get("last_attempt")
                  or by_pair.get((c["source"], c["token"]), {}).get("last_read") or "")
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


def source_yield(fetched, new=(), delivered=(), paused_feeds=(), sources=()):
    """Attribute deduplicated fetched jobs, fresh candidates and accepted cards."""
    counts = {feed: {"fetched": 0, "new": 0, "delivered": 0, "quota_paused": True}
              for feed in paused_feeds}
    for source in sources:
        counts.setdefault(source, {"fetched": 0, "new": 0, "delivered": 0, "quota_paused": False})
    for stage, jobs in (("fetched", fetched), ("new", new), ("delivered", delivered)):
        for job in jobs:
            row = counts.setdefault(job.get("source") or "unknown",
                                    {"fetched": 0, "new": 0, "delivered": 0, "quota_paused": False})
            row[stage] += 1
    return counts


def save_stats(stats, today, fetched, matched, new, avg_score, **metrics):
    """Record today's run and prune rows older than STATS_TTL_DAYS.
    avg_score is None on a no-new-jobs day (nothing was scored).

    One row per DATE, not per run. The workflow has a manual dispatch button,
    and 2026-08-08 was re-run four times, so appending gave a single day three
    of the seven slots in check_drift's trailing window. A re-run now replaces
    that day's row."""
    if avg_score is None:
        # A day's average is measured once and cannot be re-measured: a second
        # run finds every posting already in seen.json, reports 0 new, and would
        # otherwise write None over it. That corrupts the very history
        # check_drift reads. Happened on 2026-09-09, twice in one hour.
        avg_score = next((s.get("avg_score") for s in stats
                          if s["date"] == today), None)
    row = {
        "date": today, "fetched": fetched, "matched": matched,
        "new": new, "avg_score": avg_score,
        "unverified_date": 0, "unverified_fit": 0,
        "fallback_delivered": 0,
        "needs_checking_delivered": 0,
    }
    row.update(metrics)
    row["board_coverage"] = dict(BOARD_COVERAGE)
    for source, counts in sorted(metrics.get("source_yield", {}).items()):
        log(f"Source {source}: {counts['fetched']} fetched, {counts['new']} new, "
            f"{counts['delivered']} delivered"
            + ("; quota paused" if counts["quota_paused"] else ""))
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
        # Says what it measured, not what it guessed. avg_score is taken over
        # every new posting BEFORE the delivery floor, so it moves with what the
        # sources send: fetched went 198 to 738 a day when the LinkedIn, Indeed
        # and Bayt feeds landed and board discovery grew 3 confirmed boards to
        # 22, and below_floor went 0-2 to 15-20. Over the same stretch the best
        # delivered score never left 88-96. The old wording sent the reader to
        # profile.json, which was the one thing that had just been checked.
        return (
            f"Average fit of new postings down ({DRIFT_WINDOW_DAYS}d "
            f"{recent_avg:.0f} vs prior {prior_avg:.0f}). Broader sources, new "
            "employer boards, a scoring change or a stale profile — check "
            "fetched and below_floor in stats.json first."
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


def post_heartbeat(webhook, worker_url, worker_secret, today, scanned, matched, warnings,
                   *, newly_found=0, already_seen=0, stale=0, below_floor=0,
                   boards_read=0, boards_total=0, boards_skipped=0,
                   paused_feeds=(), unverified_date=0, unverified_fit=0,
                   incomplete_descriptions=0, source_failures=()):
    """Confirm a quiet day out loud.

    A green workflow run with no new roles used to exit silently, which is
    indistinguishable from a broken job. Seen postings stay separate from new
    postings rejected by the fit floor.
    """
    feed_names = {"jsearch": "JSearch", "active_ats": "Active ATS",
                  "linkedin_api": "LinkedIn", "indeed": "Indeed"}
    paused_names = ", ".join(feed_names.get(feed, feed) for feed in paused_feeds)
    lines = [
        f"**Job digest — {today}** · 0 delivered today.",
        f"Scanned {scanned} postings; {matched} matched the filters: "
        f"{already_seen} seen before, {newly_found} newly found; "
        f"{stale} older than {FALLBACK_MAX_JOB_AGE_DAYS}d, "
        f"{below_floor} rejected for role or qualification mismatch.",
        f"Excluded {unverified_date} with unverified posting dates; "
        f"{unverified_fit} awaiting complete fit verification.",
        f"Read {boards_read} of {boards_total} employer boards; "
        f"{boards_skipped} not read successfully. Coverage: {BOARD_COVERAGE}.",
        f"Quota-paused feeds: {paused_names or 'none'}.",
    ]
    lines.extend(f"⚠️ {warning}" for warning in warnings if warning)
    for line in lines[1:]:
        log(line)
    count = f"0 of {MAX_JOBS_IN_DIGEST}"
    status = f"No suitable new vacancies found ({count})."
    if incomplete_descriptions:
        status = f"Awaiting complete job descriptions: {incomplete_descriptions} ({count})."
    elif unverified_fit:
        status = f"Awaiting complete fit verification: {unverified_fit} ({count})."
    elif unverified_date:
        status = f"Unverified posting dates: {unverified_date} ({count})."
    elif below_floor:
        status = f"New vacancies had role or qualification mismatches ({count})."
    elif already_seen:
        status = f"No new suitable vacancies; previously sent jobs were excluded ({count})."
    if any(warning and warning.startswith("Matching evidence unavailable:") for warning in warnings):
        status = "Matching evidence unavailable; check your resume and profile."
    elif any(warning and warning.startswith("Fit ranking unavailable;") for warning in warnings):
        status = "Job matching temporarily unavailable; retrying next run."
    lines = [lines[0], status]
    if source_failures or paused_feeds or boards_skipped:
        lines.append("Search coverage incomplete: some sources failed, were quota-paused, or could not be read.")
        lines.append(f"Boards: {boards_read}/{boards_total} read successfully; "
                     f"{BOARD_COVERAGE.get('throttled', 0)} deferred after rate limiting. "
                     f"Failed sources: {', '.join(sorted(source_failures)) or 'none'}. "
                     f"Quota-paused feeds: {paused_names or 'none'}.")
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
    limit = 1900  # Discord hard limit is 2000; reserve room for newlines.
    for line in lines:
        for offset in range(0, max(len(line), 1), limit - 1):
            part = line[offset:offset + limit - 1]
            if len(cur) + len(part) + 1 > limit:
                chunks.append(cur)
                cur = ""
            cur += part + "\n"
    if cur:
        chunks.append(cur)
    for ch in chunks:
        r = requests.post(webhook, json={"content": ch}, timeout=30)
        if r.status_code not in (200, 204):
            log(f"ERROR Discord HTTP {r.status_code}: {r.text[:200]}")
            sys.exit(1)  # fail loud
        time.sleep(1)


def main():
    global PROFILE, NETWORK_DEADLINE
    SOURCE_FAILURES.clear()
    THROTTLED_SOURCES.clear()
    BOARD_COVERAGE.clear()
    BOARD_COVERAGE.update(attempted=0, successful=0, failed=0, throttled=0)
    PROFILE = load_profile()
    evidence = load_resume_evidence()
    evidence_ready = PROFILE is not DEFAULT_PROFILE and bool(evidence)
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
    # The network phase's deadline. Taken before the first fetch, and checked
    # only where the remaining work is optional -- never mid-delivery. See
    # RUN_BUDGET_SECONDS.
    run_deadline = time.monotonic() + RUN_BUDGET_SECONDS
    NETWORK_DEADLINE = run_deadline

    seen = load_seen()
    boards = load_boards()
    stats = load_stats()
    drift_warning = check_drift(stats)  # uses history only; today's row isn't recorded yet
    # Same rule: read only what previous runs committed, before today's archive
    # is written, so the warning describes history rather than this run.
    ranker_warning = instability_warning(score_instability(window_days=ARCHIVE_INSTABILITY_DAYS))
    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    use_rapidapi = rapidapi_enabled(os.environ.get("RAPIDAPI_ENABLED"))

    all_jobs = []

    # Breadth: JSearch aggregator (LinkedIn/Indeed/Glassdoor/company sites via Google for Jobs).
    # Paywalled postings are refused inside the fetch; the matching ones come
    # back here so their employers can still be probed for a board of their own.
    paywalled = []

    # Freshness: the two feeds that can actually answer "posted this week".
    # Measured 7 Sep 2026 against the same country on the same day, JSearch
    # returned ZERO rows inside a week and a corpus aged 18-159 days. The ATS
    # feed returned ten technical roles aged 0-1 days, all on employer boards;
    # the LinkedIn feed returned ten more, all posted that day, nine of them
    # technical. Their allowances are small, hard and metered per API, so each
    # carries its own counter in state rather than a shared assumption.
    quotas = load_api_quotas()
    paused_feeds = []
    attempted_sources = []
    if use_rapidapi:
        scheduled_feeds = fresh_api_feeds()
        attempted_sources.extend(("jsearch", *scheduled_feeds, "indeed"))
        all_jobs.extend(fetch_jsearch(rapidapi_key, paywalled,
                                      quotas.setdefault("jsearch", {}),
                                      paused_feeds=paused_feeds))
        fetchers = {"active_ats": fetch_active_ats,
                    "linkedin_api": fetch_linkedin,
                    "indeed": fetch_indeed}
        for feed in (*scheduled_feeds, "indeed"):
            quota = quotas.setdefault(feed, {})
            all_jobs.extend(fetchers[feed](rapidapi_key, quota, FALLBACK_MAX_JOB_AGE_DAYS,
                                           paused_feeds=paused_feeds))
        save_api_quotas(quotas)
    else:
        log("RapidAPI disabled for this manual run; using direct employer ATS feeds")

    # Depth: direct company ATS feeds (Greenhouse, Ashby, Workable, Lever).
    # COMPANIES is the hand-curated list; the boards after it were discovered
    # from employers the paid-relister cut discarded on an earlier run. Reading
    # them here is the entire payoff of that discovery: the posting arrives from
    # the employer with a free link and a complete JD, and
    # prefer_employer_postings drops the relisted copy without being told.
    today_iso = riyadh_today().isoformat()
    # Which discovered record a queue entry came from, so the loop can stamp
    # last_read without confirmed_boards having to widen the entry shape that
    # fetch_company takes.
    board_by_pair = {(r["source"], r["token"]): k for k, r in boards.items()
                     if r.get("source")}
    discovered_total = confirmed_boards(boards, limit=None)
    boards_total = len(COMPANIES) + len(discovered_total)
    board_queue = board_read_queue(boards)
    curated_pairs = {(c["source"], c["token"]) for c in COMPANIES}
    boards_read = 0
    read_boards = False
    for c in board_queue:
        # Rotate attempts so one failing account cannot monopolize a host.
        # Curated boards still get an attempt after the optional-read deadline.
        if (c["source"], c["token"]) not in curated_pairs and time.monotonic() >= run_deadline:
            continue
        if c["source"] in THROTTLED_SOURCES:
            BOARD_COVERAGE["throttled"] += 1
            continue
        BOARD_COVERAGE["attempted"] += 1
        attempted_sources.append(c["source"])
        all_jobs.extend(fetch_company(c))
        key = board_by_pair.get((c["source"], c["token"])) or f"{c['source']}:{c['token']}"
        record = boards.setdefault(key, {"company": c["name"], "source": c["source"], "token": c["token"],
                                         "curated": (c["source"], c["token"]) in curated_pairs})
        record["last_attempt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        read_boards = True
        if c["name"] in SOURCE_FAILURES:
            BOARD_COVERAGE["failed"] += 1
            continue
        boards_read += 1
        BOARD_COVERAGE["successful"] += 1
        record["last_read"] = today_iso
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
    # The paywalled postings never got here: fetch_jsearch refused them and
    # handed back the ones that matched the filters. This is the last line of
    # defence for a link that arrives paywalled from anywhere else, and it is
    # expected to be empty.
    still_paid = [j for j in matched if on_paid_relister(apply_link(j))]
    matched = [j for j in matched if not on_paid_relister(apply_link(j))]
    already_seen = sum(job_identity(job) in seen for job in matched)
    newly_found = len(matched) - already_seen
    for job in still_paid:
        log(f"Paid relister past the fetch on {link_host(job['url'])}: "
            f"{job['company']} — {job['title']}")

    # Their links are unreachable, but their employers are not: a board found
    # here is read directly from the next run on. Scoped to the postings that
    # passed the filters, so five ATS requests are never spent on a role this
    # candidate would not be shown.
    # Pure investment in tomorrow's run and therefore the first thing worth
    # dropping when the budget is gone: five ATS probes per seed employer buy
    # nothing for the digest going out this morning.
    discovered = False
    if time.monotonic() < run_deadline:
        discovered = discover_boards(
            board_discovery_seeds(paywalled, still_paid, matched),
            boards, today_iso)
    else:
        log("Run budget spent: skipping employer board discovery this run")
    # One write covering both the last_read stamps the company loop set and any
    # board discovery just added.
    if discovered or read_boards:
        save_boards(boards)

    # A paid-relister drop is deliberately NOT remembered in seen.json. Unlike
    # the delivery floor, this is a verdict on the link, not on the role: the
    # same posting appearing on the employer's own board tomorrow should be
    # delivered tomorrow.
    fresh = [j for j in matched
             if job_identity(j) not in seen
             and not still_cooling(seen, j, today_iso)]

    # Age before liveness, because this costs no network and settle_link spends
    # a request per job. Deliberately NOT written to seen.json: the check is
    # free to repeat, and a posting dropped for age today must still be
    # deliverable if a source later reports a corrected, newer date for it.
    fresh, age_rejected = split_by_age(fresh, FALLBACK_MAX_JOB_AGE_DAYS)
    unverified_dates = [j for j in age_rejected
                        if j.get("age_days") is None or j["age_days"] < 0]
    stale = [j for j in age_rejected if j not in unverified_dates]
    for job in stale:
        log(f"DROP older than {FALLBACK_MAX_JOB_AGE_DAYS}d ({job['age_days']}d): "
            f"{job['company']} — {job['title']}")
    undated = sum(1 for j in unverified_dates if j.get("age_days") is None)
    # Keep the pre-verification candidates: subsequent filters mutate fresh.
    # These counts mean unseen, non-cooling jobs inside the confirmed age gate,
    # before link checks and ranking.
    discovered_new = list(fresh)
    log(f"{len(all_jobs)} fetched, {len(matched)} match filter, "
        f"{len(relisted_copies)} relisted copies, {len(paywalled)} paywalled and refused "
        f"at the fetch, {len(stale)} older than {FALLBACK_MAX_JOB_AGE_DAYS}d, "
        f"{len(fresh)} new ({undated} undated)")

    # Verification is evidence, not a boolean gate: a third-party site may block
    # this runner while still serving a job seeker. Only confirmed closure drops.
    #
    # This is the pass the run budget most needs to guard. verify_link fetches a
    # third-party page once per job, and on a 24-new-job morning that is dozens
    # of round trips against sites that can hang. A job left unsettled keeps
    # LINK_UNKNOWN, which the card already renders as "link check inconclusive"
    # -- an honest degradation that exists rather than a new one.
    unsettled = 0
    for j in fresh:
        if time.monotonic() >= run_deadline:
            # Set explicitly. settle_link is the ONLY writer of link_status, and
            # the three readers below index it rather than .get() it, so leaving
            # the key absent would raise instead of degrading.
            j["link_status"] = LINK_UNKNOWN
            unsettled += 1
            continue
        settle_link(j)
    if unsettled:
        log(f"Run budget spent: {unsettled} link check(s) skipped, "
            "those jobs carry an inconclusive link")
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

    today = today_iso

    warnings = [drift_warning, ranker_warning]
    if not evidence_ready:
        warnings.append("Matching evidence unavailable: valid profile and master resume required; no job cards sent.")

    if not fresh:
        save_stats(
            stats, today, len(all_jobs), len(matched), 0, None,
            link_closed=len(closed), link_unknown=unknown_count,
            dropped_stale=len(stale), undated=undated,
            unverified_date=len(unverified_dates),
            strong=0, review=0, tailored=0, tailor_failed=0,
            delivered=0, source_yield=source_yield(all_jobs, discovered_new, paused_feeds=paused_feeds, sources=attempted_sources),
        )
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings,
                       already_seen=already_seen,
                       newly_found=newly_found, stale=len(stale),
                       unverified_date=len(unverified_dates),
                       boards_read=boards_read, boards_total=boards_total,
                       boards_skipped=boards_total - boards_read,
                       paused_feeds=paused_feeds, source_failures=SOURCE_FAILURES)
        log("No new matching jobs. Heartbeat sent.")
        return

    # A ranking built on a thin description is a worse outcome than a late one,
    # so enrichment is the last thing dropped and it degrades to whatever JD text
    # the fetchers already carried. jd_grade then labels those honestly as a
    # teaser or fragment; the final gate withholds those for a later retry.
    if time.monotonic() < run_deadline:
        enrich_descriptions(fresh)
    else:
        log(f"Run budget spent: {len(fresh)} job(s) scored on the description "
            "their source supplied, without enrichment")

    for job in fresh:
        if posting_location_conflict(job):
            log(f"DROP conflicting posting country {job['posting_countries']!r}: {job['company']} — {job['title']}")
    fresh = [job for job in fresh if not posting_location_conflict(job)
             and "url:" + canonical_url(apply_link(job)) not in seen]

    # A rescued apply link is BUILT from a token and a posting id, not copied
    # from a page, so it has never been fetched at the point enrichment records
    # it. Verify before handing it over. Only a link enrichment just introduced
    # is checked here: settle_link already looked at the one the job carried
    # into the pass above, and re-checking it would spend a request per job to
    # learn what `link_checked` already records.
    for job in fresh:
        if not job.get("apply_url") or job["apply_url"] == job.get("link_checked"):
            continue
        if time.monotonic() >= run_deadline:
            # Same rule as the first pass: an unchecked link is inconclusive,
            # never assumed good. The card says so.
            job["link_status"] = LINK_UNKNOWN
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

    # The cooldown, checked a second time now that every job has its text. The
    # pass above the fetch could only compare identities, and the identity of a
    # relisted aggregator posting is new every day -- which is the whole reason
    # description_key exists. Without this the description half of a floor: key
    # would never match anything.
    # One pass, and no `job not in cooling` membership test: that is a deep dict
    # comparison, which plans/README.md:296 already flagged once in split_tiers.
    still, cooling = [], []
    for job in fresh:
        (cooling if still_cooling(seen, job, today_iso) else still).append(job)
    fresh = still
    for job in cooling:
        log(f"Cooling {job['company']} — {job['title']}: scored below the floor "
            f"within the last {FLOOR_COOLDOWN_DAYS} days")
    if not fresh:
        save_stats(
            stats, today, len(all_jobs), len(matched), 0, None,
            link_closed=len(closed), link_unknown=unknown_count,
            dropped_stale=len(stale), undated=undated,
            unverified_date=len(unverified_dates),
            strong=0, competitive=0, review=0, tailored=0, tailor_failed=0,
            delivered=0, source_yield=source_yield(all_jobs, discovered_new, paused_feeds=paused_feeds, sources=attempted_sources),
            relisted=len(relisted), cooling=len(cooling),
        )
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings,
                       newly_found=newly_found, already_seen=already_seen,
                       stale=len(stale),
                       unverified_date=len(unverified_dates),
                       boards_read=boards_read, boards_total=boards_total,
                       boards_skipped=boards_total - boards_read,
                       paused_feeds=paused_feeds, source_failures=SOURCE_FAILURES)
        # Cooling is named here because it is the one reason on this path that a
        # reader can act on: those postings are coming back, and a morning where
        # every candidate was one would otherwise read as a dead feed.
        log(f"Nothing left to deliver: {len(relisted)} relists, {len(cooling)} "
            f"still cooling below the floor, {len(lost_late_rescue)} lost the "
            "rescued link enrichment found. Heartbeat sent.")
        return

    # The same resume evidence drives ranking and the requirements check.
    ranking = rank_jobs(fresh, rank_key, evidence) if rank_key and evidence_ready else None
    if ranking is None:
        warnings.append("Fit ranking unavailable; unverified matches are held for retry.")
    apply_ranking(fresh, ranking)
    # Keep the pre-filter average: selective delivery must not hide source drift.
    new_count = len(fresh)
    avg_score = round(sum(j["score"] for j in fresh) / new_count, 1)

    candidates, unverified = [], []
    for job in fresh:
        supported = (evidence_ready and job.get("ranking_verified")
                     and jd_grade(job.get("description") or "") == GRADE_FULL)
        (candidates if supported else unverified).append(job)
    # Scores below 40 are the rubric's wrong-function band. Check other scores
    # before selecting strong matches or the closest viable fallback.
    fresh = [job for job in candidates if job["score"] >= 40]
    below_floor = [job for job in candidates if job["score"] < 40]
    check_jobs(fresh, evidence, rank_key, RANK_MODEL, RANK_FALLBACKS,
               RANK_PROVIDER, log, key=delivery_priority)
    for job in cap_blocked_scores(fresh):
        gaps = "; ".join(b["title"] for b in job["fit_check"]["blockers"])
        log(f"Capped {job['company']} - {job['title']} at {job['score']}: {gaps}")
    strong_candidates, viable = [], []
    for job in fresh:
        if is_strong(job):
            strong_candidates.append(job)
        elif is_viable(job):
            viable.append(job)
        elif job["score"] < 40 or any(
                b.get("type") in ("location_work_authorization", "role_scope_mismatch")
                for b in (job.get("fit_check") or {}).get("blockers") or []):
            below_floor.append(job)
        else:
            unverified.append(job)
    for job in strong_candidates + viable:
        job["application_state"] = "apply_first" if is_strong(job) else "worth_reviewing"
    pending = []
    for job in unverified:
        if is_pending(job) and not pending_was_delivered(seen, job):
            job.update(application_state="needs_checking", pending_reasons=pending_reasons(job))
            pending.append(job)
    fresh = strong_candidates + viable + pending
    unique = {}
    for job in sorted(fresh, key=delivery_priority):
        unique.setdefault(canonical_url(apply_link(job)), job)
    fresh = list(unique.values())
    for job in below_floor:
        check = job.get("fit_check") or {}
        log(f"Rejected {job['company']} - {job['title']}: final fit {job['score']}; "
            f"track={job.get('track')!r}, "
            f"blockers={[(b.get('type'), b.get('title')) for b in check.get('blockers') or []]!r}, "
            f"resume strengths={len(check.get('strengths') or [])}, "
            f"url={apply_link(job)!r}; "
            f"skipped for {FLOOR_COOLDOWN_DAYS} days")
        defer_below_floor(seen, job, today)
    for job in unverified:
        check = job.get("fit_check") or {}
        log(f"Held for fit verification: {job['company']} - {job['title']}; "
            f"score={job['score']}, ranked={job.get('ranking_verified', False)}, "
            f"JD={jd_grade(job.get('description') or '')}, "
            f"JD_chars={len(job.get('description') or '')}, "
            f"JD_issues={jd_completeness_failures(job.get('description') or '')!r}, "
            f"url={apply_link(job)!r}, "
            f"track={job.get('track')!r}, ceiling={check.get('ceiling', 'unknown')}, "
            f"note={check.get('note', '')!r}, unclear={check.get('unclear', [])!r}, "
            f"resume strengths={len(check.get('strengths') or [])}")

    if not fresh:
        save_seen(seen)
        save_stats(
            stats, today, len(all_jobs), len(matched), new_count, avg_score,
            link_closed=len(closed), link_unknown=unknown_count,
            dropped_stale=len(stale), undated=undated,
            unverified_date=len(unverified_dates), unverified_fit=len(unverified),
            strong=0, competitive=0, review=0, tailored=0, tailor_failed=0,
            delivered=0, source_yield=source_yield(all_jobs, discovered_new,
                                                paused_feeds=paused_feeds, sources=attempted_sources),
            relisted=len(relisted), below_floor=len(below_floor), cooling=len(cooling),
        )
        post_heartbeat(webhook, worker_url, worker_secret, today,
                       len(all_jobs), len(matched), warnings,
                       newly_found=newly_found, already_seen=already_seen,
                       stale=len(stale), below_floor=len(below_floor),
                       unverified_date=len(unverified_dates), unverified_fit=len(unverified),
                       boards_read=boards_read, boards_total=boards_total,
                       boards_skipped=boards_total - boards_read, paused_feeds=paused_feeds,
                       incomplete_descriptions=sum(jd_grade(j.get("description") or "") != GRADE_FULL
                                                   for j in unverified),
                       source_failures=SOURCE_FAILURES)
        log("No verified strong matches or review candidates. Heartbeat sent.")
        return

    # Only eligible jobs consume employer slots or the digest's overall limit.
    fresh, deferred = cap_per_company(fresh)
    for job in deferred:
        log(f"Deferred {job['company']} - {job['title']}: fit {job['score']}, "
            f"more than {MAX_JOBS_PER_COMPANY} from one employer today")

    fresh.sort(key=delivery_priority)
    limit = MAX_JOBS_IN_DIGEST
    deferred.extend(fresh[limit:])
    fresh = fresh[:limit]
    for job in fresh:
        if job.get("application_state") == "needs_checking":
            job["tier"] = job["application_action"] = "Needs checking"
        else:
            job["tier"] = TIER_STRONG if is_strong(job) else TIER_CLOSEST
            job["application_action"] = "Apply first" if is_strong(job) else "Worth reviewing"
    strong = [j for j in fresh if is_strong(j)]
    competitive = [j for j in fresh if j["application_state"] == "worth_reviewing" and j["score"] >= COMPETITIVE_MIN_SCORE]
    review = [j for j in fresh if j["application_state"] == "worth_reviewing" and j["score"] < COMPETITIVE_MIN_SCORE]

    # Persist full job/JD evidence before delivery. A later button click can
    # therefore tailor the actual captured posting rather than its title.
    snapshots = [make_snapshot(job, f"{today}T00:00:00Z") for job in fresh]
    write_snapshots(fresh, today)

    lines = [f"**Job digest — {today}**  ({len(fresh)} new in Saudi Arabia)"]
    lines.append(f"Shortlist: {len(fresh)} of {MAX_JOBS_IN_DIGEST} roles available for review today.")
    lines.append("Apply first: verified strong matches. Worth reviewing: verified options with gaps. Needs checking: fit pending.")
    lines.append(f"Available: {len(strong)} Apply first; {len(competitive) + len(review)} Worth reviewing; "
                 f"{sum(j['application_state'] == 'needs_checking' for j in fresh)} Needs checking.")
    shortlist_summary = {"key": f"shortlist:{today}:{len(fresh)}", "content": "\n".join(lines)}
    lines.extend(f"⚠️ {warning}" for warning in warnings if warning)
    last_tier = None
    for (tier, comp), jobs in groupby(fresh, key=lambda j: (j["tier"], j["company"])):
        if tier != last_tier:
            lines.append(f"\n**{tier}**")
            last_tier = tier
        lines.append(f"\n__**{comp}**__")
        for j in jobs:
            # Only Pinpoint publishes structured pay today; everything else omits it.
            pay = f" · {j['salary']}" if j.get("salary") else ""
            resume_line = ""
            # Age leads the flags. The digest is asked for roles posted within a
            # week, so how old a posting is belongs on the card next to the
            # claim. Only confirmed fresh publication dates reach delivery.
            flags = [age_label(j.get("age_days"))]
            if j.get("link_status") == LINK_UNKNOWN:
                flags.append("link check inconclusive")
            if not j.get("description"):
                flags.append("title-only assessment")
            check = j.get("fit_check") or {}
            pending = j["application_state"] == "needs_checking"
            if pending:
                flags.extend(j["pending_reasons"])
            gaps = "; ".join(b["title"] for b in check.get("blockers") or [])
            if gaps:
                # Named whatever the ceiling. A single verified gap used to show
                # nothing, on a card whose score had already been capped for it.
                flags.append(f"gaps the posting states - {gaps}")
            elif check.get("ceiling") == "unknown":
                # Now that every delivered job is checked, an unchecked one
                # would otherwise read exactly like one checked and found clean.
                flags.append("requirements not checked")
            if check.get("unclear"):
                questions = "; ".join(f"{item.get('topic', 'Fit question')}: {item.get('reason', '')}"
                                      for item in check["unclear"])
                flags.append(f"questions to check - {questions}")
            if flags:
                resume_line += f"\n  _{'; '.join(flags)}_"
            lines.append(
                f"- **{j['title']}** — {j['location']} · {j['application_action']} · "
                + ("Fit pending" if pending else f"fit {j['score']}") + f"{pay}\n"
                + ("  Potentially relevant role; application fit has not been verified." if pending else f"  {j['why']}")
                + f"\n  <{apply_link(j)}>{resume_line}"
            )
            if check.get("strengths"):
                for strength in check.get("strengths") or []:
                    quotes = [e["snippet"] for e in strength.get("evidence") or [] if e.get("source") == "resume"]
                    if quotes:
                        lines.append(f"  Background evidence: {strength['title']} — {quotes[0][:180]}")
                        break

    failed_card_keys = set()
    delivered_cards = {}
    def record_delivery_stats(delivered_ids):
        accepted = [j for j in fresh if job_key(j) in delivered_ids]
        def receipt(job):
            return delivered_cards.get(job_key(job), {"application_state": job["application_state"], "score": job["score"]})
        states = [receipt(j).get("application_state") for j in accepted]
        save_stats(
            stats, today, len(all_jobs), len(matched), new_count, avg_score,
            link_closed=len(closed), link_unknown=unknown_count,
                dropped_stale=len(stale), undated=undated,
            unverified_date=len(unverified_dates),
            strong=states.count("apply_first"),
            competitive=sum(receipt(j).get("application_state") == "worth_reviewing" and (receipt(j).get("score") or 0) >= COMPETITIVE_MIN_SCORE for j in accepted),
            review=sum(receipt(j).get("application_state") == "worth_reviewing" and (receipt(j).get("score") or 0) < COMPETITIVE_MIN_SCORE for j in accepted),
            tailored=0, tailor_failed=0,
            delivered=len(delivered_ids), relisted=len(relisted), cooling=len(cooling),
            fallback_delivered=states.count("worth_reviewing"),
            needs_checking_delivered=states.count("needs_checking"),
            delivery_unclassified=sum(state not in ("apply_first", "worth_reviewing", "needs_checking") for state in states),
            delivery_attempted=len(fresh), delivery_failed=len(failed_card_keys),
            delivery_unconfirmed=max(0, len(fresh) - len(delivered_ids) - len(failed_card_keys)),
            source_yield=source_yield(all_jobs, discovered_new,
                                      [j for j in fresh if job_key(j) in delivered_ids], paused_feeds, attempted_sources),
            below_floor=len(below_floor), deferred=len(deferred), unverified_fit=len(unverified),
        )

    interactive_delivery_claimed = False
    delivery_failed = False
    delivered_ids = set()
    if worker_url:
        try:
            delivery = post_interactive_cards(worker_url, worker_secret, snapshots, heartbeat=shortlist_summary)
            delivered_cards = delivery.get("delivered_cards") or {}
            # Worker summaries include heartbeat keys; only job cards count as
            # delivered or pending when deciding whether the digest failed.
            job_keys = {snapshot["job_key"] for snapshot in snapshots}
            accepted_keys = job_keys.intersection(delivery.get("posted", []) + delivery.get("existing", []))
            uncertain_keys = job_keys.intersection(delivery.get("in_progress", []) + delivery.get("retryable", []))
            failed = [item for item in delivery.get("failed", []) if item.get("job_key") in job_keys]
            failed_card_keys.update(item["job_key"] for item in failed)
            interactive_delivery_claimed = bool(accepted_keys or uncertain_keys or failed)
            delivered_ids = accepted_keys
            log(
                f"Interactive delivery accepted {len(accepted_keys)} of {len(snapshots)} cards "
                f"({len(failed)} failed)"
            )
            # Named whatever else happened. This used to sit inside the
            # total-failure branch below, so the common case -- one card lost
            # among successes, 7 of 8 on 3 Sep -- printed no job and no reason.
            # The job is correctly left out of seen.json and retried tomorrow,
            # but a retry that keeps failing was invisible.
            for item in failed[:5]:
                log(f"  failed card {item.get('job_key')}: {item.get('error')}")
            if len(failed) > 5:
                log(f"  ...and {len(failed) - 5} more failed cards")
            # Nothing reached Discord and at least one card definitively failed.
            # Falling back to the webhook would risk double-posting the jobs the
            # Worker may still hold, so the run stays single-channel and instead
            # fails loudly: seen.json is untouched, so tomorrow retries them.
            if not accepted_keys and not uncertain_keys and failed:
                delivery_failed = True
                log(f"ERROR no job card reached Discord; {len(failed)} of {len(snapshots)} cards failed")
        except Exception as error:
            log(f"WARN interactive Discord delivery failed: {error}; falling back to webhook")
    if not interactive_delivery_claimed:
        if not webhook:
            record_delivery_stats(set())
            log("FATAL interactive delivery failed and DISCORD_WEBHOOK_URL is not set")
            sys.exit(1)
        try:
            post_discord(webhook, lines)
        except (Exception, SystemExit):
            record_delivery_stats(set())
            raise
        # Reaching this line means Discord accepted every chunk: post_discord
        # exits non-zero on any status outside 200/204. The jobs are therefore
        # delivered, whichever channel carried them, and have to be remembered.
        # They were not, when a Worker was configured and its call raised: the
        # webhook posted the digest, seen.json stayed empty, and the identical
        # digest went out again the next morning -- re-fetching every
        # description and paying for a second ranking call -- for as long as
        # the Worker stayed unreachable.
        delivered_ids = {job_key(job) for job in fresh}

    for j in fresh:
        if job_key(j) in delivered_ids:
            remember(seen, j, today)
    save_seen(seen)
    record_delivery_stats(delivered_ids)
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


# --------------------------- ACTIVE ATS PROBE -------------------------------
# `python digest.py --probe-active-ats` reads one page of Active Jobs DB, an
# ATS-sourced feed on the same RapidAPI account, and reports what it holds for
# Saudi Arabia within a week. It exists because --probe-freshness established
# (7 Sep 2026, two unrelated queries) that JSearch returns ZERO jobs under
# date_posted=week and 18-159 day old postings under date_posted=month, so the
# aggregator this digest is built on cannot answer "posted within a week" at all.
#
# Whether a replacement is worth building depends entirely on its real Saudi
# yield, which no amount of reading the vendor's docs will tell us.
#
# It reads. It never writes, never delivers and never touches seen.json.
# Strong matches stay within seven days; the fallback can use up to 30.
# first_published was written by
# every fetcher and read by NOTHING before this, so "fresh" meant only "not
# delivered before". The configured window can tighten the seven-day policy;
# no source's retrieval or update clock can establish a publication date.
def _max_job_age(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 7
    return value if 0 < value <= 7 else 7


MAX_JOB_AGE_DAYS = _max_job_age(os.environ.get("MAX_JOB_AGE_DAYS"))
FALLBACK_MAX_JOB_AGE_DAYS = 30

def publication_day(raw):
    """Validate publication dates and convert timestamp dates to Riyadh."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        if len(raw) == 10:
            return datetime.date.fromisoformat(raw)
        stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(datetime.timezone(datetime.timedelta(hours=3)))
        return stamp.date()
    except ValueError:
        return None


def split_by_age(jobs, max_age_days, today=None):
    """Split postings into (fresh, stale) and record each one's age in days.

    Every source uses the same freshness window. An open employer listing can
    remain published long after it first appeared, but that does not make it a
    newly posted role for this digest.

    Missing, invalid and future publication dates are excluded. Retrieval and
    update dates never substitute for an employer publication date.
    """
    today = today or riyadh_today()
    fresh, stale = [], []
    for job in jobs:
        published = publication_day(job.get("first_published"))
        if published is None:
            job["age_days"] = None
            stale.append(job)
            continue
        age = (today - published).days
        job["age_days"] = age
        too_old = age < 0 or age > max_age_days
        (stale if too_old else fresh).append(job)
    return fresh, stale


def age_label(age_days):
    """How the card states a posting's age, including when it has none."""
    if age_days is None:
        return "posting date unknown"
    if age_days == 0:
        return "posted today"
    if age_days == 1:
        return "posted yesterday"
    return f"posted {age_days} days ago"


ACTIVE_ATS_HOST = "active-jobs-db.p.rapidapi.com"

# The ATS platforms worth asking for: every source this digest already fetches
# directly, plus the large enterprise systems no public board API covers.
ACTIVE_ATS_SOURCES = ("greenhouse,ashby,lever.co,workable,pinpoint,"
                      "workday,smartrecruiters,icims,oraclecloud,successfactors")


# Target roles as a Boolean the API can evaluate, including commercial and
# sales aliases from the profile. Avoid spending the limited page on unrelated
# engineering disciplines.
# Deliberately NOT the bare word "engineer". TITLE_INCLUDE can afford it because
# LOCATION_INCLUDE, TITLE_EXCLUDE and the ranker all sit downstream of it for
# free. Here every row costs allowance: the first filtered probe (7 Sep 2026)
# spent 8 of 10 rows on civil, mechanical, electrical, irrigation and
# arboriculture engineers. Name the software disciplines instead.
ACTIVE_ATS_TITLE = (
    "software | developer | backend | 'back end' | frontend | 'front end' | "
    "'full stack' | fullstack | 'ai engineer' | 'llm' | 'ai product engineer' | "
    "'data analyst' | analytics | 'business intelligence' | 'power bi' | "
    "'solutions engineer' | 'ai solutions architect' | 'automation engineer'"
    + "".join(" | '" + re.sub(r"[^a-z0-9 ]", " ", title.lower()) + "'"
                            for title in PROFILE.get("title_priorities", {}))
)


# LinkedIn as its own feed rather than relayed through Google for Jobs. Same
# vendor and the same parameter model as Active Jobs DB, on a SEPARATE free
# allowance -- RapidAPI meters per API, so subscribing to both doubles what the
# free tier supplies. There is no source= here: every row is LinkedIn.
#
# LinkedIn is not the problem the paid-relister cut was solving. digest.py:319
# records it as the largest single source of real Saudi postings this digest
# sees, 65 of 178. What failed on 7 Sep 2026 was STALE, DEAD LinkedIn relayed
# through an index that could not filter by date -- and prefer_employer_postings
# already demotes a LinkedIn row whenever the employer's own board has the role.
LINKEDIN_API_HOST = "linkedin-job-search-api.p.rapidapi.com"

# Bayt is the largest Saudi-native board and this digest has barely seen it:
# bayt.com appeared ONCE across 196 archived postings, because Google for Jobs
# indexes it thinly for Saudi queries. A different vendor again -- not
# Fantastic.jobs -- so a different response shape and its own probe.
#
# Its free allowance is 25 requests a DAY rather than a month, which is 30x
# either Fantastic.jobs feed, and days_old takes an exact integer against the
# posting timestamp rather than a bucketed window.
#
# Not Bayt's own API: a third party scrapes Bayt and resells it here. That is
# the same legal basis as the Apify scrapers, with a storefront on top.
BAYT_HOST = "bayt-com.p.rapidapi.com"


# Indeed, which nothing else in this stack reaches. A different vendor again,
# with a separate endpoint per platform rather than one blended index -- so its
# datePosted is not the same machinery as JSearch's, even though the parameter
# shares a name. That name is a warning, not a promise: date_posted=week is
# exactly what returned zero rows on JSearch, so this gets measured before it
# gets a fetcher.
def probe_indeed(api_key, query="software engineer", location="Saudi Arabia", quota=None):
    """Report what the Indeed feed holds for Saudi Arabia inside a week."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    quota = quota if quota is not None else {}
    url = ("https://" + INDEED_HOST + "/v2/indeed/search"
           + "?query=" + requests.utils.quote(query)
           + "&location=" + requests.utils.quote(location)
           + "&countryCode=sa&remoteOnly=false")
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": INDEED_HOST}, timeout=30)
    except requests.RequestException as e:
        print(f"request failed: {e}")
        return 1
    recorded = _record_quota(quota, r.headers)
    _debit_unreported_quota(quota, recorded)
    print(f"HTTP {r.status_code} for query={query!r}, location={location!r}")
    response_quota = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"quota headers: {response_quota or '(none reported)'}")
    if r.status_code != 200:
        # It names the parameter it wanted and warns about ones it did not
        # recognise, which is cheaper to read than guessing across requests.
        print(f"body[:900]: {r.text[:900]}")
        # A clear answer IS the probe working. 403 "not subscribed" and 400
        # "missing parameter" are findings to read, not workflow failures;
        # exiting non-zero on them turned every discovery run into a red X
        # next to the daily digest and buried whether anything real broke.
        return 0
    try:
        payload = r.json()
    except ValueError:
        print(f"non-JSON response: {r.text[:200]}")
        return 1
    rows = payload
    if isinstance(payload, dict):
        print(f"top-level keys: {sorted(payload)}")
        for note in (payload.get("warnings") or []):
            print(f"  WARNING from API: {note}")
        for key in ("jobs", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                print(f"rows live under {key!r}")
                rows = payload[key]
                break
        else:
            rows = []
    print(f"{len(rows)} rows returned")
    print()
    if not rows:
        return 0
    print("first row, verbatim:")
    for key in sorted(rows[0]):
        value = rows[0][key]
        if isinstance(value, str) and len(value) > 140:
            value = value[:140] + f"... ({len(rows[0][key])} chars)"
        print(f"  {key} = {value!r}")
    print()
    # Two clocks, and they disagree by months: datePublishedTimestamp is when the
    # employer published, dateOnIndeedTimestamp is when Indeed indexed it. Both
    # are epoch MILLISECONDS. Report the employer's clock, because that is the
    # one the digest promises against -- an index date says only that a scraper
    # noticed an old posting today.
    today = datetime.date.today()

    def _age(row, field):
        stamp = row.get(field)
        if not isinstance(stamp, (int, float)):
            return None
        try:
            when = datetime.datetime.utcfromtimestamp(stamp / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
        return (today - when).days

    shaped = []
    for row in rows:
        company = row.get("company")
        if isinstance(company, dict):
            company = company.get("name")
        where = row.get("location")
        if isinstance(where, dict):
            where = ", ".join(str(v) for v in
                              (where.get("location"), where.get("country")) if v)
        shaped.append({
            "company": str(company or ""),
            "title": str(row.get("title") or "").strip(),
            "location": str(where or ""),
            "url": row.get("applyUrl") or "",
            "source": "indeed", "token": "",
            "description": str(row.get("description") or ""),
            "published_age": _age(row, "datePublishedTimestamp"),
            "indexed_age": _age(row, "dateOnIndeedTimestamp"),
        })
    kept = [j for j in shaped if matches_filters(j)]
    ages = sorted(j["published_age"] for j in kept
                  if isinstance(j["published_age"], int))
    within = sum(1 for a in ages if a <= 7)
    print(f"{len(kept)} of {len(shaped)} rows pass this digest's own filters")
    if ages:
        print(f"published age of those: min {ages[0]}, median "
              f"{ages[len(ages) // 2]}, max {ages[-1]}; within 7 days: "
              f"{within}/{len(ages)}")
    print()
    for job in kept[:25]:
        pub = job["published_age"]
        idx = job["indexed_age"]
        pub = f"{pub}d" if isinstance(pub, int) else "?"
        idx = f"{idx}d" if isinstance(idx, int) else "?"
        print(f"- [pub {pub:>5} / seen {idx:>4}] {job['company'][:22]:22} "
              f"{job['title'][:44]}")
    return 0


def probe_bayt(api_key, days_old="7", scan_pages="10"):
    """Report what Bayt holds for Saudi Arabia inside a posting-age cutoff.

    days_old and keyword are POST-filters on this API: they scan scan_pages
    pages of 30 rather than the whole index, so the request narrows on country
    first and the response reports how much was actually scanned.
    """
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    url = (f"https://{BAYT_HOST}/job-search"
           f"?country=sa&days_old={days_old}&scan_pages={scan_pages}"
           f"&page=1&language=en")
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": BAYT_HOST}, timeout=30)
    except requests.RequestException as e:
        print(f"request failed: {e}")
        return 1
    print(f"HTTP {r.status_code} for country=sa, days_old={days_old}")
    quota = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"quota headers: {quota or '(none reported)'}")
    if r.status_code != 200:
        # 403 means the key is not subscribed; subscribing is the operator's call.
        print(f"body[:300]: {r.text[:300]}")
        # A clear answer IS the probe working. 403 "not subscribed" and 400
        # "missing parameter" are findings to read, not workflow failures;
        # exiting non-zero on them turned every discovery run into a red X
        # next to the daily digest and buried whether anything real broke.
        return 0
    try:
        payload = r.json()
    except ValueError:
        print(f"non-JSON response: {r.text[:200]}")
        return 1
    if isinstance(payload, dict):
        print(f"top-level keys: {sorted(payload)}")
        # How much of the index the post-filter actually looked at. Without this
        # a small result is ambiguous: few fresh Saudi jobs, or a short scan.
        if payload.get("postFilter"):
            print(f"postFilter: {payload['postFilter']}")
        if payload.get("count") is not None:
            print(f"count (pre-post-filter): {payload['count']}")
        rows = (payload.get("jobs") or payload.get("results")
                or payload.get("data") or [])
    else:
        rows = payload
    # The envelope nests: data is itself an object holding the rows beside its
    # own metadata. Walk one level rather than assume, and say what was found.
    if isinstance(rows, dict):
        print(f"data keys: {sorted(rows)}")
        for key, value in rows.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                print(f"rows live under data[{key!r}]")
                rows = value
                break
        else:
            print("no list of objects under data; nothing to shape")
            return 0
    print(f"{len(rows)} rows returned")
    print()
    if not rows:
        return 0
    # Verbatim, because the field names decide what a fetcher would read and
    # guessing them wrong fails silently -- the lesson from description_text.
    print("first row, verbatim:")
    for key in sorted(rows[0]):
        value = rows[0][key]
        if isinstance(value, str) and len(value) > 140:
            value = value[:140] + f"... ({len(rows[0][key])} chars)"
        print(f"  {key} = {value!r}")
    print()
    # Volume is not the question -- relevance is. Bayt carries the whole Saudi
    # market, most of it retail, construction and admin, so what matters is how
    # many rows survive the filters this digest already applies. Shaped the way
    # a fetcher would shape them, then judged by matches_filters itself.
    shaped = []
    for row in rows:
        company = row.get("company")
        if isinstance(company, dict):
            company = company.get("name") or ""
        shaped.append({
            "company": str(company or ""),
            "title": str(row.get("title") or "").strip(),
            "location": str(row.get("locationFormatted")
                            or row.get("location") or ""),
            "url": row.get("url") or "",
            "first_published": str(row.get("postedAt") or "")[:10],
            "source": "bayt", "token": "",
            "description": str(row.get("summaryEn")
                               or row.get("descriptionSnippet") or ""),
            "days_old": row.get("daysOld"),
            "aggregated": row.get("isAggregated"),
        })
    kept = [j for j in shaped if matches_filters(j)]
    native = sum(1 for j in kept if j.get("aggregated") is False)
    print(f"{len(kept)} of {len(shaped)} rows pass this digest's own filters "
          f"({native} of those posted natively on Bayt)")
    print()
    for job in kept[:30]:
        age = job.get("days_old")
        age = f"{age}d" if isinstance(age, int) else "?"
        tag = "native" if job.get("aggregated") is False else "aggr"
        print(f"- [{age:>4} {tag:6}] {job['company'][:26]:26} {job['title'][:46]}")
    return 0


def probe_linkedin(api_key, time_frame="7d", limit=10, quota=None):
    """Report what the LinkedIn feed holds for Saudi Arabia inside one window."""
    url = (f"https://{LINKEDIN_API_HOST}/active-jb"
           f"?location={requests.utils.quote('Saudi Arabia')}"
           f"&title_advanced={requests.utils.quote(ACTIVE_ATS_TITLE)}"
           f"&description_format=text"
           f"&time_frame={time_frame}&limit={limit}")
    return _probe_fantastic("LinkedIn Job Search", url, api_key, time_frame, limit, quota)


def probe_active_ats(api_key, time_frame="7d", limit=10, quota=None):
    """Report what Active Jobs DB holds for Saudi Arabia inside one time window."""
    url = (f"https://{ACTIVE_ATS_HOST}/active-ats"
           f"?source={requests.utils.quote(ACTIVE_ATS_SOURCES)}"
           f"&location={requests.utils.quote('Saudi Arabia')}"
           f"&title_advanced={requests.utils.quote(ACTIVE_ATS_TITLE)}"
           f"&description_format=text"
           f"&time_frame={time_frame}&limit={limit}")
    return _probe_fantastic("Active Jobs DB", url, api_key, time_frame, limit, quota)


def _probe_fantastic(name, url, api_key, time_frame, limit, quota=None):
    """One page from a Fantastic.jobs feed, reported by age and by employer.

    Both feeds answer the only question that matters here -- how old is what
    you actually get back -- and both meter jobs as well as requests, so the
    probe reads one small page and prints one row verbatim.
    """
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    quota = quota if quota is not None else {}
    print(f"--- {name} ---")
    host = url.split("//", 1)[1].split("/", 1)[0]
    try:
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": host}, timeout=30)
    except requests.RequestException as e:
        print(f"request failed: {e}")
        return 1
    recorded = _record_quota(quota, r.headers)
    _debit_unreported_quota(quota, recorded, job_cost=limit)
    print(f"HTTP {r.status_code} for time_frame={time_frame}, limit={limit}")
    response_quota = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"quota headers: {response_quota or '(none reported)'}")
    if r.status_code != 200:
        # A 403 here means the key is not subscribed to this API. Subscribing is
        # an account action for the operator, not something this probe can do.
        print(f"body[:300]: {r.text[:300]}")
        # A clear answer IS the probe working. 403 "not subscribed" and 400
        # "missing parameter" are findings to read, not workflow failures;
        # exiting non-zero on them turned every discovery run into a red X
        # next to the daily digest and buried whether anything real broke.
        return 0
    try:
        jobs = r.json()
    except ValueError:
        print(f"non-JSON response: {r.text[:200]}")
        return 1
    if isinstance(jobs, dict):
        print(f"top-level keys: {sorted(jobs)}")
        jobs = jobs.get("data") or jobs.get("jobs") or []
    print(f"{len(jobs)} jobs returned")
    print()
    if not jobs:
        return 0
    # One row verbatim, values and all. The key list alone does not say whether
    # description_format=text lands in "description" or "description_text",
    # whether locations_derived holds strings or dicts, or what shape date_posted
    # actually takes -- and fetch_active_ats guesses all three.
    sample = dict(jobs[0])
    for key in sorted(sample):
        value = sample[key]
        if isinstance(value, str) and len(value) > 160:
            value = value[:160] + f"... ({len(sample[key])} chars)"
        print(f"  {key} = {value!r}")
    print()
    today = datetime.date.today()
    for job in jobs:
        raw = str(job.get("date_posted") or job.get("date_created") or "")[:10]
        try:
            age = f"{(today - datetime.date.fromisoformat(raw)).days}d"
        except ValueError:
            age = "?"
        # locations_derived is what the fetcher turns into `location`, and so
        # what LOCATION_INCLUDE is judged against. Print it: three of ten rows
        # in the 7 Sep probe were Morocco despite location=Saudi Arabia.
        where = job.get("locations_derived") or []
        if not isinstance(where, list):
            where = [where]
        print(f"- [{age:>4}] {str(job.get('organization') or '?')[:24]:24} "
              f"{str(job.get('title') or '?')[:36]:36} "
              f"{link_host(str(job.get('url') or '')) or '(no url)':18} "
              f"{'; '.join(str(w) for w in where)[:40]}")
    return 0
# ----------------------------------------------------------------------------


# ---------------------------- FRESHNESS PROBE -------------------------------
# `python digest.py --probe-freshness "software engineer Saudi Arabia"` asks the
# SAME query once per date_posted value and reports the age spread of what comes
# back. It exists because the first --probe-jsearch run with age reporting (7 Sep
# 2026) returned 10/10 jobs dated 32-159 days old under date_posted=month: either
# the filter is ignored on /search-v2, or it means something other than posting
# age. A posting-age gate cannot be designed without knowing which.
#
# It reads. It never writes, never delivers and never touches seen.json.
def probe_freshness(query, api_key, quota=None):
    """Report the age spread JSearch returns for each date_posted value."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    quota = quota if quota is not None else {}
    today = datetime.date.today()
    for window in ("week", "month", "all"):
        url = (f"https://{JSEARCH_HOST}/search-v2"
               f"?query={requests.utils.quote(query)}&country=sa&date_posted={window}")
        r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                       "X-RapidAPI-Host": JSEARCH_HOST}, timeout=30)
        recorded = _record_quota(quota, r.headers)
        _debit_unreported_quota(quota, recorded)
        remaining = r.headers.get("x-ratelimit-requests-remaining", "?")
        if r.status_code != 200:
            print(f"date_posted={window}: HTTP {r.status_code} {r.text[:120]}")
            continue
        payload = r.json().get("data") or {}
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        ages = []
        undated = 0
        for job in jobs:
            raw = (job.get("job_posted_at_datetime_utc") or "")[:10]
            try:
                ages.append((today - datetime.date.fromisoformat(raw)).days)
            except ValueError:
                undated += 1
        ages.sort()
        spread = (f"min {ages[0]}, median {ages[len(ages) // 2]}, max {ages[-1]}"
                  if ages else "no parseable dates")
        within = sum(1 for a in ages if a <= 7)
        print(f"date_posted={window:6} -> {len(jobs)} jobs, {undated} undated, "
              f"age days: {spread}, within 7 days: {within}/{len(ages)} "
              f"[quota left {remaining}]")
        time.sleep(0.5)
    return 0
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
def probe_jsearch(query, api_key, quota=None):
    """Report which fields one live JSearch job object carries, and what they link to."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    quota = quota if quota is not None else {}
    url = (f"https://{JSEARCH_HOST}/search-v2"
           f"?query={requests.utils.quote(query)}&country=sa&date_posted=month")
    r = requests.get(url, headers={"X-RapidAPI-Key": api_key,
                                   "X-RapidAPI-Host": JSEARCH_HOST}, timeout=30)
    recorded = _record_quota(quota, r.headers)
    _debit_unreported_quota(quota, recorded)
    print(f"HTTP {r.status_code} for query {query!r}")
    # RapidAPI reports the subscription's monthly allowance on every response;
    # the persisted value is what keeps later scheduled runs and probes safe.
    response_quota = {k.lower(): v for k, v in r.headers.items() if "ratelimit" in k.lower()}
    print(f"quota headers: {response_quota or '(none reported)'}")
    if r.status_code != 200:
        print(f"body[:200]: {r.text[:200]}")
        # A clear answer IS the probe working. 403 "not subscribed" and 400
        # "missing parameter" are findings to read, not workflow failures;
        # exiting non-zero on them turned every discovery run into a red X
        # next to the daily digest and buried whether anything real broke.
        return 0
    payload = r.json().get("data") or {}
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    print(f"top-level data keys: {sorted(payload) if isinstance(payload, dict) else 'list'}")
    print(f"{len(jobs)} jobs returned\n")

    # Page two is deliberately disabled: a diagnostic must not exceed its
    # declared one-request cost while JSearch is near its monthly reserve.
    print("page one only; cursor pages are disabled to protect the monthly budget")
    print()
    if not jobs:
        return 0

    print("job object keys (first job):")
    for key in sorted(jobs[0]):
        print(f"  {key}")
    print()
    # A posting-age gate is only as good as the date it reads. fetch_jsearch maps
    # job_posted_at_datetime_utc into first_published, and the key is listed in the
    # docstring -- but a key being PRESENT is not the same as it being POPULATED,
    # and make_snapshot drops first_published, so no archived run can answer this
    # after the fact. Report the real coverage and the actual ages: a gate over a
    # mostly-empty field would flag nearly every card "date unknown" and still ship
    # month-old roles.
    dated = [j for j in jobs if (j.get("job_posted_at_datetime_utc") or "").strip()]
    print(f"job_posted_at_datetime_utc populated: {len(dated)}/{len(jobs)} jobs")
    if dated:
        today = datetime.date.today()
        ages = []
        for job in dated:
            raw = (job.get("job_posted_at_datetime_utc") or "")[:10]
            try:
                ages.append((today - datetime.date.fromisoformat(raw)).days)
            except ValueError:
                print(f"  UNPARSEABLE date: {raw!r}")
        if ages:
            ages.sort()
            within = sum(1 for a in ages if a <= 7)
            print(f"  age in days: min {ages[0]}, median {ages[len(ages) // 2]}, "
                  f"max {ages[-1]}")
            print(f"  within 7 days: {within}/{len(ages)}")
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
        # What matches_filters actually reads. A remote US role returned under
        # country=sa can arrive with an empty city and the country expanded to
        # "Saudi Arabia", which passes LOCATION_INCLUDE on the country alone.
        print(f"    city/state/country: {job.get('job_city')!r} / "
              f"{job.get('job_state')!r} / {job.get('job_country')!r}"
              f"   is_remote={job.get('job_is_remote')!r}")
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
def probe_queries(queries, api_key, quota=None):
    """Report what each query brings in that the others do not."""
    if not api_key:
        print("RAPIDAPI_KEY is not set; nothing to probe.")
        return 1
    quota = quota if quota is not None else {}
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    pages = {}
    remaining = "?"
    for query in queries:
        url = (f"https://{JSEARCH_HOST}/search-v2"
               f"?query={requests.utils.quote(query)}&country=sa&date_posted=month")
        pages[query] = []
        # JSearch times out intermittently -- it can drop an individual query
        # on the 19:01 digest run the same day. fetch_jsearch has always caught
        # that per query; this has to as well, or one slow response throws away
        # every request the probe already paid for.
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            print(f"{query!r}: request failed: {e}")
            continue
        recorded = _record_quota(quota, r.headers)
        _debit_unreported_quota(quota, recorded)
        remaining = r.headers.get("x-ratelimit-requests-remaining", remaining)
        if r.status_code != 200:
            print(f"{query!r}: HTTP {r.status_code} — skipped")
            continue
        try:
            payload = r.json().get("data") or {}
        except ValueError:
            print(f"{query!r}: non-JSON response")
            continue
        pages[query] = payload.get("jobs", []) if isinstance(payload, dict) else payload
        print(f"{query!r}: {len(pages[query])} returned")
        time.sleep(0.5)
    print(f"quota after probe: {remaining} remaining")
    print()

    def as_job(j):
        city = (j.get("job_city") or "").strip()
        state = (j.get("job_state") or "").strip()
        country = COUNTRY_CODE_TO_NAME.get((j.get("job_country") or "").strip().upper(),
                                           (j.get("job_country") or "").strip())
        return {"title": (j.get("job_title") or "").strip(),
                "location": ", ".join(p for p in (city, state, country) if p)}

    # Keyed on the canonical URL, NOT job_id: JSearch mints a fresh opaque id for
    # the same posting on every request, which is why job_identity keys this
    # source on its URL too. Counting ids reported every query as returning ten
    # postings no other query returned, including two that plainly shared them.
    def key(j):
        return canonical_url(j.get("job_apply_link") or "") or str(j.get("job_id") or "")

    owners = {}
    for query, jobs in pages.items():
        for j in jobs:
            owners.setdefault(key(j), set()).add(query)

    for query, jobs in pages.items():
        only = [j for j in jobs if len(owners[key(j)]) == 1]
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
        queries = [q.strip() for q in sys.argv[2:] if q.strip()]
        sys.exit(run_paid_probe("jsearch", len(queries), probe_queries, queries,
                                os.environ.get("RAPIDAPI_KEY")))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-indeed":
        sys.exit(run_paid_probe("indeed", 1, probe_indeed,
                                os.environ.get("RAPIDAPI_KEY"),
                                *(sys.argv[2:4] or [])))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-linkedin":
        sys.exit(run_paid_probe("linkedin_api", 1, probe_linkedin,
                                os.environ.get("RAPIDAPI_KEY"),
                                *(sys.argv[2:3] or []),
                                job_cost=ACTIVE_ATS_PAGE))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-active-ats":
        sys.exit(run_paid_probe("active_ats", 1, probe_active_ats,
                                os.environ.get("RAPIDAPI_KEY"),
                                *(sys.argv[2:3] or []),
                                job_cost=ACTIVE_ATS_PAGE))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-freshness":
        query = " ".join(sys.argv[2:]) or AGGREGATOR_QUERIES[0]
        sys.exit(run_paid_probe("jsearch", 3, probe_freshness, query,
                                os.environ.get("RAPIDAPI_KEY")))
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-jsearch":
        query = " ".join(sys.argv[2:]) or AGGREGATOR_QUERIES[0]
        sys.exit(run_paid_probe("jsearch", 1, probe_jsearch, query,
                                os.environ.get("RAPIDAPI_KEY")))
    else:
        main()
