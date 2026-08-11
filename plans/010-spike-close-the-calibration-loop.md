# Plan 010 (SPIKE): Design a click-based calibration signal for the score threshold

> **Executor instructions**: This is a **design spike, not an implementation
> plan**. Its deliverable is a written design document and one offline analysis
> of data already in the repository — not a working feature. Do not build a
> data pipeline. If you find yourself modifying `digest.py` or
> `worker/src/index.js`, stop: you have left the scope of this plan.
>
> Follow the steps in order. Run every verification command. If anything in the
> "STOP conditions" section occurs, stop and report. When done, update the
> status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- digest.py worker/schema.sql worker/src/index.js docs/weekly-job-review-template.md`
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first.

## Status

- **Priority**: P3
- **Effort**: M (spike only)
- **Risk**: LOW (produces a document and an analysis; changes no behavior)
- **Depends on**: none, but **read the interaction with plan 004 below before starting**
- **Category**: direction
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

The digest sorts jobs into two tiers using one number:

```python
STRONG_MATCH_MIN_SCORE = 80
```

Strong matches are the ones the system says are worth acting on. Everything
else is "Worth reviewing". Whether 80 is the right line is currently answered by
hand: `docs/weekly-job-review-template.md` asks the operator to fill in a table
each week and then decide.

```markdown
## Calibration decision

