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

The digest makes that confidence visible instead of hiding borderline roles. **Strong matches** have a validated Gemini score of at least 80 and JD text. **Worth reviewing** passed every hard eligibility filter but have a lower or invalid score, no JD text, or another fit uncertainty. This favors finding viable roles without presenting every result as equally ready to apply for. Every delivered job, either tier, still gets its own Tailor CV button (see below); a card without captured JD text says so in its footer and the button reports back what's missing instead of guessing.

`RANK_MODEL = gemini-3.1-flash-lite` does the scoring — classification over at most 25 short records in a single call, which is what Flash-Lite is built for and the cheapest option available. The id was checked against [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models) on 2 Aug 2026 and listed **stable**; `gemini-3.1-flash-lite-preview` is shut down. Re-check when a run starts returning HTTP 404 from the model endpoint.

## Tailoring, on demand

Every card in the digest — Strong or Worth reviewing — carries a "Tailor CV" button. Its `custom_id` is `tailor:<first 32 hex chars of sha256(job_key)>`; the job key itself never appears in the id. Clicking it sends a signed Ed25519 interaction to the Cloudflare Worker, which checks the interaction's guild, channel, and user against its own configured ids before doing anything else — only the one person this deployment is scoped to can trigger a tailor.

If that check passes, the Worker looks up the immutable snapshot captured for that exact Discord message at delivery time and re-verifies the job description text against the SHA-256 recorded in the snapshot. Only if it still matches does the Worker write a task row to D1 (with the interaction token encrypted) and trigger the `interactive.yml` workflow with the task id. `interactive_runner.py` fetches that task over HMAC-authenticated HTTP and re-checks the JD hash and completeness a second time before anything is generated.

### How the tailoring stays honest

`resume_render.parse_resume` splits the master into sections, entries and bullets, giving every bullet a stable ref. `resume_tailor` sends those refs with the JD and gets back the refs to keep, in order, each with its rewritten wording. Nothing without a ref can enter the document.

`resume_source.validate_tailored_resume` then checks each rewrite against the exact bullet it cites: every number must survive verbatim, and every proper noun must already exist in the master or the job title, so an invented framework or cloud platform is rejected rather than sent. A rejected draft goes back to the model once with the specific failures; if it fails again, the untouched master resume is delivered with the reason stated in the message. The same thing happens when `GEMINI_API_KEY` is absent — you get your master resume and a note saying why it wasn't tailored. A failure costs you the tailoring, never the document.

The PDF is single column, standard fonts, no tables, forced to one page by shrinking type first and dropping the least relevant bullets second. Gaps and fit notes travel in the Discord message, never in the PDF — the attachment is always safe to forward as-is.

The Worker attaches the PDF to the original interaction as an ephemeral reply (`flags: 64`), visible only to the requester. No channel message is posted, and nothing tailored is ever committed to the repository — that's a stated constraint of the design, not an oversight.

`resume_tailor.py` picks its own model, `TAILOR_MODEL`, defaulting to `gemini-3.6-flash` and overridable by environment variable. Rewriting a resume under hard truth constraints is long-form generation; Google's guidance puts Flash-Lite at classification and extraction, and Flash at longer reasoning chains. Flash-Lite drifts on this job — it drops sections and loosens the no-invention rule. Scoring is the cheaper model and is configured separately in `digest.py` (see "How it ranks" above).

Keep `resume.md` in sync with the master resume in Cowork (`OUTPUTS/master-resume/master-resume.md`) — it is the single source of truth every tailored CV is built from.

## Delivery

The primary path is application-owned: the daily run builds one immutable snapshot per delivered job and POSTs them all to the Cloudflare Worker's `/internal/digest` route (HMAC-signed, single-use nonce). The Worker posts one Discord embed per job as the application itself — not a webhook — each with its own Tailor CV button.

Posting is idempotent. A `delivery_claims` row per job key, held under a lease, means a retried run never double-posts the same job. Only jobs the Worker confirms as `posted` or `existing` are recorded in `seen.json`, so a job that never actually made it to Discord is retried the next day instead of silently disappearing.

If the Worker is unreachable, the run falls back to the plain Discord webhook and posts the old-style text digest: grouped by company within Strong matches and Worth reviewing, sorted by fit score, split into sub-2000-character messages so it never hits Discord's limit.

The fallback is deliberately *not* used when the Worker answers but every card fails — posting the same jobs through two channels is worse than posting them late, and nothing is marked seen, so the next run retries them. Instead the run exits non-zero, so a day where the digest reached nobody shows up as a red workflow rather than a green one. A broken run stays loud.

Link verification is evidence, not a source of false negatives. A link marked *inconclusive* means GitHub Actions received a block, rate limit, authentication gate, or network failure; the role remains in the digest because it may open from your browser. Only a definite closure (for example a not-found response or a closed-posting page) is dropped.

