# Job-search digest

A daily digest of new senior tech jobs in Saudi Arabia, posted to Discord at 07:00 AST. Same shape as your AI-news digest: gather, filter, rank, deliver, run on GitHub Actions cron.

This folder is the plan plus the working code. Build and test the live parts in Claude Code. The Cowork sandbox can't POST to Discord or call Gemini, so the real test runs where the network is open.

## The source decision

You asked whether LinkedIn is still the best source. It isn't, for an automated daily job.

LinkedIn Jobs and Indeed have no free API. Scraping them breaks their terms, the HTML changes often, and they block datacenter IPs like GitHub Actions runners fast. That path is fragile and you'd spend your time fixing scrapers.

The reliable source is each company's own hiring system. The companies you care about post through free, public, JSON APIs: Greenhouse, Ashby, Workable, Lever, and Pinpoint. No keys, no scraping, allowed by design. You point the script at a list of companies and pull their open roles directly. The engine reads all five, every one verified live against the real API.

There's no single board that covers Saudi tech, so the list is built company by company. That's the real work here, and why expanding it means confirming each firm's system, not guessing.

## Verified live (24 Jun 2026)

I fetched all four from the real APIs during the build:

- **HALA** — Greenhouse, token `hala`, US host. 15 roles including AI Engineer, Automation Engineer, Site Reliability Engineer, Software Backend Engineer (Riyadh).
- **Careem** — Greenhouse, token `careem`, US host. AI and founding-engineer roles, including a Founding Forward Deployed Engineer for enterprise agentic AI. Mostly UAE and Pakistan, so few Saudi roles surface, but it costs nothing to keep.
- **Tamara** — Greenhouse, token `tamara`, EU host. 30 roles including Senior Product Engineer - Backend, Product Engineer II, Senior Product Manager (Riyadh).
- **Lean Technologies** — Ashby, token `LeanTech`. Roles including Senior Solutions Engineer and Staff DevOps Engineer (Riyadh).

All four return the fields the matcher needs: title, location, apply URL, publish date.

## Coverage (23 Jul 2026)

The old candidate list is fully resolved. 14 companies now feed the pipeline: **216 postings fetched, 24 matched** — up from 127 and 10.

- **Salla** — Workable, 27 roles. The strongest single addition: 13 matches, including the highest-scoring roles in the whole digest (Senior AI Backend Engineer, Senior SRE/MLOps, Senior Data Scientist, all Saudi).
- **Tabby** — Pinpoint. `careers.tabby.ai` redirects to `tabby.pinpointhq.com`; that's why Greenhouse came up empty. 62 roles, 26 in Saudi.
- **Mozn, Unifonic, Jahez, Zid, Nana, Rasan, Sary** — real Workable accounts, but 0 open roles today. Kept in `COMPANIES` on the Careem precedent: one cheap fail-soft call a day, and a senior role surfaces the moment one lands.

### Ruled out — don't re-litigate

Big Saudi employers mostly can't be reached without scraping, which this project refuses:

- **stc** runs SuccessFactors (`sapsf.com`) — no free public JSON API.
- **NEOM** serves its own SPA with no public feed found.
- **Aramco** is not on Greenhouse. The "greenhouse" string on its careers page is *greenhouse gas* — it's an oil company. Easy false positive.
- **SmartRecruiters** is useless for discovery: its API answers `200 {"totalFound":0}` for *any* string, so it happily confirms companies that don't exist. 13 KSA employer ids were probed, all empty. No verified company means no fetcher.

Reaching these would need the JSearch aggregator (`RAPIDAPI_KEY`), which is deliberately left off.

## Adding a company

Run the discovery probe first:

```
python digest.py --discover salla tabby mozn
```

It probes all five ATS APIs and prints a ready-to-paste `COMPANIES` line with the live job count. It guards itself with a known-bad control token first — if an ATS answers for nonsense, its results are reported `UNRELIABLE` rather than believed. That guard is the whole point; without it SmartRecruiters-style APIs invent companies.

A miss isn't proof of absence — the probe guesses the company name as the token, so anything whose token differs (Lean is `LeanTech`) stays invisible. In that case, open the careers page and read where Apply sends you:

- `boards.greenhouse.io/<token>` or `job-boards.eu.greenhouse.io/<token>` — Greenhouse. Add `{"source":"greenhouse","token":"<token>","region":"us"|"eu"}`.
- `jobs.ashbyhq.com/<token>` — Ashby. Add `{"source":"ashby","token":"<token>"}`.
- `apply.workable.com/<token>` — Workable. Add `{"source":"workable","token":"<token>"}`.
- `jobs.lever.co/<token>` — Lever. Add `{"source":"lever","token":"<token>"}`.
- `<token>.pinpointhq.com` — Pinpoint. Add `{"source":"pinpoint","token":"<token>"}`.

Every fetch is fail-soft: a wrong token or a down API logs a WARN and is skipped, so a bad entry never breaks the run.

## How it filters

The job is matching, not relevance ranking, so the filter is strict before anything gets scored.

1. **Location** must be Saudi (Riyadh, Jeddah, Dammam, and so on). Everything else is dropped, including the Dubai and India roles these companies post.
2. **Seniority** drops junior, intern, graduate, trainee, tamheer, associate, entry — and, since 31 Jul 2026, everything above senior IC too: lead, staff, principal, head-of, director, engineering manager, team leader. The 2026-07-31 digest scored Lead/Staff roles 85–98 because seniority was only bounded from below.
2b. **Title-vs-location conflict** drops a job whose title says it's really elsewhere ("Bangkok based", "relocation provided") even when the board's location metadata claims Saudi. Same digest delivered Agoda's Bangkok role as Riyadh because only the metadata was checked.
3. **Title** must hit one of your target areas: engineer, software, AI/ML, data, DevOps/SRE, platform, automation, architect, product manager, solutions engineer.
4. **Not seen before.** `seen.json` holds every id already sent, so you only get new postings.