- Keep or change `STRONG_MATCH_MIN_SCORE`:
- Filter/profile wording to revisit:
- Link hosts inaccessible from Actions but usable locally:
```

That manual loop was a deliberate choice — the README says the feedback is
"deliberately manual: it tells you when to tune the score threshold, profile
wording, or filters without allowing the workflow to change selection rules
silently." That principle should survive whatever this spike proposes.

What has changed since that template was written is that the system now
records a behavioral signal for free. Every "Tailor CV" click writes a row to
the D1 `tasks` table with the `job_key` of the job the user cared enough to act
on. And every delivered job's score and tier is already in `jobs/<date>.json`.
Joining the two answers a question the manual template can only estimate: at
what score does the operator actually start clicking?

The honest limitation, which must be stated in the design: a click measures
curiosity, not an application. It is a weaker signal than the operator's own
weekly note. It is also continuous, free, and unbiased by memory. The design
should treat it as an input to the manual decision, not a replacement for it.

**A concrete recent data point worth investigating**: `stats.json` records
`{"avg_score": 55.0, "date": "2026-08-07", "strong": 0, "review": 12}` — a full
day where twelve jobs matched every hard filter and not one cleared 80. Whether
that is the threshold doing its job or the threshold being wrong is exactly the
question this spike should be able to answer.

### Interaction with plan 004 — read before starting

Plan 004 adds retention to the Worker's D1 tables, including:

```
TASK_RETENTION_DAYS = 7
DELETE FROM tasks WHERE datetime(expires_at) < datetime('now', '-7 days')
```

If plan 004 lands, the click history this spike depends on survives about a
week. That is not an argument against plan 004 — `tasks` rows hold encrypted
Discord interaction tokens and should not be kept — it is a constraint on this
design: **the click signal needs its own durable, minimal record, not a
dependency on task rows sticking around.** A row of `(job_key, clicked_at)`
with no token and no user content is a very different retention question from a
row holding a credential. Design for that.

## Current state

The data that already exists:

- **`jobs/<date>.json`** — one entry per delivered job, written by
  `job_archive.make_snapshot`. Contains everything needed for the analysis:

```python
    return {
        "schema_version": 1,
        "job_key": job_key(job),
        "captured_at": captured_at,
        "source": job.get("source", ""),
        "source_id": job.get("id", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "salary": job.get("salary", ""),
        "tier": job.get("tier", "Worth reviewing"),
        "score": job.get("score", 0),
        "why": job.get("why", ""),
        "ranking_verified": bool(job.get("ranking_verified")),
        "link_status": job.get("link_status", "unknown"),
        "jd": { ... },
    }
```

  Two files exist today: `jobs/2026-08-08.json` and `jobs/2026-08-09.json`.

- **D1 `tasks`** (`worker/schema.sql`) — one row per interaction, including
  every button click:

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  user_id TEXT NOT NULL,
  job_key TEXT,
  input_json TEXT NOT NULL,
  interaction_token TEXT NOT NULL,
  status TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  delivery_lease_until INTEGER,
  delivery_error TEXT
);
```

  `job_key` is set for button tasks at `worker/src/index.js:278`
  (`task.jobKey = snapshot.job_key;`), so the join key to `jobs/*.json` already
  matches exactly.

- **`stats.json`** — daily aggregates including `strong` and `review` counts
  and `avg_score`. 17 rows today.

- **`docs/weekly-job-review-template.md`** — the manual loop, 20 lines.

What does **not** exist:

- Any route on the Worker that reads data back out. Every `/internal/` route
  either writes or fetches a single task by id. There is no query interface, so
  the daily digest run cannot currently see click data at all.
- Any durable record of a click that outlives the task row.
- Any code in `digest.py` that reads `jobs/*.json` back. The archive is
  write-only today.

Repo conventions that apply here:

- Design records go in `docs/`, named `YYYY-MM-DD-<slug>.md`.
  `docs/2026-07-31-ai-agent-plan.md` is a good model for a proposal document:
  it opens with what the system's real failure mode is, recommends one approach,
  and says explicitly why not the alternatives.
- The project refuses complexity by default: "No database. State is one JSON
  file committed back by the Action. No scraper, no browser automation, no
  queue." Any proposal that adds a service has to earn it in the document.
- Selection rules are never changed silently by the workflow. Whatever this
  spike proposes must keep the human in the loop for threshold changes.

## Commands you will need

| Purpose        | Command                                                   | Expected on success        |
|----------------|-----------------------------------------------------------|----------------------------|
| Python tests   | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0               |
| Worker tests   | `cd worker && node --test`                                | 0 fail                     |
| Read archives  | `python <throwaway script in a scratch dir>`               | as described in step 2     |

## Scope

**In scope**:

- `docs/<YYYY-MM-DD>-calibration-signal-design.md` (create) — the deliverable.
- Throwaway analysis scripts written **outside the repository** in a temp
  directory. Not committed.

**Out of scope** (do NOT modify):

- `digest.py`, `worker/src/index.js`, `worker/schema.sql`,
  `job_archive.py` — every production file. This spike changes no behavior.
- `STRONG_MATCH_MIN_SCORE`. Do not tune it. Recommending a value with evidence
  is in scope; changing it is the operator's call.
- `docs/weekly-job-review-template.md` — do not replace the manual loop as part
  of this spike. Proposing how it and the new signal fit together is in scope.
- The live D1 database. Do not connect to it, query it, or modify it.
- `profile.json` and the ranking rubric in `gemini_rank`. Out of scope even
  though they are the other half of why scores are what they are.

## Git workflow

- Branch: `advisor/010-spike-calibration-signal`
- Commit style: Conventional Commits.
  Suggested message: `docs: design a click-based calibration signal`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Establish what the archive can already tell you

Read `jobs/2026-08-08.json` and `jobs/2026-08-09.json` and `stats.json`. Write
down, from the data rather than from this plan:

- How many jobs were delivered per day, and the score distribution.
- How many landed in each tier, and how many of the "Worth reviewing" ones were
  there because of a low score versus a missing JD versus an unverified
  ranking. `split_tiers` in `digest.py:732-741` is the exact rule:

```python
    strong = [
        job for job in jobs
        if job["ranking_verified"]
        and job["score"] >= STRONG_MATCH_MIN_SCORE
        and bool(job.get("description"))
    ]
```

  Three separate reasons collapse into one tier. Knowing which reason dominates
  matters more than the threshold does — if most review-tier jobs are there for
  lack of a JD, moving the threshold changes nothing.

**Verify**: you can state, with numbers, the reason breakdown for the review
tier across the available days.

### Step 2: Analyse the score distribution offline

In a temp directory outside the repository, write a script that loads every
`jobs/*.json`, and produces:

- The score histogram across all delivered jobs.
- For each candidate threshold from 60 to 90 in steps of 5: how many jobs would
  be Strong, and how many of those would have been blocked by the other two
  conditions anyway.
- The same breakdown for 2026-08-07's zero-strong day if the data reaches back
  that far. (`stats.json` has the aggregate; `jobs/` may not have the file. Say
  which.)

Two days of archive is a very small sample. Say so explicitly in the write-up —
an honest "we cannot yet answer this from the data" is a valid and useful
spike result, and it tells the operator to let the archive accumulate.

**Verify**: the numbers are in your notes, and you have written one sentence on
whether the sample is large enough to support any recommendation.

### Step 3: Design the click record

Specify the minimal durable record of a click. Answer:

1. **What is stored.** Recommend the smallest useful row — `job_key`, a
   timestamp, and nothing else. Argue explicitly against storing the user id
   (there is exactly one authorized user, so it carries no information) and
   against storing anything from the interaction token.
2. **Where it is stored.** Options: a new D1 table with its own long retention;
   an append-only file in the repository like `jobs/`; or a counter folded into
   the existing snapshot archive. State the trade-offs. Note that the
   repository is private, and that a file-based option needs the interactive
   workflow to gain write access — which plan 009 flags as a permission
   escalation on a Discord-triggered workflow. That argues for D1.
3. **How the digest run reads it.** There is no read route on the Worker today.
   Sketch the smallest one that would work, reusing the existing HMAC
   authentication (`internalRequestIsValid` at `worker/src/index.js:129-140`) —
   do not invent a new auth scheme.
4. **Retention.** Reconcile with plan 004. A click row with no credential in it
   can be kept far longer than a task row. Name the window and justify it.
5. **What is reported and when.** Recommend the smallest surface: most likely
   one line appended to the weekly review or the daily digest, e.g. "clicked 3
   of 12 delivered in the last 7 days; median clicked score 84, median unclicked
   72." Not a dashboard.

**Verify**: each of the five questions has a recommendation with reasoning.

### Step 4: Address the honest objections

The document must engage with these, not skip them:

- **A click is not an application.** The signal measures interest, and interest
  is cheap. Say what it can and cannot support.
- **Sample size.** One user, roughly 10 delivered jobs a day, and a click rate
  that may be very low. Estimate how long until the signal means anything, and
  recommend not acting on it before then.
- **Feedback loop risk.** If the threshold moves toward what gets clicked, the
  system narrows toward what it already surfaces. Say how to avoid that —
  probably: never let the workflow change the threshold, only report the
  evidence. This is the README's stated principle and the strongest reason to
  keep the human in the loop.
- **The manual template already exists.** Explain how the two coexist. The
  honest answer may be that the click signal is a prompt to fill in the manual
  template, not a substitute for it.

**Verify**: all four objections are addressed in the document by name.

### Step 5: Write the design document

Create `docs/<today>-calibration-signal-design.md`, modelled on
`docs/2026-07-31-ai-agent-plan.md`: open with the real problem, recommend one
approach, say why not the alternatives, and scope it.

It must contain: the step 1 reason-breakdown, the step 2 distribution analysis
with an explicit statement about sample size, the step 3 design decisions, the
step 4 objections, and a "what this does not do" section.

**Verify**: `test -e docs/<today>-calibration-signal-design.md`.

### Step 6: Confirm nothing changed

**Verify**:
- `python -m unittest test_digest.py test_interactive.py` → `OK`, count unchanged
- `cd worker && node --test` → 0 fail, count unchanged
- `git status --porcelain` → exactly one new file, the design document

## Test plan

No code tests — this spike ships a document and an analysis. The test plan for
any resulting implementation is part of the deliverable.

Verification: both suites still pass unchanged, and the document exists and
addresses every question in steps 3 and 4.

## Done criteria

ALL must hold:

- [ ] `docs/<YYYY-MM-DD>-calibration-signal-design.md` exists
- [ ] It answers all five questions from step 3 and addresses all four
      objections from step 4 by name
- [ ] It states explicitly whether the current sample is large enough to
      recommend a threshold change
- [ ] It states a retention window for click records and reconciles it with
      plan 004's `TASK_RETENTION_DAYS`
- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with its
      count unchanged (this plan adds no tests)
- [ ] `cd worker && node --test` exits 0 with 0 fail and its count unchanged
- [ ] `git status --porcelain` lists exactly one added file and no modifications
- [ ] `STRONG_MATCH_MIN_SCORE` in `digest.py` is unchanged
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- You are about to modify any production file, including changing
  `STRONG_MATCH_MIN_SCORE`.
- You are about to connect to or query the live D1 database.
- The analysis in step 2 has too little data to say anything. That is a
  legitimate result — write it up and stop, rather than manufacturing a
  recommendation from two days of archive.
- The design starts to require a dashboard, a metrics service, or a scheduled
  job beyond the daily digest. The project's stated principle is deliberate
  simplicity; a proposal that needs infrastructure has to justify it in the
  document or be cut.
- You conclude the right answer is "do not build this". Say so, with reasoning.
  A spike that recommends against its own subject is a successful spike.

## Maintenance notes

- The join key between the two data sources is `job_key`
  (`"{source}:{id}"`, from `job_archive.job_key`). It is used by the D1
  `tasks`, `deliveries`, `delivery_claims` and `job_snapshots` tables and by
  the `jobs/*.json` archive. Anything that changes its format — for example
  hashing the very long JSearch ids, which plan 003 notes as a possible future
  change — invalidates every historical join. If both changes are ever made,
  do them in the same commit and note the discontinuity.
- Plan 003 prunes `jobs/*.json` after 120 days. That bounds how far back any
  retrospective analysis can ever reach. If the click signal is meant to
  support a longer view, the aggregate belongs in `stats.json`, which is
  pruned on the same window but is two orders of magnitude smaller per day.
- `docs/weekly-job-review-template.md` remains the authoritative feedback
  mechanism until the operator decides otherwise. Do not deprecate it in this
  document.
- Reviewer should read the document for one thing above all: whether the
  proposal preserves the rule that the workflow never changes selection
  criteria on its own.
