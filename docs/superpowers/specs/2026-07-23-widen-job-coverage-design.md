# Widen job coverage — design

**Date:** 2026-07-23

## Problem

The digest reads 5 companies (HALA, Careem, Tamara, Lean, Foodics) across 4 ATS APIs. The
last run fetched 127 postings and matched **10**. The bottleneck is the company list, not the
filter: `COMPANIES` in `digest.py` is the whole funnel.

Two coverage gaps matter: more KSA startups, and big Saudi employers.

## Constraints

Two rules from `README.md` govern every decision here:

1. Free public JSON APIs only. No scraping (`README.md:11`).
2. Never ship a fetcher you haven't seen return data (`README.md:30`).

## What live probing established

Every claim below was verified against the real API during design, not inferred.

| Finding | Evidence |
|---|---|
| **Salla → Workable, live** | `POST apply.workable.com/api/v3/accounts/salla/jobs` → `total: 25`. Page 1: Senior Data Scientist (Madinah), Senior Backend Engineer (Jeddah), Senior AI Backend Engineer (Makkah), Senior PM (Jeddah), Engineering Manager (Makkah), Head of Security. |
| **Tabby → Pinpoint, live** | `careers.tabby.ai` redirects to `tabby.pinpointhq.com`. `GET /postings.json` → 63 postings, 26 in KSA. New ATS type. |
| **7 KSA firms hold dormant Workable accounts** | Mozn, Unifonic, Jahez, Zid, Nana, Rasan, Sary — v3 returns `{"total":0}`: account real, no open roles. |
| **Big employers mostly unreachable cleanly** | stc → SuccessFactors (`sapsf.com`), no free JSON API. NEOM → own-host SPA. Aramco → the "greenhouse" string on its careers page is *greenhouse gas*, not the ATS. |
| **SmartRecruiters unusable for discovery** | Returns `200 {"totalFound":0}` for **any** string including a nonsense control. 13 KSA IDs guessed, all 0. |
| **Static ATS fingerprinting fails on SPAs** | `curl \| grep` for ATS strings found nothing on tabby/mozn/unifonic/jahez/zid careers pages. Use direct-API probes and redirect-following. |

Two measurement traps found the hard way, both now guarded against:

- The Workable **v1 widget** endpoint undercounts relative to the **v3** endpoint the engine
  actually uses. Always probe the endpoint the fetcher uses.
- SmartRecruiters 200s on everything. Any probe needs a known-bad control token before its
  result can be trusted.

## Decision

**Clean APIs only.** No SuccessFactors/Workday scrapers (rejected: fragile, violates rule 1).
No JSearch aggregator activation. Big employers are reached only where a clean API exists;
stc/NEOM/Aramco are explicitly out of scope and recorded as such so they aren't re-litigated.

## Design

### `fetch_pinpoint` — new ATS reader

`GET https://{token}.pinpointhq.com/postings.json`. No key, no pagination, flat `data` array.

Follows the existing fetcher contract exactly: takes a company dict, returns a list of job
dicts, and is **fail-soft** — request error, non-200, or non-JSON logs a `WARN` and returns
`[]`, never raises. Error-handling shape copied from `fetch_greenhouse`.

Field mapping, verified against the live payload:

| Job dict | Pinpoint |
|---|---|
| `id` | `str(j["id"])` |
| `title` | `j["title"]` |
| `location` | `j["location"]["city"] + ", " + j["location"]["name"]` — the `city, country` convention `fetch_workable` already uses. Tabby's country reads `"KSA"`, already in `LOCATION_INCLUDE`. |
| `url` | `j["url"]` (absolute, UUID tail) |
| `first_published` | `""` — Pinpoint publishes no create/publish date, only a null `deadline_at`. |

**The missing date costs nothing.** `first_published` is written by all five fetchers and read
by nothing — the Discord render emits title, location, score, why, and url, with no date.
Freshness is driven entirely by membership in `seen`, not by any date. No fallback is built
for a consumer that does not exist. If a date is wanted later, `seen.json` is already
`{id: first-seen-date}`.

### Company additions

Salla (workable) and Tabby (pinpoint) are live adds. The 7 dormant Workable accounts are added
too, on the Careem/Foodics precedent (`README.md:22`): one cheap fail-soft call per day, and a
senior role surfaces automatically when one lands.

### `--discover` mode

`python digest.py --discover salla tabby mozn` probes each name across the token-based ATS
APIs and prints the confirmed source, token, and live job count.

**Control-token guard is mandatory.** Each ATS is first probed with a known-bad token. If the
control also "succeeds", that ATS cannot confirm existence and results are reported
`UNRELIABLE` rather than as hits. This is what makes the tool trustworthy — without it,
SmartRecruiters-style APIs produce confident false positives.

Probes hit the exact endpoints the fetchers use, not convenience endpoints.

**Known limit:** token-guessing covers Greenhouse / Ashby / Lever / Workable. It will not find
Pinpoint — Tabby was found by following `careers.tabby.ai` → `tabby.pinpointhq.com`, and a
bare name as subdomain doesn't generalize. Pinpoint is confirmed by hand; this is documented,
not engineered around.

### Salary signal

Pinpoint is the first source publishing structured pay (`compensation_minimum/maximum`,
`compensation_currency: "SAR"`, `compensation_frequency: "month"`, gated by
`compensation_visible`), which makes `SALARY_FLOOR_SAR_MONTH` a real signal for the first
time. Carried into the job dict and shown when present. It stays a **soft** signal — a posting
is never rejected for missing or low salary (`README.md:58`).

## Out of scope

- SuccessFactors / Workday / Taleo readers.
- JSearch aggregator activation (`RAPIDAPI_KEY` stays unset).
- A SmartRecruiters fetcher — no verified company would violate rule 2.
- Filter and ranking changes. This widens the funnel, not the sieve.

## Testing

Verification is behavioural, against live APIs:

1. `--discover salla tabby zzznotarealcompany999` — Salla resolves to workable, Tabby is
   reported as hand-confirmed, and the nonsense control resolves to nothing anywhere. A hit on
   the control means the guard is broken.
2. Pinpoint reader returns 63 jobs for Tabby with non-empty title/location/url.
3. Full run with `GEMINI_API_KEY` unset exercises the keyword-scoring fallback; the 7 dormant
   accounts log 0 jobs without a WARN.
4. Fail-soft proof: a bad Pinpoint token logs a WARN and the run still completes.
5. `verify_link` against a live Pinpoint URL. It is generic and fail-soft, and Pinpoint URLs
   carry a UUID tail so the existing redirect check applies — but it has not been tested
   against a *pulled* Pinpoint posting. If Pinpoint serves a 200 shell rather than a 404, it
   needs the same treatment Ashby got.
6. Re-run with state intact confirms no duplicates.

**`JOBS.md` is not generated by anything.** Neither `digest.py` nor the workflow references
it; the current file was produced ad hoc despite `JOBS.md:62` claiming the digest regenerates
it. That stale line gets fixed, and the file is refreshed by hand from the run output.
