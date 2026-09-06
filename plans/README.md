# Implementation Plans

This directory is the standing audit record for this repository: every plan,
the index, findings deliberately rejected (so they are not re-audited), and
defects found in the plans themselves during execution.

**Four passes are recorded here.** Plan 018 is the current pass. Plan 017 is the
pass before it, plans 012–016 the one before that, and plans 001–011 the first;
their records are preserved below.

---

## Current pass — 2026-09-06 (018)

**Written against `main` at commit `01c9dc3`.** Opened by the operator: the
digest delivered a role at **fit 85** that Watheq's match feature scored **58**
with a low-ceiling verdict. This pass ports the structure that makes Watheq's
verdict honest.

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| [018](018-ground-the-fit-score-in-resume-evidence.md) | Ground the fit score in resume evidence | P1 | M + M | — | **018A DONE**, committed `43cc4ce`. **018B BLOCKED on the gate** — see below |

### The diagnosis, verified in code rather than asserted

Three structural causes, none of them a matter of prompt wording:

1. **The ranker scored a self-marketing blurb, not a resume.** `rank_jobs` sent
   `PROFILE["blurb"]` and never sent `resume.md`, which is read only on the
   Tailor-CV path. Watheq's own anti-inflation clause
   (`contracts/index.js:403`) names the failure: a term whose only appearance is
   a summary or a skills list is an unproven claim. The blurb **is** a summary.
2. **The rubric had no band for experience or education.** Watheq weights those
   at 30 + 15 of 100. This prompt had no year count and no credential check
   anywhere.
3. **No evidence gate, no blockers, no ceiling.** `apply_ranking` validates
   `why` for non-emptiness and nothing else. Watheq's low-ceiling copy comes
   from `confirmedRisks.length >= 2 || riskTier === 'critical'` — never from the
   score, which is why a 58 alone cannot produce it.

**The grounding case makes cause 2 concrete — but it is a stand-in.** The role
Watheq actually scored 58 has not been identified; the operator holds that
output and it names the blockers. Ask for it, and pass `--company` to the
harness if it is a different role. Until then the stand-in is
`jobs/2026-09-03.json` → OmniOps,
"Senior Solution Engineer", archived at **fit 85, `ranking_verified: true`**,
5,492-character `grade=full` JD. The 2,000-character excerpt the ranker actually
reads *opens* with a required Bachelor's in CS/IT/Software Engineering and 5+
years of Solution Engineering / Presales / Solutions Architecture. `resume.md`
shows a pipefitting diploma and none of those functions. The old rubric had
nowhere to put either fact and scored it 85.

### GATE — 018B is deliberately not started

018A is committed. **018B must not be built until 018A's effect is measured**,
because 018A is what tests the pass's premise. If reading the resume does not
move the grounding case, the premise is wrong and a verdict layer would be built
on sand.

`scripts/measure_ranker.py` is the harness. It scores one archived job under
both the old and new prompts in one process, and reports whether the shift
clears run-to-run noise. It needs a live `OPENROUTER_API_KEY`, which exists only
as a GitHub Actions secret — **the operator has to run it:**

```
OPENROUTER_API_KEY=sk-... python scripts/measure_ranker.py --runs 3
```

Judge the reasons before the number: `chat_json` sends `seed=7` and no
temperature, so a single run proves nothing, and a shift inside the spread is
not a result. Record the outcome here before starting 018B.

### Decisions taken by the operator for 018B

| Question | Decision |
|---|---|
| Score effect of blockers | Cap the score **and** add the verdict, following `cap_teaser_scores`. |
| Which jobs get the per-job pass | Strong tier only (`ranking_verified`, score >= 80, has a JD). ~3–6/day. |
| Capped score below the floor | Deliver it, flagged. Never dropped, never `remember()`ed. |
| Gold-set evals | Deferred. **Risk this leaves: nothing detects inflation drift automatically.** |

### Expected one-off drift warning

018A lowers scores broadly, and `check_drift` warns on a >15-point drop across a
7-day window. It will fire once. **Do not widen `DRIFT_SCORE_DROP_THRESHOLD`** —
that disables the detector permanently to silence one expected event.

---

## Pass 2026-09-06 (017)

**Written against `main` at commit `ab0eb45`.** Opened by the monthly job-fit
profile refresh: the profile was re-read, and reading it surfaced a defect in
the code that consumes it.

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| [017](017-match-a-skill-keyword-at-a-word-start.md) | Match a skill keyword at a word start, not anywhere in the title | P3 | S | — | DONE — committed `01c9dc3` on `fix/keyword-word-start` |

Measured, not asserted: 6 of 91 archived titles carry a spurious keyword hit
today, and the both-boundaries fix would destroy 2 legitimate ones. The plan
carries the reproduction script and the full before/after table.

Scope note: `keyword_score` only runs when the model ranker fails, and
`drop_below_floor` never drops on an unverified score, so this changes ordering
in a fallback digest and nothing else. P3 for that reason.

### Execution log — 2026-09-06

Executed directly on branch `fix/keyword-word-start` (not in a worktree), two
commits: `267f750` commits the profile refresh on its own, `01c9dc3` is the fix.

**Baseline observed at execution time: 272 Python tests (1 skipped), 44 Worker.**
Not the 212/44 recorded for the previous pass — the tree moved. Final: **280
Python, 44 Worker, all passing.** `git diff --numstat test_digest.py` reports
**0 deletions**, so no pre-existing test was weakened to accommodate the change.

Three corrections were made to plan 017 during execution:

