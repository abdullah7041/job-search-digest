#!/usr/bin/env python3
"""
Daily job-search digest -> Discord.

Pattern (same as the AI-news digest):
  gather  -> company ATS APIs (Greenhouse, Ashby, Workable, Lever) for curated KSA/GCC firms
  filter  -> Saudi-only, senior+, title relevant, not seen before
  rank    -> one cheap Gemini call for score + one-line "why it fits"
             (deterministic keyword fallback if Gemini is missing or fails)
  deliver -> Discord webhook
Runs on GitHub Actions cron. No laptop needed.

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  DISCORD_WEBHOOK_URL   required
  GEMINI_API_KEY        optional. Without it, scoring is keyword-only.

State:
  seen.json holds job ids already sent. The workflow commits it back after each
  run, so every posting is delivered once and a missed day is caught next run.
  First run has an empty seen.json, so the first digest can be large (capped).
"""

import os
import sys
import json
import time
import datetime

import requests

# ------------------------------- CONFIG -------------------------------------
# Each company names its ATS in "source":
#   "greenhouse" + region "us"/"eu"   -> boards-api[.eu].greenhouse.io
#   "ashby"                           -> api.ashbyhq.com/posting-api
#   "workable"                        -> apply.workable.com/api/v3 (POST, paginated)
#   "lever"                           -> api.lever.co/v0/postings
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
]

# Saudi only. A job's location must contain one of these (case-insensitive).
LOCATION_INCLUDE = [
    "saudi", "ksa", "riyadh", "jeddah", "dammam", "khobar",
    "dhahran", "mecca", "makkah", "medina", "madinah",
]

# Title must match at least one of these to count as relevant.
TITLE_INCLUDE = [
    "engineer", "developer", "software", "backend", "back-end", "frontend",
    "front-end", "full stack", "fullstack", "full-stack", "ai ", " ai",
    "ml ", " ml", "machine learning", "data scien", "data engineer", "mlops",
    "devops", "sre", "site reliability", "platform", "automation", "architect",
    "product manager", "technical product", "head of engineering", "solutions engineer",
]

# Seniority filter: drop anything junior. senior+ only.
TITLE_EXCLUDE = [
    "junior", "intern", "internship", "graduate", "trainee", "tamheer",
    "associate", "entry", "fresh", "co-op", "apprentice", "student",
]

# Skill keywords from Abdullah's profile -> keyword match scoring.
SKILL_KEYWORDS = [
    "ai", "ml", "machine learning", "llm", "automation", "python", "backend",
    "api", "cloud", "aws", "gcp", "data", "saas", "platform", "devops",
    "founding", "applied ai", "architect", "solutions",
]

# Salary floor (SAR / month). SOFT signal only. Boards almost never list salary,
# so this NEVER rejects a posting. Benchmark 24 Jun 2026: senior eng Riyadh
# ~25k-35k SAR/mo, AI/ML 28k-35k.
SALARY_FLOOR_SAR_MONTH = 25000

# Gemini model. Confirm the exact id against current Google AI docs when building.
GEMINI_MODEL = "gemini-3.1-flash-lite"

MAX_JOBS_IN_DIGEST = 25
STATE_FILE = "seen.json"
STATE_TTL_DAYS = 120  # prune seen ids older than this to bound file size
USER_AGENT = "job-digest/1.0 (personal job search)"
# ----------------------------------------------------------------------------


def log(msg):
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds")
    print(f"[{ts}Z] {msg}", flush=True)


def _get(url):
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)


def _post(url, body):
    # Workable's public job-board API is POST-only and cursor-paginated.
    return requests.post(
        url,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )


def fetch_greenhouse(c):
    host = "boards-api.eu.greenhouse.io" if c.get("region") == "eu" else "boards-api.greenhouse.io"
    url = f"https://{host}/v1/boards/{c['token']}/jobs?content=false"
    try:
        r = _get(url)
    except requests.RequestException as e:
        log(f"WARN {c['name']}: request failed: {e}")
        return []
    if r.status_code != 200:
        log(f"WARN {c['name']}: greenhouse HTTP {r.status_code} (token '{c['token']}', {c.get('region')}). Skipping.")
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
        })
    log(f"{c['name']}: {len(out)} jobs (lever)")
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "lever": fetch_lever,
}


def fetch_company(c):
    fn = FETCHERS.get(c["source"])
    if not fn:
        log(f"WARN {c['name']}: unknown source '{c['source']}'. Skipping.")
        return []
    return fn(c)


def matches_filters(job):
    title = job["title"].lower()
    loc = job["location"].lower()
    if not any(k in loc for k in LOCATION_INCLUDE):
        return False
    if any(k in title for k in TITLE_EXCLUDE):
        return False
    if not any(k in title for k in TITLE_INCLUDE):
        return False
    return True


