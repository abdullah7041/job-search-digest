# Plan 008: Bring the README back in sync with the architecture

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- README.md`
> If `README.md` changed since this plan was written, compare the "Current
> state" excerpts against the live file before proceeding; on a mismatch, treat
> it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first and confirm
> `git rev-parse --short HEAD` prints `6c7541e` or later.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: nothing hard. Run this **after** whichever of plans 001–007
  are actually executed, so the README documents the final state rather than an
  intermediate one. Running it alone is valid — the plan reads the repository
  for its facts and checks the code before documenting behavior that another
  plan may or may not have landed
- **Category**: docs
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

`README.md` is the only architecture document in this repository. There is no
`CLAUDE.md`, no `AGENTS.md`, and no `docs/architecture.md`. It is what a human
or an agent reads to understand the system before changing it.

It currently describes a system that no longer exists. Thirteen commits added a
Cloudflare Worker, a D1 database, a second GitHub workflow, five new Python
modules, and two new dependencies — and removed the auto-tailoring pipeline the
README devotes a whole section to. None of that is reflected.

A stale README is worse than a missing one: it is confidently wrong. Someone
following its "Test before you trust it" section runs `python test_digest.py`
and never learns that CI also runs `test_interactive.py` and a Node suite in
`worker/`. Someone following "Setup" configures two secrets and gets a run that
cannot reach the Worker at all.

This is known outstanding work — task 5 of
`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md` lists
"documentation" among the remaining items. This plan closes it.

## Current state

The file is `README.md`, 140 lines. These sections are wrong and must change:

**Lines 89–100, `## Tailored resumes`** — describes a pipeline that was deleted:

> Only Strong matches (up to `TAILOR_MAX` per run) get `resume.md` — the master
> resume — rewritten against that job's actual JD text… Output lands in
> `tailored/<date>/<company>--<title>.md`, the workflow commits it back, and
> each digest line links its file directly.

None of this happens now. `digest.py` no longer tailors anything; the
`tailored/` directory is historical output; digest lines contain no resume
links; `TAILOR_MAX` is an unused constant. The section also documents
`TAILOR_MODEL = gemini-3.6-flash` as a digest-time model choice, which it no
longer is.

**Lines 102–108, `## Delivery`** — describes the webhook as the delivery
mechanism:

> Discord webhook. The digest is grouped by company within Strong matches and
> Worth reviewing, sorted by fit score, and split into sub-2000-character
> messages so it never hits Discord's limit. A failed POST exits non-zero so a
> broken run is loud, not silent.

The webhook is now the *fallback*. The primary path posts one embed card per
job through the Cloudflare Worker, each carrying a "Tailor CV" button.

**Lines 110–119, `## Files`** — lists 7 files. The repository now has these
that are unlisted: `worker/` (the Worker, its schema and tests), `jobs/` (the
snapshot archive), `discord_delivery.py`, `job_archive.py`,
`interactive_runner.py`, `resume_source.py`, `resume_ingest.py`,
`.github/workflows/interactive.yml`, `test_interactive.py`, and
`docs/superpowers/`. Line 118 says `requirements.txt` is "just `requests`" —
it is now three packages.

**Line 115** — `python test_digest.py`. CI actually runs, per
`.github/workflows/digest.yml`:

```yaml
      - name: Run offline regression tests
        run: python -m unittest test_digest.py test_interactive.py

      - name: Run Worker regression tests
        working-directory: worker
        run: npm test
```

**Lines 121–126, `## Setup`** — lists two secrets (`DISCORD_WEBHOOK_URL`,
`GEMINI_API_KEY`). The workflows now also read `INTERACTIVE_WORKER_URL` and
`WORKER_CALLBACK_SECRET`, and the Worker itself needs its own configuration.

**Lines 128–135, `## Test before you trust it`** — the first bullet gives the
wrong test command.

**Line 139, `## What's deliberately simple`** — "No database." There is now a
D1 database with five tables.

### The facts you need to write the new sections

You cannot infer these from the README, so they are inlined here. Verify each
against the file cited before writing it down.

**Delivery architecture** (`digest.py:1108-1141`, `discord_delivery.py`,
`worker/src/index.js`):

- The daily run builds one snapshot per delivered job (`job_archive.make_snapshot`)
  and POSTs them all to the Worker's `/internal/digest` route, authenticated
  with an HMAC-SHA256 signature over `timestamp\nnonce\nMETHOD\npath\nbody`
  plus a single-use nonce (`discord_delivery.post_interactive_cards`).