Salary is a soft signal, not a filter. These boards almost never publish a number, so a hard salary floor would reject nearly everything. The benchmark for your floor: senior engineer in Riyadh runs SAR 25,000 to 35,000 per month in 2026, AI/ML 28,000 to 35,000. The config records SAR 25,000/month for reference and never rejects a posting for missing salary.

Pinpoint is the one source that does publish structured pay (`compensation_minimum/maximum`, currency, frequency), so Tabby roles can show a real `SAR/month` range in the digest line. It stays a display signal — nothing is ever rejected on it.

One caveat the filter can't catch: some Gulf roles are nationality-gated (for example Tamara's "Builders Program - Emirati National" lines). The title doesn't always say so. You'll spot these when you read the digest.

## How it ranks

One Gemini call scores the survivors 0 to 100 and writes a one-line "why it fits," using a short profile of you. Since 31 Jul 2026 the prompt carries an explicit rubric (90+ only for senior-IC core-area roles whose JD requirements the profile covers; leadership/staff/wrong-location scores 0–39; never above 85 without JD text) and each job includes a JD excerpt, so scores reflect actual requirements rather than title vibes. If `GEMINI_API_KEY` is missing or the call fails, it falls back to deterministic keyword scoring so a model outage never kills the run. Same fallback design as the news digest.

## Tailored resumes

Every delivered job (top 10 by score per run) gets `resume.md` — the master resume — rewritten against that job's actual JD text: reordered and re-emphasized, truth-constrained to the master (the prompt forbids inventing anything), ending with a fit-notes section that lists real gaps instead of papering over them. JD text comes free from Ashby/Lever/Pinpoint/JSearch payloads; Greenhouse costs one GET per delivered job and Workable one cached GET per company. Output lands in `tailored/<date>/<company>--<title>.md`, the workflow commits it back, and each digest line links its file directly. Missing `resume.md` or `GEMINI_API_KEY` skips tailoring; the digest still delivers.

Keep `resume.md` in sync with the master resume in Cowork (`OUTPUTS/master-resume/master-resume.md`) — it is the single source of truth the tailoring quotes from.

Two models, on purpose. Both ids checked against [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models) on 2 Aug 2026 and listed **stable**:

- `RANK_MODEL = gemini-3.1-flash-lite` — scoring is classification over at most 25 short records in a single call. Flash-Lite is built for that and is the cheapest option.
- `TAILOR_MODEL = gemini-3.6-flash` — rewriting a whole resume is long-form generation under hard truth constraints. Google's own guidance puts Flash-Lite at classification, extraction and basic summarization, and Flash at longer reasoning chains and sustained coherence. On the tailoring prompt Flash-Lite drops sections, loosens the no-invention rule, and writes thin Fit notes. It runs at most `TAILOR_MAX` times a day, so the extra cost is bounded.

Note `gemini-3.1-flash-lite-preview` is shut down; the stable id above is not. Re-check both when a run starts returning HTTP 404 from the model endpoint.

## Delivery

Discord webhook. The digest is grouped by company, sorted by fit score, and split into sub-2000-character messages so it never hits Discord's limit. A failed POST exits non-zero so a broken run is loud, not silent.

## Files

- `digest.py` — the whole engine. Config block and company list at the top, the five ATS fetchers in the middle, ranking + tailoring after them, candidates list and `--discover` mode at the bottom. One file, no framework.
- `resume.md` — the master resume the tailoring stage rewrites. Update it whenever the Cowork master changes.
- `tailored/` — one folder per digest date, one tailored resume per delivered job. Committed back by the workflow.
- `test_digest.py` — offline tests (no network, everything mocked): the six real jobs from 2026-07-31, both scoring bugs, JD fetch, and three failure paths. `python test_digest.py`.
- `.github/workflows/digest.yml` — the cron. `0 4 * * *` is 07:00 AST. Includes a manual run button and the step that commits `seen.json`, `stats.json`, and `tailored/` back.
- `requirements.txt` — just `requests`.
- `seen.json` — created on first run. Don't commit it by hand; the workflow does.

## Setup

1. New private GitHub repo. Drop these files in (keep the `.github/workflows/` path).
2. Discord: Server Settings, Integrations, Webhooks, New Webhook, pick the channel, Copy Webhook URL.
3. Repo Settings, Secrets and variables, Actions. Add `DISCORD_WEBHOOK_URL`. Add `GEMINI_API_KEY` if you want LLM scoring.
4. Actions tab, run the workflow manually once. Read the log.

## Test before you trust it (in Claude Code)

- Run `python digest.py` locally with the two env vars set. Confirm a message lands in Discord.
- Read the fetch log. Each company prints its job count or a WARN. Confirm HALA, Tamara, Careem, and Lean all return jobs.
- Check the first digest by hand: are the roles actually Saudi, actually senior, actually relevant? Tune `TITLE_INCLUDE` / `TITLE_EXCLUDE` if not.
- Kill the Gemini key for one run and confirm the keyword fallback still posts.
- Then let the cron take over.

## What's deliberately simple

No database. State is one JSON file committed back by the Action. No scraper, no browser automation, no queue. Five ATS readers because the companies genuinely split across them, not for their own sake — each one was added only after a company you actually want forced it, and only after the live API was seen returning data.