- **The profile refresh had to be committed first.** `profile.json` was
  uncommitted and held 37 keywords where the committed file held 33. The plan's
  six-row evidence table assumes 37, and its STOP condition fires on any other
  output — so a fresh worktree would have checked out the 33-keyword file and
  stopped spuriously. This is the same class of failure that cost the
  2026-08-09 pass a full executor run (see the rejection note below).
- **Task 1's "patch `PROFILE`" is not how this suite works.** Nothing patches
  that global; tests read the real `profile.json` through a cwd swap performed
  before import (`test_digest.py:30-40`), and `PROFILE = load_profile()` runs at
  import. The matcher was split into `keyword_hits(title, keywords)` taking an
  explicit list, which is what makes the tests parameterisable.
- **Tasks 2 and 3 conflicted as written.** "Compile once at import" and "test
  against `DEFAULT_PROFILE`" only both hold if the matcher accepts a keyword
  list. `functools.lru_cache` on `_keyword_pattern` gives compile-once without a
  module-level table, and `keyword_hits` taking a list satisfies Task 3.

**The regression guard was verified to discriminate**, not assumed: adding a
trailing `(?![a-z0-9])` to `_keyword_pattern` fails exactly 2 tests — `AI
Engineer - Agentic` and the `Datacentre` case — and removing it returns the
suite to green. All six affected titles were also re-scored through the live
`keyword_score` and match the plan's predicted "after" column exactly.

---

## Pass 2026-08-30 — audit (012–016)

**Written against `main` at commit `23de025`.** Each plan stamps that SHA and
opens with a drift check.

### Verification baseline (verified green at `23de025`)