- The Worker posts one Discord embed per job as the application (bot token, not
  a webhook), each with a "Tailor CV" button whose `custom_id` is
  `tailor:<first 32 hex chars of sha256(job_key)>` (`job_archive.job_card_payload`).
- Posting is idempotent: a `delivery_claims` row per `job_key` with a lease
  means a retried run never double-posts (`worker/src/index.js:195-243`).
- Only jobs the Worker confirms as `posted` or `existing` are recorded in
  `seen.json`, so an undelivered job is retried the next day (`digest.py:1117`,
  `:1132-1134`).
- If the Worker is unreachable, the run falls back to the plain
  `DISCORD_WEBHOOK_URL` and posts the old-style text digest
  (`digest.py:1122-1128`).

**On-demand tailoring** (`worker/src/index.js`, `.github/workflows/interactive.yml`,
`interactive_runner.py`):

- Clicking "Tailor CV" sends a signed Ed25519 interaction to the Worker's
  `/interactions` route. The Worker verifies the signature, checks the guild,
  channel and user against its configured ids, and rejects anything else.
- It looks up the immutable job snapshot stored against that exact Discord
  message id, verifies the JD text still matches its recorded SHA-256, writes a
  task row to D1 with the interaction token encrypted, and triggers the
  `interactive.yml` workflow with the task id.
- `interactive_runner.py` fetches the task over HMAC-authenticated HTTP,
  re-verifies the JD hash and completeness, produces the PDF, and POSTs the
  result back. The Worker attaches it to the original interaction as an
  ephemeral reply (`flags: 64`), visible only to the requester.
- Nothing tailored is ever committed to the repository. That is a stated
  constraint of the design.

**Job snapshot archive** (`job_archive.write_snapshots`, `digest.py:1077-1078`):

- Every delivered job's full evidence — title, company, location, URL, score,
  tier, link status, and the complete JD text with its SHA-256 — is written to
  `jobs/<date>.json` and committed back by the workflow. Same-day re-runs append
  new jobs and never rewrite existing ones.
- The archive is what lets a button clicked days later tailor against the
  posting as it was captured, not as it reads today.

**Secrets and configuration**:

- Repository secrets used by `.github/workflows/digest.yml`:
  `DISCORD_WEBHOOK_URL` (fallback delivery), `INTERACTIVE_WORKER_URL`,
  `WORKER_CALLBACK_SECRET`, `RAPIDAPI_KEY` (optional, JSearch),
  `GEMINI_API_KEY` (optional, ranking).
- Repository secrets used by `.github/workflows/interactive.yml`:
  `INTERACTIVE_WORKER_URL`, `WORKER_CALLBACK_SECRET`.