Use [the weekly review template](docs/weekly-job-review-template.md) for a two-minute review of saved, applied, noisy, and inaccessible roles. That feedback is deliberately manual: it tells you when to tune the score threshold, profile wording, or filters without allowing the workflow to change selection rules silently.

## Files

**Daily pipeline**

- `digest.py` — the whole engine. Config block and company list at the top, the five ATS fetchers, filters, and ranking in the middle, delivery and the candidates/`--discover` mode at the bottom. One file, no framework.
- `resume.md` — the master resume the on-demand tailoring quotes from. Update it whenever the Cowork master changes.
- `profile.json` — your job-fit profile (blurb, skill keywords, target titles) sent to Gemini for scoring and used by the keyword fallback. Refreshed by hand or via `PROFILE_REFRESH_PROMPT.md`.
- `requirements.txt` — `requests` for every HTTP call, `pypdf` and `reportlab` for building the tailored PDF.
- `discord_delivery.py` — signs and POSTs the daily batch of job cards to the Worker's `/internal/digest` route.
- `job_archive.py` — builds job snapshots (including the JD SHA-256), the Discord card/button payload, and writes `jobs/<date>.json`.
- `interactive_runner.py` — runs one on-demand tailoring task: re-verifies the JD hash, builds the two-page PDF, reports the result back to the Worker.
- `resume_source.py` — evidence helpers shared by resume ingestion and tailoring: splits `resume.md` into quotable source blocks and checks a tailored draft's claims against them.
- `resume_ingest.py` — normalizes raw resume text (from a `/resume` upload) into the form `resume.md` needs, with a preview step before anything is accepted as the new master.
- `test_digest.py` — offline tests for the daily pipeline (no network, everything mocked): the six real jobs from 2026-07-31, both scoring bugs, JD fetch, and three failure paths.
- `test_interactive.py` — offline contract tests for the snapshot/tailoring/Worker-callback pipeline.
- `.github/workflows/digest.yml` — the cron (`0 4 * * *` = 07:00 AST). Runs the Python and Worker test suites, runs `digest.py`, then commits `seen.json`, `stats.json`, and `jobs/` back.
- `jobs/` — one `<date>.json` per digest date: full evidence (title, company, location, URL, score, tier, link status, complete JD text and its SHA-256) for every delivered job. Committed back by the workflow; same-day re-runs append, never rewrite.
- `seen.json` — job ids already delivered. Created on first run; only the workflow commits it.
- `stats.json` — the per-run trend (fetched/matched/new/avg-score) the drift warning reads.

**On-demand tailoring (Cloudflare Worker)**

- `worker/src/index.js` — the Worker: verifies Discord interactions, claims delivery of daily cards idempotently, creates and serves tailoring tasks, relays the PDF back as an ephemeral reply.
- `worker/schema.sql` — the D1 schema: `tasks`, `deliveries`, `delivery_claims`, `internal_nonces`, `job_snapshots`.
- `worker/wrangler.jsonc` — Worker config: non-secret `vars` (Discord application/guild/channel/user ids, the GitHub repository, the Discord public key) and the D1 binding. No secret values live here.
- `worker/test/interaction.test.js` — the Worker's Node test suite (`node --test`): signature verification, channel/user gating, snapshot hash checks, idempotent delivery claims, nonce handling.
- `worker/package.json` — Worker has no runtime dependencies; `npm test` runs `node --test`.
- `.github/workflows/interactive.yml` — triggered by the Worker (`workflow_dispatch` with a `task_id` input) to run `interactive_runner.py` for one tailoring task.

**Historical / reference**

- `tailored/` — tailored resumes from the old, now-removed auto-tailoring pipeline. Historical output, not produced by anything on `main` anymore.
- `JOBS.md` — a dated snapshot of verified job listings from 2026-07-23. Not a living document; don't expect it to track current postings.
- `PROFILE_REFRESH_PROMPT.md` — the monthly prompt used to review and update `profile.json` by hand.
- `docs/weekly-job-review-template.md` — copy this once a week to record which Strong and Review roles were useful, then use the notes to calibrate the workflow deliberately.
- `docs/superpowers/` — design specs and plans from when features were built. Records of a decision at a point in time, not maintained docs — don't edit them to match today's code.
- `docs/2026-07-31-ai-agent-plan.md` — a proposal for a future Claude Code review agent. Not implemented: there is no `.github/workflows/agent.yml`.

## CI/CD hardening

The repository has four explicit GitHub Actions workflows:

- **CI** runs on pull requests, code pushes to `main`, and manual dispatch. It has no production secrets and checks the Python suite, Worker suite, and workflow syntax. State-only digest commits are ignored.
- **job-digest** still targets 07:00 Riyadh (`04:00 UTC`), but GitHub cron is a daily window rather than an exact-time scheduler. It runs preflight tests, executes with read-only access, uploads seven-day generated-state evidence, then uses a separate write-only persistence job to commit only `seen.json`, `stats.json`, and dated `jobs/` snapshots.
- **Interactive job task** runs the same preflight tests before processing the opaque Worker task identifier. Its Worker callback remains the authority for task completion and retry state.
- **Digest freshness watchdog** runs at 11:00 Riyadh and alerts the private Discord webhook if the latest completed digest is not successful for the Riyadh day or `stats.json` lacks that day. It is best-effort and shares GitHub cron's scheduling limits.