| Purpose      | Command                                                                        | Observed at `23de025` |
|--------------|--------------------------------------------------------------------------------|-----------------------|
| Install      | `python -m pip install --require-hashes -r requirements.txt`                    | exit 0                |
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`  | `OK`, 199 tests (1 skipped) |
| Worker tests | `cd worker && npm test`                                                        | 43 pass, 0 fail       |

Both suites run in CI (`.github/workflows/ci.yml`) and as a preflight job in
`digest.yml` and `interactive.yml`, before any live API call. `actionlint` lints
the workflows. There is still no lint, formatter or typecheck step for the
application code.

**Do not hardcode 199 or 43 in anything.** Every plan adds tests, and 013 adds
one to each suite:

| Plan | Python tests | Worker tests |
|------|--------------|--------------|
| 012  | +1           | —            |
| 013  | +1           | +1           |
| 014  | +5           | —            |
| 015  | +1           | —            |
| 016  | +5           | —            |

Every plan opens by recording the counts it actually observes and expresses its
done criteria as a delta from those.

### Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| [012](012-record-webhook-fallback-delivery.md) | Record a webhook-fallback delivery so a Worker outage stops re-posting the same digest daily | P1 | S | — | DONE — reviewed, **APPROVED**, committed `cff3770` **merged to main** |
| [013](013-stop-dispatching-a-resume-command-nothing-can-serve.md) | Stop dispatching a `/resume` nothing can serve, and guard the Worker→runner task contract | P1 | S–M | — | DONE — reviewed, **APPROVED**, committed `d9ae0c4` **merged to main**; needs Worker deploy |
| [014](014-hold-fit-notes-to-the-same-evidence-check-as-gaps.md) | Hold "Why you fit" notes to the same evidence check the gaps already pass | P2 | S | — | DONE — reviewed, **APPROVED**, committed `2666af1` **merged to main** |
| [015](015-sign-worker-requests-in-one-place.md) | Sign every Worker request through the one shared client | P2 | S | — | DONE — reviewed, **APPROVED**, committed `4849455`+`3c5275f` **merged to main** |
| [016](016-cap-the-bytes-a-third-party-page-may-spend.md) | Cap the bytes a third-party page may spend on the daily run | P3 | M | — | DONE — reviewed, **APPROVED**, committed `ff6a153`+`1d88f2a` **merged to main** |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (with one-line reason) |
REJECTED (with one-line rationale).

This selection was made non-interactively — the operator was not available to
choose — using the skill's default: the top five findings by leverage. The
findings that were **not** planned are listed below with the reason, so the
choice is reviewable.

### Execution log — 2026-08-31

All five plans executed by dispatched subagents in isolated worktrees, each
diff reviewed by re-running its done criteria rather than trusting the
executor's report. **All five APPROVED. None merged, none pushed — that is the
operator's call.**

| Plan | Branch | Commit(s) | Worktree |
|------|--------|-----------|----------|
| 012 | `advisor/012-record-webhook-fallback-delivery` | `cff3770` | `.claude/worktrees/agent-a5d80e8810834e186` |
| 013 | `worktree-agent-a41f05b27130ab990` | `d9ae0c4` | `.claude/worktrees/agent-a41f05b27130ab990` |
| 014 | `advisor/014-validate-fit-notes` | `2666af1` | `.claude/worktrees/agent-ae25abae24d3d2e78` |
| 015 | `advisor/015-share-signed-request` | `4849455`, `3c5275f` | `.claude/worktrees/agent-ad7e7bd032334990f` |
| 016 | `advisor/016-cap-page-bytes` | `ff6a153`, `1d88f2a` | `.claude/worktrees/agent-ac165cba34e201088` |

Every branch is based on `db71bee` (`origin/main`, one `stats.json`-only commit
past the `23de025` the plans were stamped against). No source drift.

### Test counts per branch (baseline 199 Python / 43 Worker)

012 → 200 py. 013 → 200 py, 44 worker. 014 → 204 py. 015 → 200 py. 016 → 204 py.
Worker suite unchanged at 43 on every branch except 013.

**These counts do not add up across branches.** 014 and 016 both report 204
because each adds 5 to the same 199 baseline, in different files. Merging is
where they compose; expect roughly 199 + 1 + 1 + 5 + 1 + 5 = 212 Python tests
once all five land, but verify rather than trusting that arithmetic.

### Reviewer verification beyond re-running the criteria

- **012** — restored the `if not worker_url:` guard and re-ran the new test: it
  FAILED with `AssertionError: 0 not greater than or equal to 2` (the empty
  `seen.json`), then passed once restored. The test bites.
- **014** — ran `clean_fit_notes` against the real `resume.md` with four
  realistic notes. Both supported notes survived (including one naming the
  target role and employer, the case a resume-only vocabulary would wrongly
  drop); the fabricated "Kubernetes and Terraform" claim and the inflated "93%"
  were both dropped. This answers the executor's own flagged caveat that
  nothing asserts "Why you fit" survives end to end.
- **015** — restored the pre-migration `discord_delivery.py` from `23de025` and
  ran the new test: FAILED on the signature check, confirming it discriminates.
  Worker suite stayed at 43, proving the canonical HMAC string did not move.
- **016** — confirmed `test_digest.py` has **0 deletions** against the base, so
  no pre-existing test was weakened to accommodate the wrapper.

### Deviations accepted on merit

- **012** — the plan told the executor to raise unconditionally from the mocked
  `post_interactive_cards`. That crashes day 2: with the fix working, day 2 has
  no fresh jobs, so `main()` takes the quiet-day path into `post_heartbeat`,
  which calls `post_interactive_cards` with no try/except. The executor made
  the mock raise only for non-heartbeat calls and commented why. This isolates
  the regression test from the heartbeat asymmetry the plan itself lists as out
  of scope. Correct call — the plan was wrong, not the executor.
- **013** — committed on the worktree's own branch rather than
  `advisor/013-resume-command-honesty`, following a reviewer instruction after
  the rate-limit interruption. Cosmetic.
- **016** — folded the step-4 and step-5 tests into one `TestBoundedFetch` class
  instead of two. The plan only said "next to `TestJdFetch`". Fine.

### Defects found in these plans by their executors

- **014's done criteria** claimed `grep -c "vocabulary(" resume_tailor.py` counts
  "the import, `clean_gaps`, and `tailor`". The import line has no parentheses.
  The real 3 are `clean_gaps` and `tailor`'s two calls (`allowed` and
  `headline_terms`). The expected value of 3 is right; the reasoning was wrong,
  and a 4 would still correctly flag a recomputed set.
- **015's step 3** as written produced a vacuous test. Posting to a bare origin
  makes the hardcoded `/internal/digest` and the URL-derived path identical, so
  the first draft passed against the very bug it was meant to catch. The
  executor caught this, re-mounted the test server under `/mounted`, and
  asserted the signed path is `/mounted/internal/digest`. The plan should have
  specified a base path.
- **016's step 4** as written produced a test that only proved slicing: a
  5000-byte body against a 64 KiB chunk size arrives in one chunk, so the read
  loop's `break` was never exercised. The executor replaced it with an infinite
  slow-drip generator that would hang without the break.

### Known weakness left in place

016's oversized-page test pins that the read loop terminates and that the
returned body is capped, but not *where* it breaks: a mutation breaking at
1000x the limit would still slice to `limit` and still report `truncated`, so
the test would pass while unbounded memory was read. The shipped code breaks at
`total >= limit`, which is correct — this is a note for whoever next edits
`bounded_get`, not a defect in what landed.

### Operator actions, in order

1. Review and merge the five branches. Land them one at a time and re-run both
   suites after each: 012 and 016 both touch `digest.py`, and 014 and 015 both
   touch `test_interactive.py`.
2. `cd worker && npx wrangler deploy` — **013 changes nothing for the user until
   this runs.** The Worker does not ship by merging. Note this also deploys
   plans 004, 006 and 011 from the previous pass, which as far as this
   repository can tell are still undeployed.
3. `git push` — nothing from this pass has been pushed.

## Merge log — 2026-08-31

All five approved branches merged into local `main` at the operator's explicit
instruction (normally the advisor never merges; the operator overrode that, as
in the previous pass). **Zero conflicts.**

`main` was fast-forwarded to `2e2a3fa` first — two cron commits touching only
`stats.json`, with `db71bee` (every branch's base) in its history.

| # | Branch merged | Merge commit | Python | Worker |
|---|---------------|--------------|--------|--------|
| 1 | `advisor/012-record-webhook-fallback-delivery` | `23e5121` | 200 | 43 |
| 2 | `worktree-agent-a41f05b27130ab990` (013) | `5adf1a1` | 201 | 44 |
| 3 | `advisor/014-validate-fit-notes` | `991f23f` | 206 | 44 |
| 4 | `advisor/015-share-signed-request` | `f491d4d` | 207 | 44 |
| 5 | `advisor/016-cap-page-bytes` | `cbdb034` | 212 | 44 |

Both suites were re-run after every single merge, never only at the end.

**Final merged tree: 212 Python tests, 44 Worker tests, all passing.** HEAD
`cbdb034`, 12 commits ahead of `origin/main`, **not pushed**.

212 is exactly the 199 + 1 + 1 + 5 + 1 + 5 predicted before merging, which is
the evidence that no branch silently dropped another's tests.

`git` auto-merged the three overlaps without help: `test_interactive.py` (013,
014, 015), `digest.py` (012's delivery block vs 016's `_get`), and
`test_digest.py` (012, 016). Every plan's key criterion was re-checked on the
composed tree afterwards — all nine hold, and no conflict markers exist
anywhere in the source.

### Still outstanding for the operator

1. **`cd worker && npx wrangler deploy`.** Merging did NOT ship 013 — the
   Worker runs on Cloudflare. Until this runs, `/resume` still creates a task
   and burns an Actions run exactly as before, even though `main` now says
   otherwise. This deploy also ships plans 004, 006 and 011 from the previous
   pass, which appear to have never been deployed.
2. **`git push`.** 12 commits are local-only, and the daily cron keeps moving
   `origin/main`, so this will keep diverging.
3. The five executor worktrees under `.claude/worktrees/` can be removed once
   the merges are pushed (`git worktree remove <path>`), along with their
   branches.

## Dependency notes

- **The five plans are independent** and touch disjoint regions. They may be
  executed in any order and in parallel worktrees. Merge one at a time and
  re-run both suites after each, as the previous pass did.
- File overlap to watch at merge time: 012 and 016 both touch `digest.py`
  (012 in `main()`'s delivery block around line 1606; 016 only `_get` at line
  338). 013, 014 and 015 touch files no other plan in this pass touches.
- `test_digest.py` is touched by 012 and 016; `test_interactive.py` by 013,
  014 and 015. Additive in every case — new tests, no edits to existing ones.
- **015 changes a security primitive's call path.** Land it on its own and run
  the Worker suite before and after, since the Python signer and the Worker
  verifier must agree byte-for-byte.
- **013 is the only plan that needs a Cloudflare deploy to take effect.**
  `worker/src/index.js` does not ship by merging — it ships when the operator
  runs `cd worker && npx wrangler deploy`. 013 can pass every done criterion,
  land on `main`, and leave `/resume` behaving exactly as it does today until
  that happens. Its Python test and README corrections do take effect on merge.
  012, 014, 015 and 016 are Python-only and are live as soon as they merge.
  (Plans 004, 006 and 011 from the previous pass changed `worker/` too; nothing
  in this repository records whether they were ever deployed.)
- **If you dispatch executors while local work is unpushed, make step zero of
  every prompt `git reset --hard main`** and give the agent concrete numbers to
  verify its base against. The previous pass lost a full executor run to a
  worktree that branched from `origin/main` instead of local `main`; the
  details are in the previous-pass record below.

### The three findings verified by running the code, not by reading it

Recorded because the evidence matters if any plan's premise is later
questioned:

- **012**: `digest.main()` was run with `INTERACTIVE_WORKER_URL` set and
  `post_interactive_cards` raising. Output: the digest went out over the
  webhook, the log said `Delivered 0 jobs. seen.json now holds 0 ids.`,
  `seen.json` was `{}`, and a second run posted the identical digest again.
- **013**: `interactive_runner.run_task({"kind": "resume", ...})` raises
  `ValueError: The task has no immutable job snapshot.` — which is what the
  user reads, after a full Actions run, when they use `/resume`.
- **013 (dead-code half)**: `grep` over tracked source confirms
  `resumeComponentAction`, `confirmResumePreview`, `createResumePreview` and
  `prepare_resume_preview` are referenced only by their own tests. No
  production caller exists for any of them.

### Findings surfaced but deliberately not planned

Recorded so they are neither lost nor re-audited without new evidence.

- **The quiet-day heartbeat has no webhook fallback.** `post_heartbeat`
  (`digest.py:1332-1353`) calls `post_interactive_cards` with no `try/except`,
  so a Worker outage on a day with no new jobs crashes the run — where the same
  outage on a day *with* jobs falls back to the webhook. Confirmed by reading.
  Not planned because it fails loudly, `notify-failure` fires and the watchdog
  alerts: an inconsistency, not a silent loss. Fixing it is a natural follow-up
  to 012 and is noted in 012's maintenance section.
- **README's Files section has fallen behind the tree.** Six tracked files
  appear nowhere in `README.md` at all — `worker_client.py`,
  `resume_render.py`, `resume_tailor.py`, `test_workflow_guard.py`,
  `.github/workflows/ci.yml` and `.github/workflows/digest-watchdog.yml`
  (verified by `grep -c` per filename). Three more are mentioned in prose but
  missing from the Files list: `openrouter.py` (README:180),
  `workflow_guard.py` (README:252) and `worker/test/commands.test.js`
  (README:154). `worker/schema.sql`'s entry also omits the `confirmed_resumes`
  and `resume_previews` tables, and the `CI/CD hardening` section says the
  repository has "four explicit GitHub Actions workflows" where there are five. Real, and it has a cost in a repo whose plans are
  executed by agents that read the README for orientation — but it is M effort
  for a docs-only gain, and it should be written *after* 012–016 land so it
  documents the final state. Plan 013 corrects the two README lines that are
  actively *wrong* (they describe `/resume` as working); the rest is
  incompleteness, not error.
- **`split_tiers` builds the review tier with `job not in strong`**
  (`digest.py:1163`). Dict `in` compares by equality, so it is O(n²) with deep
  comparison, and two structurally identical job dicts would collapse into one.
  Not planned: `n` is capped at 25, jobs are deduped by canonical URL and
  carry distinct ids well before this point, so the collapse is unreachable in
  practice. A readability nit with no observable defect.
- **`stats.json` carries non-zero `tailored` / `tailor_failed` columns no code
  path can produce** — `digest.py` passes both as a hardcoded `0`. Residue from
  the auto-tailoring pipeline removed by plan 007. Carried forward from the
  previous pass, still true, still harmless. Do not read those columns as data.
- **`discordSignatureIsValid` skips its freshness window on a non-numeric
  timestamp** (`worker/src/index.js:147`). Re-checked at `23de025`: still not
  exploitable, because the timestamp is part of the Ed25519-signed payload, so
  a forged one fails verification regardless. Previously rejected; re-confirmed
  rather than re-opened.
- **`internalRequestIsValid` signs the path but not the query string**
  (`worker/src/index.js:165`). Sound today — every internal route matches on
  `pathname` alone and none reads a query parameter. Recorded in plan 015's
  maintenance notes as the thing to revisit if such a route is ever added.

### Direction — options for the maintainer, not defects

These are choices, not problems, and they are deliberately not ranked against
the bug findings above. No plans were written for them.

- **Probe the sixteen employers the digest keeps seeing only through
  relisters.** `digest.py:1670-1680` lists them with occurrence counts over 20
  archived days — JAL International (12), Qualcomm (11), QuantumBlack /
  McKinsey (11), exequt MENA (10), Mindstream (8), Pasqal (6) and ten more.
  `.github/workflows/discover.yml` already exists, is `workflow_dispatch`-only,
  touches no secret, and prints a ready-to-paste `COMPANIES` line. This is the
  highest-value-per-minute action available: one dispatch, no code, and every
  confirmed board turns relister links into first-party ones — which is what
  the link-quality passes in `main()` exist to work around. Effort: minutes.
- **Finish `/resume` end to end** (task 2 of
  `docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`). The
  design is already written in `docs/2026-08-10-resume-command-design.md` and
  `docs/superpowers/specs/2026-08-12-reliable-resume-ingestion-design.md`, and
  roughly 60% of the code exists and is tested. It is blocked on one decision
  only the operator can make: where a confirmed resume is stored. The standing
  recommendation is D1 on the untrusted Discord path, synced back to
  `resume.md` by the daily workflow's existing commit step. Effort: M–L after
  the decision. Plan 013 is written to preserve, not delete, everything this
  would build on.
- **Close the calibration loop.** `docs/2026-08-10-calibration-signal-design.md`
  designs a click-based signal and nothing implements it. The grounding is
  stronger now than when that spike was written: across the 50 jobs sampled
  then, *every* review-tier job was there for low score alone — two of
  `split_tiers`' three conditions were inert, and scores were quantized to
  multiples of 5. Without a feedback signal the thresholds stay guesses.
  Effort: M, and it interacts with the Worker's 7-day task retention (see the
  previous pass's note on 004 vs 010).

### What was not audited

- **Live behavior of any external API** (Greenhouse, Ashby, Workable, Lever,
  Pinpoint, JSearch, OpenRouter, Discord, Cloudflare, GitHub). Every finding
  comes from reading code and running the offline suites. No live call was
  made.
- **Dependency advisories.** `pip-audit` was not run; `requirements.txt` is
  hash-locked and Dependabot opens weekly PRs, so the posture is reasonable,
  but no advisory check was performed this pass. `worker/` has no runtime
  dependencies at all.
- **`.worktrees/` and `.claude/worktrees/`** — disposable agent worktrees, some
  holding stale copies of this repository. Read only to avoid mistaking a stale
  copy for tracked source; not audited.
- **`seen.json`, `stats.json`, `jobs/*.json`** — generated state, inspected as
  data rather than reviewed as code.
- **`resume.md`, `JOBS.md`, `profile.json`** — user content, not code.
- **`resume_render.py`, `job_archive.py`, `resume_source.py`** were read for
  the findings above but not swept exhaustively. `digest.py`'s five ATS
  fetchers (lines 352–653) were read for their HTTP handling only, not for
  field-mapping correctness against each board's live schema — that needs a
  live call, which this pass did not make.

---

# Previous pass — 2026-08-09 audit (001–011)

Everything below this line is the record of the previous pass. All eleven plans
are DONE and merged. Its "Findings considered and rejected" section is still
authoritative: **do not re-surface those findings without new evidence.**

Note that its verification baselines (45 / 55 / 70 / 72 Python tests, 16 / 20 /
24 Worker tests) are historical. The current baseline is 199 / 43, recorded
above.


## (Historical) Before executing anything — read this

> Superseded. Every instruction in this section applied to the 2026-08-09 pass
> and all eleven of its plans are DONE and merged. Nothing here is a live
> instruction; it is kept because the reasoning below explains defects that
> were found in plans during execution, which is worth reading before writing
> or executing new ones.

**These plans were written against `origin/main` at commit `6c7541e`.**

At the time of the audit the local working tree was 13 commits behind
`origin/main`, and those 13 commits contain most of what these plans touch:
the Cloudflare Worker, the D1 schema, `.github/workflows/interactive.yml`, and
five new Python modules. A plan executed against the older tree will fail its
own drift check on step one.

```bash
git pull --ff-only && git rev-parse --short HEAD
```

Confirm this prints `6c7541e` or later before starting any plan.

**Uncommitted work exists in `.worktrees/discord-tailoring`** (branch
`codex/discord-tailoring`). It modifies `digest.py`, `interactive_runner.py`,
`resume_source.py`, `test_interactive.py`, and adds untracked `resume_render.py`
and `resume_tailor.py`. Plans that touch those files say so in their header.
**Plan 007 must not run until that branch is merged or abandoned.**

### Verification baseline (verified green at `6c7541e`)

| Purpose      | Command                                                 | Expected               |
|--------------|---------------------------------------------------------|------------------------|
| Install      | `pip install -r requirements.txt`                       | exit 0                 |
| Python tests | `python -m unittest test_digest.py test_interactive.py` | `OK`, 45 tests         |
| Worker tests | `cd worker && node --test`                              | 16 pass, 0 fail        |

Both suites run in CI before the digest calls any live API. There is no lint,
formatter, or typecheck step in this repository.

**The Python test count is 45 only at `6c7541e`.** Plans 001, 002, 003, 005 and
007 each change it (+2, +1, +3, +4, −2 respectively), so an executor running
second will not see 45. Each of those plans opens with a step that records the
count it actually observes and expresses its done criteria as a delta from
that. Do not treat 45 as a fixed number, and do not "correct" a plan whose
count does not match this table.

The Worker suite count (16) is changed only by plans 004 and 006 (+2 each).

## Execution order & status

| Plan | Title | Priority | Effort | Depends on | Status |
|------|-------|----------|--------|------------|--------|
| [001](001-plumb-gemini-key-into-interactive-workflow.md) | Pass `GEMINI_API_KEY` to the interactive workflow | P1 | S | — | DONE — reviewed, APPROVED, **merged to main** |
| [002](002-make-total-delivery-failure-loud.md) | Fail the run loudly when no job card reaches Discord | P1 | S | — | DONE — reviewed, APPROVED, **merged to main** |
| [003](003-prune-the-job-snapshot-archive.md) | Prune the daily job snapshot archive on a TTL | P1 | S | — | DONE — reviewed, APPROVED, **merged to main** |
| [004](004-add-d1-retention.md) | Add retention to the Worker's D1 tables | P1 | S–M | — | DONE — reviewed, APPROVED, **merged to main** |
| [005](005-one-stats-row-per-day.md) | Keep one stats row per day so drift measures days | P2 | S | — | DONE — reviewed, APPROVED, **merged to main** |
| [006](006-ack-discord-before-dispatching.md) | Acknowledge the Discord interaction before dispatching | P2 | S–M | — | DONE — reviewed, APPROVED, **merged to main** |
| [007](007-remove-dead-auto-tailoring-code.md) | Remove the dead auto-tailoring code from `digest.py` | P2 | S | ~~worktree~~ resolved | DONE — reviewed, APPROVED, **merged to main** (2nd attempt; see rejection note) |
| [008](008-bring-the-readme-back-in-sync.md) | Bring the README back in sync with the architecture | P2 | M | run last | DONE — reviewed, APPROVED, **merged to main** |
| [009](009-spike-resume-and-tailor-jd-commands.md) | SPIKE: design the `/resume` and `/tailor-jd` paths | P3 | M | — | DONE — reviewed, APPROVED, **merged to main** |
| [010](010-spike-close-the-calibration-loop.md) | SPIKE: design a click-based calibration signal | P3 | M | — | DONE — reviewed, APPROVED, **merged to main** |
| [011](011-register-discord-commands.md) | Make the repo the source of truth for command registration | P2 | S | — | DONE — reviewed, APPROVED, **merged to main** |

### Worktree locations (branches are NOT merged; merging is the operator's call)

| Plan | Branch | Worktree |
|------|--------|----------|
| 001 | `advisor/001-gemini-key-interactive` | `.claude/worktrees/agent-a4b44c82bd581d707` |
| 002 | `advisor/002-loud-delivery-failure` | `.claude/worktrees/agent-ac13b66a070b0cddc` |
| 003 | `advisor/003-prune-job-archive` | `.claude/worktrees/agent-ae315b32dfa3114f3` |
| 004 | `advisor/004-d1-retention` | `.claude/worktrees/agent-abfa68d2fa2cc279e` |
| 005 | `advisor/005-one-stats-row-per-day` | `.claude/worktrees/agent-ad98dbb2516e6b5ee` |
| 006 | `advisor/006-ack-before-dispatch` | `.claude/worktrees/agent-a9437a1dbee866905` |
| 008 | `worktree-agent-a4c993adc3cbc4aa2` (not the planned `advisor/008-…`) | `.claude/worktrees/agent-a4c993adc3cbc4aa2` |
| 009 | `advisor/009-spike-resume-command` | `.claude/worktrees/agent-a0fe854f0c87c4dcf` |
| 010 | `advisor/010-spike-calibration-signal` | `.claude/worktrees/agent-a2a756db1e7c0857d` |

### Findings produced by the two spikes

- **010 established, and the reviewer independently reproduced**: across the 50
  jobs in `jobs/2026-08-08..10.json`, 25 are Strong and 25 are review-tier — and
  **every** review-tier job is there for low score alone. Zero are there for a
  missing JD, and the 2 with an unverified ranking also scored below every
  candidate threshold. So of the three conditions in `split_tiers`
  (`digest.py:732-741`), only the score threshold is currently doing any work.
  Scores are also quantized to multiples of 5 with 12 of 50 sitting at exactly
  85, which caps how finely any future signal could ever discriminate.
- **010 also found**: `stats.json` carries nonzero `tailored` / `tailor_failed`
  values that no current code path can produce — `digest.py` passes both as a
  hardcoded `0`. They are residue from the removed auto-tailoring pipeline.
  Harmless, but do not read them as data.
- **009's open questions requiring the operator or a live probe** (it made no
  live calls, as instructed): whether `/resume` and `/tailor-jd` are registered
  with Discord at all (no registration code exists in the repo); the Discord CDN
  attachment-URL lifetime and auth requirements; which storage option to use for
  the confirmed resume (the doc recommends D1 over granting `contents: write` to
  a Discord-triggered workflow, and flags it as the operator's call).

### Merge log — 2026-08-10

All nine approved branches merged into local `main` at the operator's explicit
instruction (normally the advisor never merges; the operator overrode that).
**Zero conflicts.** Merge order and result:

1. `advisor/001-gemini-key-interactive` → 47 py tests
2. `advisor/003-prune-job-archive` → 50 py tests (`test_interactive.py` auto-merged with 001)
3. `advisor/002-loud-delivery-failure` → 51 py tests
4. `advisor/005-one-stats-row-per-day` → 55 py tests (`digest.py` auto-merged with 002)
5. `advisor/004-d1-retention` → 18 worker tests
6. `advisor/006-ack-before-dispatch` → 20 worker tests (`worker/src/index.js` auto-merged with 004)
7. `advisor/009-spike-resume-command`, `advisor/010-spike-calibration-signal`, `advisor/008-readme-sync`

Final merged tree: **55 Python tests, 20 Worker tests, all passing.** HEAD
`67d6b10`, 21 commits ahead of `origin/main`, **not pushed**.

Follow-up committed directly after the merges (`67d6b10`): plan 008's README
was written before 002 landed and correctly omitted the non-zero-exit
behavior. With 002 merged, the `## Delivery` section now records it.

`advisor/008-readme-sync` was renamed from `worktree-agent-a4c993adc3cbc4aa2`
before merging, for naming consistency with the other eight.

### Executor run rejected — 007, first attempt (2026-08-10)

Branch `advisor/007-remove-dead-tailoring` (commit `6a2b70f`) was **rejected on
review and must not be merged.** It reported COMPLETE with all done criteria
passing, and it was internally consistent — but it was built on the wrong base.

The harness created its worktree from `origin/main`, not from local `main`. At
that moment local `main` was 23 commits ahead (nine merged plans plus
`codex/discord-tailoring`), and none of that was visible to the executor. The
tell was in its own report: it measured a 45-test baseline and a 16-test Worker
suite, where `main` had 72 and 20.

Why it mattered concretely: the branch's `test_digest.py` is missing all four
tests added by plans 002 and 005
(`test_failed_interactive_delivery_fails_loudly_without_falling_back`,
`test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen`,
`test_rerunning_the_same_day_replaces_that_days_row`,
`test_drift_counts_days_not_runs`), and it rewrote `run_main`'s signature to
drop `tailor_result` while updating only the call sites its stale base knew
about. Merging it risked either dropping those tests in conflict resolution or
leaving newer call sites passing three positional arguments to a two-argument
function.

**Lesson for future dispatches**: agent worktrees may branch from `origin/main`
rather than local `main`. When local work is unpushed, the executor prompt must
tell the agent to `git reset --hard main` first and must give it concrete
base-verification numbers to check against before it does any work.

The re-run (`advisor/007-remove-dead-tailoring-2`, commit `a76d728`) did exactly
that, verified the base as 72 Python / 20 Worker tests before touching anything,
found **8** `run_main` call sites rather than the plan's stated 7, updated all of
them, and preserved every test added by plans 002 and 005. Approved and merged.

The rejected branch `advisor/007-remove-dead-tailoring` could not be deleted —
its worktree at `.claude/worktrees/agent-abf25663aa1a20d0a` still holds it. To
clean up: `git worktree remove .claude/worktrees/agent-abf25663aa1a20d0a` then
`git branch -D advisor/007-remove-dead-tailoring`. **Do not merge that branch.**

### Plan defects found by executors (corrected in the plan files)

- **003 step 4** — the fixture files had to be written with `json.dump`, not via
  `write_snapshots`, because `write_snapshots` prunes against wall-clock today
  on every call and would delete the old fixture during setup. Assertions were
  kept byte-identical. This also surfaced a real property worth knowing:
  `write_snapshots` prunes relative to wall-clock today, not its `date`
  argument — harmless for the only production caller, which always passes
  today's date.
- **002 done criteria** — a grep counted the function's own `def` line. Fixed.
- **001 step 2** — a relative path in the new test would break when both suites
  run in one process (as CI runs them). The executor anchored it to `__file__`.
  Correct; the plan should have said so.
- **011 done criteria** — required `grep -c "POST" worker/register-commands.mjs`
  to return 0, which the script the plan itself prescribes can never satisfy:
  its comment block explains *why not* POST and therefore contains the word.
  The executor flagged it rather than deleting the comment to force the check
  green — the right call. Criterion now checks the HTTP method instead.

### Reviewer verification of 011's key test

The bidirectional name check in `worker/test/commands.test.js` was tested by
deliberately breaking it in the disposable worktree, both directions:

- renaming `resume` → `resume-v2` in `commands.js` (handler untouched) →
  1 failure, `worker/src/index.js has no dispatch branch for /resume-v2`
- changing the handler to dispatch on `tailor-jd-x` (definitions untouched) →
  1 failure

Both reverted, suite back to 24 green. The guard is real, not decorative.

### Execution log

- **2026-08-10** — `git pull --ff-only` run; HEAD `28bf1b1`. Drift check against
  the `6c7541e` stamp: only generated state changed (`jobs/2026-08-10.json`,
  `seen.json`, `stats.json`). No in-scope source file moved, so every plan's
  drift check passes. Eight executors dispatched into isolated worktrees.
  Each worktree branches from the same base, so the plans' "land one at a time"
  advice applies at **merge** time, not execution time.
- **2026-08-10** — The API session limit killed five executors mid-run. 001, 003
  and 006 had already finished their work (all done criteria re-verified by the
  reviewer); 003 and 006 died before committing, so their changes sit
  uncommitted in their worktrees. 009 and 010 produced nothing — no worktree,
  no branch. Reviewer verdicts below are based on re-running each plan's done
  criteria directly, not on the executors' self-reports.
- **Correction to plan 002 made during review**: its done criterion
  `grep -n "post_discord(webhook, lines)" digest.py` expected 2 matches but the
  function's own `def` line matches the pattern, so the raw grep returns 3 even
  on an unmodified file. The criterion now filters the `def` line. The executor
  caught this and verified the real invariant (exactly 2 call sites) instead of
  failing on a bad check — correct behavior.
- **Improvement to plan 001 made by its executor**: the plan specified a
  relative path for the workflow file in the new test. The executor anchored it
  to `os.path.dirname(os.path.abspath(__file__))` instead. This is a real fix,
  not a deviation to reverse — `test_digest.py` chdirs to a temp directory at
  import time, so a relative path breaks when both suites run in one process,
  which is exactly how CI runs them.

Status values: TODO | IN PROGRESS | DONE | BLOCKED (with one-line reason) |
REJECTED (with one-line rationale).

## Dependency notes

- **001 should land before the `codex/discord-tailoring` branch merges.** That
  branch's tailoring reads `GEMINI_API_KEY`, and the interactive workflow does
  not supply it. Merging without 001 ships a feature that silently returns the
  untailored master resume on every click.
- **007 requires the worktree to be merged or abandoned first.** It deletes ~90
  lines from `digest.py`; the worktree edits the same file. It has been verified
  that the worktree does not use any of the symbols 007 removes
  (`resume_tailor.py:24` defines its own `TAILOR_MODEL`), but a large deletion
  racing a nearby edit is a bad merge waiting to happen.
- **008 should run last** among whichever of 001–007 are actually executed, so
  the README documents the final state rather than an intermediate one. It does
  **not** require all of them: the plan reads the repository to build its Files
  section and its step 3 explicitly tells the executor to check `digest.py` for
  `delivery_failed` before documenting the non-zero-exit behavior. Running 008
  alone is valid and still an improvement.
- **004 and 006 both modify `worker/src/index.js`** in different regions (004
  adds a function after `claimInternalNonce` plus one line in
  `/internal/digest`; 006 changes `handleInteraction` and the entry point).
  They should merge cleanly. Land one at a time and re-run `node --test` after
  each.
- **002, 005, and 007 all modify `digest.py`** in different regions (delivery
  block ~1108, stats functions ~920, dead code ~740–818 and the constants block
  ~158–186). Same advice: land one at a time.
- **010 interacts with 004.** Plan 004 prunes `tasks` rows 7 days past expiry,
  which is the click history 010 would otherwise depend on. 010 is written to
  design its own durable record rather than rely on task retention; if 004's
  window is ever revisited, re-read 010's constraint section.
- 003 and 004 set retention windows that must stay aligned at 120 days, matching
  `STATE_TTL_DAYS` in `digest.py`. Both plans say so in their maintenance notes.

## Findings considered and rejected

Recorded so they are not re-audited on the next pass.

- **`_tailored_pdf` produces meaningless output** (`interactive_runner.py:43-60`).
  Real and confirmed by running it: the "Gaps to verify" line renders the
  alphabetically-first JD words absent from the resume — observed output was
  `about; architecture; automated; build; candidates; clear; communication; containers`
  — and the bullet selection matched 32 of 44 resume lines because any shared
  3-letter word counts, then truncated to the first 18 in file order.
  **Already being replaced** on `codex/discord-tailoring` by `resume_tailor.py`
  and `resume_render.py`, whose own docstring names the same defect. No plan
  written.
- **`run_task` tells the user "PDF generation is the next implementation slice"
  while attaching a PDF** (`interactive_runner.py:92-98`). Same branch fixes it.
  No plan written.
- **Identifiers in `worker/wrangler.jsonc:6-12`.** The Discord public key is
  public by design (it is what verifies Discord's signatures), and the
  repository is private — confirmed via `gh repo view --json visibility` →
  `PRIVATE`. Not an exposure. No secret values are committed anywhere; the four
  real secrets are set via `wrangler secret put`.
- **`discordSignatureIsValid` skips its 5-minute freshness window when the
  timestamp header is non-numeric** (`worker/src/index.js:120`). `Number("abc")`
  is `NaN` and `Math.abs(NaN) > 300` is false. Not exploitable: the timestamp is
  part of the signed payload, so a forged one fails Ed25519 verification.
- **`tailoringInputFromSnapshotRow(snapshot, snapshot?.job_key)` compares a row
  against itself** (`worker/src/index.js:274`). The real button-to-snapshot
  binding happens one line later via `buttonReferenceForJobKey`. Confusing to
  read, not a security hole. A readability nit, not worth a plan.
- **Unclosed file handle in `_tailored_pdf`** (`interactive_runner.py:36`,
  observed as a `ResourceWarning` during the test run). Trivial, and that
  function is being rewritten on the worktree branch.
- **"Establish a verification baseline" was not needed.** Both suites exist,
  run in CI before delivery, and pass. This is unusual and worth keeping.

## What was not audited

- `.worktrees/discord-tailoring` — dirty, in-flight work. Read to avoid planning
  work already underway; not audited for defects.
- `seen.json`, `stats.json`, `jobs/*.json` — generated state, inspected as data
  rather than reviewed as code.
- `resume.md`, `JOBS.md`, `profile.json` — user content, not code.
- Live behavior of any external API (Greenhouse, Ashby, Workable, Lever,
  Pinpoint, JSearch, Gemini, Discord, GitHub). Every finding is from reading
  code and running the offline suites; no live call was made.
