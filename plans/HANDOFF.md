# Handoff

**Written**: 2026-08-12
**Branch state at handoff**: local `main` = `f57d169`, 28 commits ahead of `origin/main` (`f624299`)
**Verified green at that commit**: `python -m unittest test_digest.py test_interactive.py` → 70 tests; `cd worker && node --test` → 24 tests

You are picking up a codebase that just had an 11-plan audit-and-fix pass
executed against it. Everything described as DONE below was reviewed by
re-running its acceptance criteria and reading its diff — not by trusting an
executor's self-report. Read this file before touching anything; several of the
traps below cost real time to find.

---

## 1. What this project is

A daily job digest for one person. GitHub Actions cron at 04:00 UTC (07:00 AST)
runs `digest.py`, which pulls postings from five ATS APIs plus an optional
aggregator, filters to Saudi senior-IC tech roles, scores them with Gemini,
archives full JD evidence, and delivers Discord cards through a Cloudflare
Worker. Clicking a card's "Tailor CV" button triggers a second workflow that
generates a tailored PDF against that job's captured JD.

Two runtimes, and this is the thing to internalise:

- **Python**, run by GitHub Actions (`digest.py` and its modules).
- **JavaScript**, run by Cloudflare (`worker/`), holding state in D1.

They talk over HMAC-signed HTTP. A change to `worker/` does **not** ship by
merging — see §3.

Authoritative docs: `README.md` (architecture, current as of `f57d169`),
`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md` (the design
this is implementing, 5 tasks, ~3 done), `docs/2026-08-10-*.md` (two design
spikes from this pass).

## 2. What was done in this pass

`plans/README.md` is the index and the detailed record — read it. Summary:

| Plan | What it fixed |
|---|---|
| 001 | `GEMINI_API_KEY` never reached the interactive workflow, so tailoring would have silently returned the untailored master resume forever |
| 002 | A run where no card reached Discord exited 0 and looked healthy; now exits non-zero |
| 003 | `jobs/<date>.json` grew unbounded and was committed daily; now prunes on the same 120-day TTL as `seen.json` |
| 004 | Only `internal_nonces` was ever cleaned in D1; four tables grew forever, including encrypted Discord tokens |
| 005 | `stats.json` appended a row per *run*, so re-running a day let it fill the drift detector's 7-day window |
| 006 | The Worker awaited a GitHub API round-trip before acking Discord, against a 3-second deadline |
| 007 | ~90 lines of superseded auto-tailoring code in `digest.py`, kept alive by tests |
| 008 | README described a pipeline that no longer existed and omitted the Worker entirely |
| 009 | SPIKE — `/resume` and `/tailor-jd` design (`docs/2026-08-10-resume-command-design.md`) |
| 010 | SPIKE — click-based calibration signal (`docs/2026-08-10-calibration-signal-design.md`) |
| 011 | Command registration had no source of truth anywhere in the repo |

Also merged in this pass: `codex/discord-tailoring`, which replaces the
placeholder keyword-intersection PDF with model-backed, evidence-validated
tailoring (`resume_tailor.py`, `resume_render.py`).

## 3. Nothing is live yet — three actions, in this order

**These are the operator's to run.** The work is merged locally and nothing
below has happened.

```bash
# 1. Push. 28 commits are local-only.
git push
```

```bash
# 2. Deploy the Worker. Plans 004, 006 and 011 changed worker/.
#    Merging does NOT ship them — Cloudflare still runs the old code.
cd worker && npx wrangler deploy
```

```bash
# 3. Register slash commands. ONLY after §4 is decided.
DISCORD_BOT_TOKEN=... npm run register --prefix worker
```

Step 3 uses `PUT`, which **replaces the entire command set**. Anything
registered by hand and not present in `worker/commands.js` is deleted. That is
intentional — it is what makes the repo authoritative — but know it before
running it.

## 4. Open decisions — do not decide these unilaterally

1. **Register `/tailor-jd`?** It is defined in `worker/commands.js` but its
   handler does not exist: `interactive_runner.run_task` accepts only
   `job_snapshot` tasks, so the command would appear in Discord and fail with
   "The task has no immutable job snapshot." Either comment it out of the array
   until its handler lands, or accept a visible-but-broken command. One-line
   change either way.
2. **Where does a confirmed resume get stored?** `docs/2026-08-10-resume-command-design.md`
   §Q3 lays out three options. The recommendation carried forward: write via D1
   (no repo write permission on a Discord-triggered surface), then have the
   *daily digest workflow* — which already has `contents: write` and already
   commits — sync it back to `resume.md` in its existing commit step. That gets
   D1's safety on the untrusted path and git's history on the durable one,
   without granting a new permission anywhere.