- Worker configuration: non-secret values live in `worker/wrangler.jsonc` under
  `vars` (the Discord application, guild, channel and user ids, the GitHub
  repository, and the Discord **public** key, which is public by design).
  Secret values are set with `wrangler secret put` and are **not** in the repo:
  `DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `CALLBACK_HMAC_SECRET`,
  `TASK_ENCRYPTION_KEY`.
- **Never write a secret value into the README.** Name the secret and where it
  is set; nothing more.

**Dependencies** (`requirements.txt`): `requests>=2.31`, `pypdf>=5.0`,
`reportlab>=4.2`. The Worker has no dependencies (`worker/package.json` is
`{"private": true, "type": "module", "scripts": {"test": "node --test"}}`).

Repo conventions that apply here:

- The README's voice is direct, second person, and explains *why* a decision was
  made, often with the date it was verified ("Verified live (24 Jun 2026)").
  Match it. It is not a generic project README and should not become one.
- It records rejected options so they are not re-litigated ("Ruled out — don't
  re-litigate"). Preserve every such section verbatim: the SmartRecruiters
  finding, the Aramco "greenhouse gas" false positive, the stc/NEOM notes.
  These are the most valuable paragraphs in the file.
- Code identifiers are in backticks; file paths are in backticks.

## Commands you will need

| Purpose        | Command                                                   | Expected on success        |
|----------------|-----------------------------------------------------------|----------------------------|
| Python tests   | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0               |
| Worker tests   | `cd worker && node --test`                                | 0 fail                     |
| List real files| `git ls-files`                                            | the authoritative file list|

Use `git ls-files` to build the Files section — do not write it from memory or
from this plan.

## Scope

**In scope** (the only file you should modify):

- `README.md`

**Out of scope** (do NOT touch):

- Every source file. This is a documentation change only. If you find a
  discrepancy where the code is wrong rather than the docs, write it in your
  report — do not fix it here.
- `docs/superpowers/**` — those are design records of what was decided at a
  point in time. They are not maintained documents and must not be edited to
  match today's code.
- `JOBS.md` — a dated snapshot of verified job listings from 2026-07-23, not a
  living document.
- Do **not** document the work in progress on `codex/discord-tailoring`
  (`resume_tailor.py`, `resume_render.py`, Gemini-backed tailoring). It is not
  merged. Describe `interactive_runner.py` as it exists on `main`.

## Git workflow

- Branch: `advisor/008-readme-sync`
- Commit style: Conventional Commits.
  Suggested message: `docs: describe the Worker delivery and on-demand tailoring`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Inventory what actually exists

```bash
git ls-files
cat requirements.txt
cat .github/workflows/digest.yml
cat .github/workflows/interactive.yml
```

Write down the real file list and the real test commands. Everything you
document must trace to output you saw here.

**Verify**: you have a list of every tracked file, and you can name which of
them the README currently omits.

### Step 2: Replace the `## Tailored resumes` section

Replace lines 89–100 entirely with a section titled `## Tailoring, on demand`
that describes the button flow using the facts in "Current state". It must
cover: the button on each card, that only the requester can use it, that the
snapshot's JD hash is re-verified before anything is generated, that the result
is an ephemeral Discord reply, and that nothing is committed to the repository.

Keep the paragraph about `resume.md` being the master and staying in sync with
the Cowork master resume — that is still true and still matters.

Remove the two-model justification (`RANK_MODEL` / `TAILOR_MODEL`) from this
section. Ranking still uses `gemini-3.1-flash-lite`; say so once, in the
`## How it ranks` section where it belongs.

**Verify**: `grep -n "TAILOR_MAX\|tailored/<date>\|tailored resume:" README.md`
→ no matches.

### Step 3: Rewrite `## Delivery`

Replace lines 102–108. The new section must say, in this order:

1. The primary path: application-owned embed cards posted by the Worker, one
   per job, each with a Tailor CV button.
2. Idempotency: `delivery_claims` in D1 means a retried run never double-posts,
   and only confirmed deliveries are written to `seen.json`.
3. The fallback: if the Worker is unreachable the run posts the plain text
   digest through `DISCORD_WEBHOOK_URL`, chunked under Discord's 2000-character
   limit.
4. Link verification — keep the existing paragraph on inconclusive links
   verbatim; it is still accurate.
5. Keep the pointer to `docs/weekly-job-review-template.md` verbatim.

If plan 002 has landed, also state that a run which delivers nothing exits
non-zero. If it has not, do **not** claim that — check `digest.py` for
`delivery_failed` before writing the sentence.

**Verify**: `grep -n "delivery_claims" README.md` → at least 1 match.

### Step 4: Rewrite `## Files`

Rebuild the list from `git ls-files`. Every entry gets one line explaining its
role. At minimum it must include:

- `digest.py`, `resume.md`, `profile.json`, `requirements.txt`
- `discord_delivery.py`, `job_archive.py`, `interactive_runner.py`,
  `resume_source.py`, `resume_ingest.py`
- `worker/src/index.js`, `worker/schema.sql`, `worker/wrangler.jsonc`,
  `worker/test/interaction.test.js`
- `jobs/`, `seen.json`, `stats.json`, `tailored/` (noting it is historical)
- `test_digest.py`, `test_interactive.py`
- `.github/workflows/digest.yml`, `.github/workflows/interactive.yml`
- `docs/weekly-job-review-template.md`, `docs/superpowers/`

Correct the `requirements.txt` line to name all three packages and say what
each is for (`requests` for every HTTP call, `pypdf` and `reportlab` for the
tailored PDF).

**Verify**: for each path you list, `test -e <path>` succeeds. Confirm at least
one that you might have guessed at, e.g.
`test -e worker/test/interaction.test.js && echo ok`.

### Step 5: Rewrite `## Setup` and `## Test before you trust it`

Setup must cover both halves of the system:

1. The repository half: fork/clone, add the repository secrets listed in
   "Current state" — naming each and what happens without it (the digest runs
   ATS-only without `RAPIDAPI_KEY`, keyword-scores without `GEMINI_API_KEY`,
   and falls back to the webhook without the Worker secrets).
2. The Worker half: a Discord application with an interactions endpoint URL
   pointing at the Worker, a D1 database created from `worker/schema.sql`, the
   `vars` in `worker/wrangler.jsonc`, and the four secrets set via
   `wrangler secret put`. Name them; give no values.

For "Test before you trust it", replace the first bullet with the two real
commands:

```
python -m unittest test_digest.py test_interactive.py
cd worker && node --test
```

and note that CI runs both before it calls any live API.

**Verify**: `grep -n "python test_digest.py" README.md` → no matches.
Then actually run both commands and confirm they behave as documented.

### Step 6: Correct `## What's deliberately simple`

Line 139 currently opens "No database." That is no longer true. Rewrite the
paragraph to say what is still deliberately simple — no scraper, no browser
automation, no queue, no framework, one Python file for the pipeline, five ATS
readers only because real companies forced them — and describe D1 honestly: a
small amount of coordination state for the interaction flow, with the daily
job's own state still being JSON files committed by the Action.

**Verify**: `grep -n "No database" README.md` → no matches.

### Step 7: Final consistency pass

Read the whole file top to bottom. Check that:

- The intro (lines 1–5) still describes the system accurately.
- `## The source decision`, `## Verified live`, `## Coverage`,
  `## Ruled out — don't re-litigate`, `## Adding a company`, `## How it filters`
  are **unchanged** — they are still correct and are the file's most valuable
  content.
- No section contradicts another.
- No secret value appears anywhere.

**Verify**: `git diff README.md` → the diff touches only the sections named in
steps 2–6. If `## How it filters` or `## Ruled out` appear in the diff, revert
those hunks.

## Test plan

No code tests. Verification is by grep and by running the documented commands:

- Every command the README tells the reader to run must be executed once and
  produce the documented result.
- Every file path the README mentions must exist (`test -e`).
- No stale identifier survives:
  `grep -n "TAILOR_MAX\|TAILOR_MODEL\|python test_digest.py\|No database" README.md`
  → no matches.

## Done criteria

ALL must hold:

- [ ] `grep -c "TAILOR_MAX\|TAILOR_MODEL\|python test_digest.py\|No database" README.md` returns `0`
- [ ] `grep -c "interactive_runner.py" README.md` returns 1 or more
- [ ] `grep -c "worker/" README.md` returns 1 or more
- [ ] `grep -c "jobs/" README.md` returns 1 or more
- [ ] `grep -c "pypdf" README.md` returns 1 or more
- [ ] Every path named in the Files section exists (`test -e` for each)
- [ ] `python -m unittest test_digest.py test_interactive.py` → `OK` (documented
      command actually works)
- [ ] `cd worker && node --test` → 0 fail (documented command actually works)
- [ ] `git status --porcelain` lists only `README.md`
- [ ] No secret value appears in the diff — secret **names** only
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `README.md` does not match the excerpts in "Current state" — someone has
  already started this.
- You cannot determine from the code what a component actually does, and would
  have to guess. Documenting a guess is worse than leaving the section marked
  as needing input. Report the specific question instead.
- You find the code contradicts a design document in `docs/superpowers/`. Report
  the contradiction; do not silently pick a side in the README.
- You are about to write any credential value, token, webhook URL, or database
  id that is not already in a tracked file. The Discord application/guild/
  channel/user ids and the D1 database id in `worker/wrangler.jsonc` are already
  committed; you still do not need to repeat them in the README.

## Maintenance notes

- The README is now the only place describing the two-workflow, two-runtime
  architecture. If the Worker gains a route or the interaction flow changes,
  this file is part of that change, not a follow-up.
- Consider whether a `CLAUDE.md` would carry the build/test/lint commands and
  conventions better than the README does — agents read it automatically, and it
  would keep the README aimed at a human reader. Deliberately not created here:
  it is a new convention for this repository and the operator should choose it.
- The work on `codex/discord-tailoring` will change what `interactive_runner.py`
  produces (Gemini-backed tailoring with evidence validation instead of the
  current PDF path). When that merges, the `## Tailoring, on demand` section
  needs one more pass.
- Reviewer should scrutinize: that `## Ruled out — don't re-litigate` survived
  untouched, that no documented command was written without being run, and that
  no secret values appear.
