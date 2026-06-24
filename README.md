# Job-search digest

A daily digest of new senior tech jobs in Saudi Arabia, posted to Discord at 07:00 AST. Same shape as your AI-news digest: gather, filter, rank, deliver, run on GitHub Actions cron.

This folder is the plan plus the working code. Build and test the live parts in Claude Code. The Cowork sandbox can't POST to Discord or call Gemini, so the real test runs where the network is open.

## The source decision

You asked whether LinkedIn is still the best source. It isn't, for an automated daily job.

LinkedIn Jobs and Indeed have no free API. Scraping them breaks their terms, the HTML changes often, and they block datacenter IPs like GitHub Actions runners fast. That path is fragile and you'd spend your time fixing scrapers.

The reliable source is each company's own hiring system. The companies you care about post through free, public, JSON APIs: Greenhouse, Ashby, Workable, and Lever. No keys, no scraping, allowed by design. You point the script at a list of companies and pull their open roles directly. The engine now reads all four — Greenhouse, Ashby, Workable, and Lever (all four verified live against the real APIs).

There's no single board that covers Saudi tech, so the list is built company by company. That's the real work here, and why expanding it means confirming each firm's system, not guessing.

## Verified live (24 Jun 2026)

I fetched all four from the real APIs during the build:

- **HALA** — Greenhouse, token `hala`, US host. 15 roles including AI Engineer, Automation Engineer, Site Reliability Engineer, Software Backend Engineer (Riyadh).
- **Careem** — Greenhouse, token `careem`, US host. AI and founding-engineer roles, including a Founding Forward Deployed Engineer for enterprise agentic AI. Mostly UAE and Pakistan, so few Saudi roles surface, but it costs nothing to keep.
- **Tamara** — Greenhouse, token `tamara`, EU host. 30 roles including Senior Product Engineer - Backend, Product Engineer II, Senior Product Manager (Riyadh).
- **Lean Technologies** — Ashby, token `LeanTech`. Roles including Senior Solutions Engineer and Staff DevOps Engineer (Riyadh).

All four return the fields the matcher needs: title, location, apply URL, publish date.

## Candidates to add

These are strong KSA/GCC tech firms I couldn't fully wire from the sandbox. The Lever and Workable APIs kept timing out here, so I won't ship a fetcher I haven't seen return data. Confirm each in Claude Code, where the network is open, then add a line to `COMPANIES`.

- **Foodics** — **DONE.** Wired on Workable (`apply.workable.com/foodics`), verified live (32 roles, pagination confirmed). It's in `COMPANIES`. Note: its current Saudi postings are interns/sales, so the strict filter shows nothing from it yet — it stays in like Careem, surfacing a senior role when one lands.
- **Tabby** — not on Greenhouse (US or EU, both checked and empty). ATS still unknown. Open tabby.com/careers, follow the Apply link, tell me the host.
- **Salla, Mozn, Unifonic, Rasan, Sary, Nana, Jahez, Zid** — ATS unconfirmed. Mozn (KSA AI) is worth checking on Ashby and Lever first.

I checked and ruled out Greenhouse for all of these, so don't add them as Greenhouse tokens. The `CANDIDATES` comment block at the bottom of `digest.py` lists the same set with the URL pattern for finding each token.

## Adding a company

Open its careers page and read where Apply sends you:

- `boards.greenhouse.io/<token>` or `job-boards.eu.greenhouse.io/<token>` — Greenhouse. Add `{"source":"greenhouse","token":"<token>","region":"us"|"eu"}`.
- `jobs.ashbyhq.com/<token>` — Ashby. Add `{"source":"ashby","token":"<token>"}`.
- `apply.workable.com/<token>` — Workable. Add `{"source":"workable","token":"<token>"}`.
- `jobs.lever.co/<token>` — Lever. Add `{"source":"lever","token":"<token>"}`.

Every fetch is fail-soft: a wrong token or a down API logs a WARN and is skipped, so a bad entry never breaks the run.

## How it filters

The job is matching, not relevance ranking, so the filter is strict before anything gets scored.

1. **Location** must be Saudi (Riyadh, Jeddah, Dammam, and so on). Everything else is dropped, including the Dubai and India roles these companies post.
2. **Seniority** drops junior, intern, graduate, trainee, tamheer, associate, entry.
3. **Title** must hit one of your target areas: engineer, software, AI/ML, data, DevOps/SRE, platform, automation, architect, product manager, solutions engineer.
4. **Not seen before.** `seen.json` holds every id already sent, so you only get new postings.

Salary is a soft signal, not a filter. These boards almost never publish a number, so a hard salary floor would reject nearly everything. The benchmark for your floor: senior engineer in Riyadh runs SAR 25,000 to 35,000 per month in 2026, AI/ML 28,000 to 35,000. The config records SAR 25,000/month for reference and never rejects a posting for missing salary.

One caveat the filter can't catch: some Gulf roles are nationality-gated (for example Tamara's "Builders Program - Emirati National" lines). The title doesn't always say so. You'll spot these when you read the digest.

## How it ranks

One Gemini call scores the survivors 0 to 100 and writes a one-line "why it fits," using a short profile of you. If `GEMINI_API_KEY` is missing or the call fails, it falls back to deterministic keyword scoring so a model outage never kills the run. Same fallback design as the news digest.

Confirm the exact Gemini model id against current Google AI docs when you build. The config has `gemini-3.1-flash-lite` as you specified, but model strings change and I won't assume it's current.

## Delivery

Discord webhook. The digest is grouped by company, sorted by fit score, and split into sub-2000-character messages so it never hits Discord's limit. A failed POST exits non-zero so a broken run is loud, not silent.

## Files

- `digest.py` — the whole engine. Config block and company list at the top, Greenhouse and Ashby fetchers, candidates list at the bottom. One file, no framework.
- `.github/workflows/digest.yml` — the cron. `0 4 * * *` is 07:00 AST. Includes a manual run button and the step that commits `seen.json` back.
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

No database. State is one JSON file committed back by the Action. No scraper, no browser automation, no queue. Two ATS readers because the companies genuinely split across them, not for their own sake. Add Lever or Workable readers only when a company you want forces it.