3. **Discord CDN attachment URLs expire at 24h** (signed `ex`/`is`/`hm`
   params). Fine for the normal path; fatal for a confirmation flow that parks
   an upload for hours. Design for downloading the bytes immediately rather
   than storing the URL. Not yet implemented anywhere.

## 5. Traps that cost time in this pass

**Agent worktrees may branch from `origin/main`, not local `main`.** This is
the big one. An executor produced a complete, self-consistent, all-checks-green
change built on a tree 23 commits stale. It would have deleted tests that only
existed on `main` and left `run_main` call sites passing three arguments to a
two-argument function. It was caught only because its report mentioned a
45-test baseline where `main` had 72.

If you dispatch subagents while local work is unpushed, make step zero of every
prompt: `git reset --hard main`, then verify concrete numbers (test counts,
presence of a known-recent file) before any work. Do not rely on the agent
noticing.

The rejected branch `advisor/007-remove-dead-tailoring` (commit `6a2b70f`)
still exists and **must not be merged**. It cannot be deleted while its
worktree holds it:

```bash
git worktree remove .claude/worktrees/agent-abf25663aa1a20d0a
git branch -D advisor/007-remove-dead-tailoring
```

**The cron pushes to `main` daily.** Mid-pass, `origin/main` moved under us
(`f624299`, a state commit). Local work that sits unpushed will keep diverging.
It only touches `seen.json`, `stats.json` and `jobs/`, so conflicts are
unlikely — but reconcile before assuming your base is current.

**`write_snapshots` prunes against wall-clock today, not its `date` argument.**
Harmless for the only production caller (always today's date), but a backfill
call with an old date deletes its own file on the next write. This broke a test
fixture and is documented in `plans/003-*.md`'s maintenance notes.

**Test counts moved a lot**: 45 → 72 (merges) → 70 (007 removed two tests for
dead code) → 70 Python / 24 Worker now. Never hardcode an expected count in a
plan; record the observed baseline and express criteria as a delta.

## 6. Known issues, not yet planned

- **`digest.yml`'s `git push` has no retry and no rebase** (`.github/workflows/digest.yml`,
  the "Persist state" step). It is also in a different `concurrency` group from
  `interactive.yml`. Today only one workflow commits, so it is latent. The
  moment anything else commits to the repo — including the `resume.md` sync in
  §4.2 — a non-fast-forward push silently drops that day's state update. Fix
  before adding a second committer. This is the highest-value unplanned item.
- **`/resume` and `/tailor-jd` are half-wired.** The Worker accepts both and
  dispatches the workflow; the runner rejects both. Design exists
  (`docs/2026-08-10-resume-command-design.md`), implementation does not. This is
  task 2 of the original 5-task design.
- **Only the score threshold gates the Strong tier.** Verified across 50
  archived jobs: every review-tier job was there for low score alone — zero for
  missing JD. Two of `split_tiers`' three conditions are currently inert.
  Scores are also quantized to multiples of 5 with a quarter of the sample at
  exactly 85, which caps how finely any future signal can discriminate.
- **`stats.json` has nonzero `tailored`/`tailor_failed` columns that no current
  code path can produce** — `digest.py` passes both as hardcoded `0`. Residue
  from the removed pipeline. Do not read them as data.

## 7. How to work in this repo

- **Verification**: `python -m unittest test_digest.py test_interactive.py` and
  `cd worker && node --test`. CI runs both before any live API call. There is no
  lint, formatter, or typecheck step.
- **No dependencies in `worker/`** — Node built-ins only, `node:test`, no
  lockfile. Python is three packages: `requests`, `pypdf`, `reportlab`.
- **Fail-soft is the governing instinct**: a failure costs the user the feature,
  never the run, and always says why. Match it.
- **Selection rules are never changed silently by the workflow.** Threshold and
  filter changes stay human decisions — stated in the README and honored by the
  calibration spike.
- Commits: Conventional Commits. Design records: `docs/YYYY-MM-DD-<slug>.md`.
- `plans/` holds the audit record: every plan, the index, findings considered
  and rejected (so they are not re-audited), and defects found in the plans
  themselves during execution. Read `plans/README.md` first.

## 8. If you are asked to continue the audit

Findings deliberately rejected — with reasons — are listed in
`plans/README.md` under "Findings considered and rejected". Do not re-surface
them without new evidence. The most useful next work, in order:

1. Fix `digest.yml`'s push (see §6) — small, and it blocks §4.2 safely.
2. Implement task 2 of the design doc (`/resume` end-to-end), using
   `docs/2026-08-10-resume-command-design.md` as the spec.
3. Watch one live run after deploy: 002 makes a total delivery failure red
   instead of silent, and 006 changes what a failed dispatch looks like to the
   user. Both are new behaviors worth seeing once before trusting.