def keyword_score(job):
    title = job["title"].lower()
    hits = sum(1 for k in SKILL_KEYWORDS if k in title)
    return min(100, 40 + hits * 15)  # 40 base for passing filters, +15 per skill hit


def gemini_rank(jobs, api_key):
    """One call. Returns {id: {score, why}} or None on any failure (caller falls back)."""
    profile = (
        "Candidate: solo founder and senior engineer in Saudi Arabia. Strong in AI, "
        "automation, LLM apps, Python, backend, and cloud SaaS. Wants senior or above "
        "AI/ML, software, product, or data roles based in Saudi Arabia. Values simple, "
        "reliable, high-ownership work."
    )
    listing = [
        {"id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"]}
        for j in jobs
    ]
    prompt = (
        f"{profile}\n\nScore each job 0-100 for fit to this candidate and give a one-line "
        f"reason of at most 15 words. Return ONLY a JSON array of objects "
        f'{{"id","score","why"}}.\nJobs:\n{json.dumps(listing, ensure_ascii=False)}'
    )
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
    }
    try:
        r = requests.post(endpoint, json=body, timeout=60)
        if r.status_code != 200:
            log(f"WARN Gemini HTTP {r.status_code}: {r.text[:200]}")
            return None
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        arr = json.loads(text)
        return {
            str(it["id"]): {"score": int(it["score"]), "why": str(it["why"])[:140]}
            for it in arr
        }
    except Exception as e:
        log(f"WARN Gemini failed: {e}")
        return None


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
    if not webhook:
        log("FATAL: DISCORD_WEBHOOK_URL not set")
        sys.exit(1)
    gemini_key = os.environ.get("GEMINI_API_KEY")  # optional

    seen = load_seen()

    all_jobs = []
    for c in COMPANIES:
        all_jobs.extend(fetch_company(c))
        time.sleep(0.5)

    # dedupe within the run (same posting could appear twice)
    uniq = {}
    for j in all_jobs:
        uniq[j["url"] or j["id"]] = j
    all_jobs = list(uniq.values())

    fresh = [j for j in all_jobs if matches_filters(j) and j["id"] not in seen]
    log(f"{len(all_jobs)} fetched, {len(fresh)} new and matching")

    if not fresh:
        log("No new matching jobs. Done.")
        return

    ranking = gemini_rank(fresh, gemini_key) if gemini_key else None
    if gemini_key and ranking is None:
        log("Gemini unavailable, using keyword scoring for this run")
    for j in fresh:
        if ranking and j["id"] in ranking:
            j["score"] = ranking[j["id"]]["score"]
            j["why"] = ranking[j["id"]]["why"]
        else:
            j["score"] = keyword_score(j)
            j["why"] = "matches your target titles and skills"

    fresh.sort(key=lambda x: x["score"], reverse=True)
    fresh = fresh[:MAX_JOBS_IN_DIGEST]

    today = datetime.date.today().isoformat()
    lines = [f"**Job digest — {today}**  ({len(fresh)} new in Saudi Arabia)"]
    by_company = {}
    for j in fresh:
        by_company.setdefault(j["company"], []).append(j)
    for comp, jobs in by_company.items():
        lines.append(f"\n__**{comp}**__")
        for j in jobs:
            lines.append(
                f"- **{j['title']}** — {j['location']} · fit {j['score']}\n"
                f"  {j['why']}\n  <{j['url']}>"
            )

    post_discord(webhook, lines)

    for j in fresh:
        seen[j["id"]] = today
    save_seen(seen)
    log(f"Posted {len(fresh)} jobs. seen.json now holds {len(seen)} ids.")


# ----------------------------- CANDIDATES -----------------------------------
# KSA/GCC tech firms to add once their ATS + token are confirmed in Claude Code.
# Workable and Lever readers now exist, so any of these four ATS types is one line.
# To confirm one, open its careers page and read where "Apply" sends you:
#   jobs.ashbyhq.com/<token>        -> source "ashby"
#   boards[.eu].greenhouse.io/<tk>  -> source "greenhouse" (set region)
#   apply.workable.com/<token>      -> source "workable"
#   jobs.lever.co/<token>           -> source "lever"
#
#   Foodics   -> DONE (workable/foodics, in COMPANIES above, verified 24 Jun 2026)
#   Tabby     -> ATS unconfirmed (not Greenhouse US/EU)
#   Salla     -> ATS unconfirmed (not Greenhouse)
#   Mozn      -> ATS unconfirmed (KSA AI; try Ashby/Lever)
#   Unifonic  -> ATS unconfirmed
#   Rasan / Sary / Nana / Jahez / Zid -> ATS unconfirmed
# ----------------------------------------------------------------------------


if __name__ == "__main__":
    main()