All third-party Actions are pinned to immutable commits. Python runtime dependencies are edited in `requirements.in`; regenerate the hash-locked `requirements.txt` with `pip-compile --generate-hashes requirements.in` and install it with `python -m pip install --require-hashes -r requirements.txt`. Dependabot opens weekly update PRs for both pip packages and Action pins.

Failure alerts contain only the workflow, stage, timestamp, and GitHub run URL. They do not include logs, resumes, job data, or secrets. If delivery has succeeded but state persistence fails, do **not** rerun the digest: download the retained state artifact from the failed run, validate/apply it with `workflow_guard.py`, and make a normal scoped commit. Roll back an automation defect by reverting its code/workflow commit; never force-push or delete state history.

## Setup

Two halves: the repository that runs the daily job, and the Cloudflare Worker that owns Discord delivery and on-demand tailoring.

**Repository**

1. New private GitHub repo. Drop these files in (keep the `.github/workflows/` path).
2. Repo Settings, Secrets and variables, Actions. Add, per `.github/workflows/digest.yml`:
   - `DISCORD_WEBHOOK_URL` — the fallback delivery path, used whenever the Worker can't be reached. Not optional: if interactive delivery fails and this isn't set, the run logs FATAL and exits non-zero instead of silently dropping the digest.
   - `INTERACTIVE_WORKER_URL` and `WORKER_CALLBACK_SECRET` — the Worker's address and the shared HMAC secret. Without these the run always falls back to the plain webhook and never posts a Tailor CV button.
   - `RAPIDAPI_KEY` — optional. Without it the digest runs against the five ATS APIs only, no JSearch.
   - `GEMINI_API_KEY` — optional. Without it ranking falls back to deterministic keyword scoring.
   `.github/workflows/interactive.yml` reuses `INTERACTIVE_WORKER_URL` and `WORKER_CALLBACK_SECRET` — same secrets, same names.
3. Actions tab, run the `job-digest` workflow manually once. Read the log.

**Cloudflare Worker**

1. Create a Discord application; point its interactions endpoint URL at the deployed Worker.
2. Create a D1 database and load it from `worker/schema.sql`.
3. Fill in the non-secret `vars` in `worker/wrangler.jsonc`: the Discord application, guild, channel and user ids, the GitHub repository, and the Discord public key (public by design, safe to commit).
4. Set the Worker's secret values with `wrangler secret put` — these are not committed anywhere: `DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `CALLBACK_HMAC_SECRET`, `TASK_ENCRYPTION_KEY`.

**Registering the slash commands.** `worker/commands.js` is the source of
truth for which slash commands exist. `npm run register --prefix worker`
applies it to the Discord application, guild-scoped so it takes effect
immediately. It uses `PUT`, so a command removed from that file is removed
from Discord on the next run. It needs `DISCORD_BOT_TOKEN`,
`DISCORD_APPLICATION_ID` and `DISCORD_GUILD_ID` in the environment — the
latter two are in `worker/wrangler.jsonc`. Run it by hand when the command
set changes; no workflow runs it.

## Test before you trust it (in Claude Code)

- Run `python -m unittest test_digest.py test_interactive.py`. CI runs this offline regression suite before it calls any live API.
- Run `cd worker && node --test`. CI runs this too, right after the Python suite, before `digest.py` ever runs.
- Run `python digest.py` locally with `DISCORD_WEBHOOK_URL` set (and `INTERACTIVE_WORKER_URL` / `WORKER_CALLBACK_SECRET` if you want to exercise the Worker path). Confirm a digest lands in Discord.
- Read the fetch log. Each company prints its job count or a WARN. Confirm HALA, Tamara, Careem, and Lean all return jobs.
- Check the first digest by hand: are the roles actually Saudi, actually senior, actually relevant? Tune `TITLE_INCLUDE` / `TITLE_EXCLUDE` if not.
- Kill the Gemini key for one run and confirm the keyword fallback still posts.
- Then let the cron take over.

## What's deliberately simple

No scraper, no browser automation, no queue, no framework. `digest.py` is one file for the whole daily pipeline. Five ATS readers because the companies genuinely split across them, not for their own sake — each one was added only after a company you actually want forced it, and only after the live API was seen returning data.

There is a database now — D1, five tables — but it's deliberately small: coordination state for the interaction flow (which delivery claimed which job, which task is waiting on which Discord token, a short-lived nonce table). The daily job's own state is still exactly what it always was: JSON files (`seen.json`, `stats.json`, `jobs/`) committed back by the Action. Nothing about the daily pipeline needed a database; the button flow did, because a Discord interaction has to be looked up minutes or days after the run that created it.
