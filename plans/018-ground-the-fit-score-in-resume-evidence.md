# Plan 018: Ground the fit score in resume evidence

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 01c9dc3..HEAD -- digest.py test_digest.py profile.json resume.md`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M (018A) + M (018B)
- **Risk**: MEDIUM — changes what every delivered score means
- **Depends on**: none (017 is unrelated; it touches the fallback path)
- **Category**: correctness (score honesty)
- **Planned at**: commit `01c9dc3`, 2026-09-06

## Why this matters

Discord delivered a role at **fit 85**. The same role through Watheq's match
feature scored **58** with a low-ceiling verdict: *"Optimizing will improve your
wording, but is unlikely to make you competitive for this role given the gaps
below."* Watheq is right and the digest is wrong, for three reasons that are all
structural rather than a matter of prompt wording.

### 1. The ranker scores a self-marketing blurb, not a resume

`rank_jobs` (`digest.py:1176`) sends `PROFILE["blurb"]` — 1,900 characters of
self-description. `resume.md` is never seen by the ranker; it is read only on
the Tailor-CV path (`interactive_runner.py:67-74`).

Watheq's own anti-inflation clause names this exact failure mode
(`netlify/lib/ai-contracts/contracts/index.js:403`):

> A term whose ONLY appearance in the resume is a skills list, a summary, a
> headline, or an objective is an unproven claim, not evidence — anyone can type
> any keyword there.

The digest's entire candidate input **is** a summary. By that rule nothing in it
could earn a hard-skills point, yet it is all the ranker has, so every keyword
in it reads as demonstrated capability.

### 2. The rubric has no band for experience or education

The four bands (`digest.py:1188-1195`) test function, seniority label, and
location. There is no year count and no credential check anywhere in the prompt.
Watheq weights those at **30 + 15 = 45 of 100** (`contracts/index.js:387`:
*hard skills 40, experience 30, education 15, soft skills 15*).

Against `resume.md` — Watheq founded Aug 2025, prior role Data Analyst / Senior
Sales Specialist, highest credential a pipefitting diploma — that is where much
of the 85 to 58 gap lives, and the digest has nowhere to put it.

### 3. No evidence gate, no blockers, no ceiling

`apply_ranking` (`digest.py:1401`) validates `why` for non-emptiness and nothing
else. Watheq requires every risk and strength to cite a copied snippet,
re-verifies each one server-side against the actual source text, **demotes**
unverified risks to `unclearRisks` and **deletes** unverified strengths, and
only then derives the ceiling from `confirmedRisks.length >= 2 || riskTier ===
'critical'` (`src/lib/match/optimizationOutlook.ts:35-41`) — never from the
score. A 58 alone cannot produce that copy; two server-verified blockers can.

## Decisions already made

| Question | Decision |
|---|---|
| Score effect of blockers | Cap the score **and** add the verdict. `cap_teaser_scores` is the existing idiom. |
| Which jobs get the per-job pass | Strong tier only (`ranking_verified`, score >= 80, has a JD). ~3-6/day. |
| Capped score below the floor | Deliver it, flagged. The cap runs after `drop_below_floor`, so it can never *cause* a drop; the job is then delivered and remembered like any other delivery. |
| Gold-set evals | **Built** (`eval/`) once 018A landed, at the operator's request. |

---

## 018A — Fix what the ranker reads

### Current state

`digest.py:1176-1179`:

```python
    profile = PROFILE["blurb"]
    targets = PROFILE.get("target_titles")
    if targets:
        profile += " Currently targeting: " + ", ".join(targets) + "."
```

### Task A1 — a bounded, degrading resume loader

Add `load_resume_evidence(path="resume.md", limit=RESUME_EVIDENCE_CHARS)` to
`digest.py`. It returns the resume text, or `""` when the file is missing,
unreadable or empty.

**It must not raise.** `interactive_runner._master_document`
(`interactive_runner.py:67`) deliberately raises on a missing resume because
that path is user-triggered and should fail loud. The ranker is the opposite: a
bad read must degrade to exactly today's behaviour rather than crash the daily
run. Do not reuse `_master_document`.

`RESUME_EVIDENCE_CHARS = 8000` — `resume.md` is ~4.3 KB today, so the cap is
headroom against an unbounded file, not a working truncation.

**Verify**: a test asserting a missing path returns `""` and does not raise, and
that a present resume comes back non-empty.

### Task A2 — send two labelled blocks

**Do not replace the blurb with the resume.** The blurb carries the constraints
— `MUST BE BASED IN SAUDI ARABIA`, "never managed a team" — and
`PROFILE_REFRESH_PROMPT.md:19-20` records that those are what stop Lead/Staff
and relocation roles scoring 90+. `resume.md` contains none of it.

Send both, tagged, following the `taggedBlock` idiom Watheq uses
(`netlify/lib/ai-contracts/prompt.js:3`):

- `<candidate_evidence>` — `load_resume_evidence()`.
- `<candidate_preferences>` — `PROFILE["blurb"]` + target titles, built exactly
  as today.

The prompt must state which is which: the evidence block is the only thing that
proves capability; the preferences block states constraints and wants and proves
nothing.

When `load_resume_evidence()` returns `""`, omit the evidence block and its
rules entirely rather than sending an empty tag — an empty block invites the
model to treat "no evidence" as "no capability" and would tank every score on a
day the file went missing.

Cost is one prefix per call, not per job: `rank_jobs` makes a single call for
the whole day's listing.

### Task A3 — port the demonstrated-vs-listed rule

Adapted from `contracts/index.js:403-404` to a Markdown resume: a term whose
only appearance is under `## SUMMARY` or `## SKILLS` is a claim, not evidence;
`## EXPERIENCE` and `## PROJECTS` bullets are evidence. A resume that lists every
required tool and demonstrates none scores low on skills, not high.

### Task A4 — add the two missing rubric dimensions

Extend the rubric so that when the JD states a hard year count or a required
degree/credential, it is weighed, and the band is capped when the evidence block
does not show it.

**Keep the existing clause** that a requirement the excerpt does not mention is
unknown rather than a gap. That clause is load-bearing: `jd_excerpt` is a
2,000-character window (`JD_RANK_CHARS`), so absence of a requirement in it is
usually truncation, not absence from the job.

**Do not port `categoryScores`.** Watheq's own eval notes record that the model
emits the four categories on inconsistent scales and that nothing reconciles
their sum against `score` (`eval/match-score.mjs:16-19`). It is decorative
there; here it would buy tokens, not honesty.

### Expected side effect — drift will fire once

This lowers scores broadly. `avg_score` feeds `check_drift`, which warns on a
drop over `DRIFT_SCORE_DROP_THRESHOLD = 15` across a 7-day window
(`digest.py:337-339`). A deliberate rubric change will trip that alarm once.

Accept the warning and log a line naming the cause. **Do not widen the
threshold** — that would disable the detector permanently to silence one
expected event.

---

## GATE — measure 018A before building 018B

**018A must be committed and its effect measured before any of 018B exists.**

The premise of this pass is that the ranker inflates because it reads a blurb
instead of a resume. The measurement below is what tests that premise. If 018A
alone does not move the grounding case, **stop and re-check the premise** rather
than layering a verdict on top of an input fix that did not work.

### The case

Ask the operator which role Watheq scored 58; they hold the Watheq output and it
names the blockers. Absent that, use the strongest available stand-in:
**`jobs/2026-09-03.json` → OmniOps, "Senior Solution Engineer"** — score 85,
`ranking_verified: true`, a 5,492-character `grade=full` JD. It is the most
recent exact-85 in the archive carrying a complete posting.

### The shape mismatch

`rank_jobs` reads `jd_signal(job["description"], JD_RANK_CHARS)` off a **live**
job dict. Archived snapshots store the JD at `snapshot["jd"]["text"]` — there is
no `description` key. The harness maps snapshot shape back to job shape, and
handles older archives that predate the `jd` block the same way `snapshot_grade`
(`job_archive.py:126`) handles archives written before its field existed.

`scripts/measure_ranker.py` is that harness. It is a measurement tool, not
production code.

### What counts as passing

`chat_json` sends `seed=7` and deliberately no temperature
(`openrouter.py:94-99`), so this is reproducible-ish, **not deterministic**. One
run landing on a number proves nothing. Run three times against the old prompt
and three against the new, and judge:

- **Direction** — does the new prompt's median score drop, and does the drop
  survive the spread across the three runs? A move inside run-to-run noise is
  not a result.
- **Reasons** — does the new `why` name an evidence gap (years, a credential, a
  required skill the resume does not demonstrate) rather than restating the
  title match? This matters more than the number: it is what shows the model is
  actually reading the evidence block.

**Do not set a target of 58.** Watheq scores a different rubric over a different
input window. Agreement to the point is not the goal, and chasing it would mean
tuning the prompt until it produces one number on one job.

---

## 018B — The evidence-gated verdict

**Do not start until the gate above has been run and reported.**

New module **`fit_check.py`**. `digest.py` is already 120 KB; this is a distinct
responsibility with its own model call, schema and validators.

### Contract

One call per job via `chat_json` (`openrouter.py:76`). Returns:

- `blockers[]` — `{type, severity: medium|high|critical, title, explanation,
  evidence[]}`, each evidence item `{source: resume|job_description, snippet}`.
- `unclear[]` — `{topic, reason, evidence_needed}`.
- `strengths[]` — `{title, why_it_matters, evidence[]}`.

`type` is an enum ported from `REALITY_CHECK_RISK_TYPES`
(`netlify/lib/strategic-reality-check.js:13-23`), trimmed to what this pipeline
can see: `missing_required_skill`, `experience_gap`, `seniority_mismatch`,
`domain_gap`, `education_certification_gap`, `location_work_authorization`,
`role_scope_mismatch`, `evidence_quality`, `other`.

**Do not port `limits.cannot_determine`.** It feeds a full-analysis panel in
Watheq; a Discord footer has nowhere to put it, and a required field with no
reader is a validation failure mode that buys nothing.

### Prompt rules to port

From `contracts/index.js:1013-1023`: every blocker and strength must cite a short
snippet copied from the resume or the JD; if evidence is missing, ambiguous or
inferred it goes to `unclear`, not `blockers`; invent no skills, credentials,
employers, dates or metrics; never claim the candidate will be rejected,
screened out, or interviewed.

### Post-validation — this is the mechanism, not the prompt

Reuse what `resume_source.py` already has; add only what it does not.

- **New primitive** in `resume_source.py`: `snippet_supported(snippet, source)`
  — normalise, then accept on substring match **or** token overlap >= 0.75. This
  is the genuine port of `verifyEvidenceSnippet`
  (`strategic-reality-check.js:105`) and is the one thing `resume_source.py`
  does not already do. Reuse its existing `_normalise`.
- **Reuse** `unsupported_terms(title, vocabulary(resume_text, job_title,
  company))` (`resume_source.py:77`, `:59`) on each blocker *title*, to catch an
  invented framework name that snippet checking alone would pass. Six existing
  tests already cover these primitives (`test_interactive.py:609-669`).
- **Asymmetric handling, exactly as Watheq does it** — a blocker whose evidence
  fails verification is **demoted** to `unclear`
  (`strategic-reality-check.js:142`); a strength whose evidence fails is
  **deleted** (`:187`). Unverifiable pessimism is softened; unverifiable
  optimism is removed. Only surviving blockers count toward the ceiling.
- **Banned-claim check** — port `BANNED_CLAIM_PATTERN`
  (`strategic-reality-check.js:43`). A digest must never tell the reader they
  will be rejected.
- **Safe fallback** — when the call fails or nothing survives, return a neutral
  result with zero blockers. A failed evidence pass must never invent a ceiling
  and must never change a score.

### `ceiling_band(score, blockers)`

Port of `computeOptimizationOutlook` (`optimizationOutlook.ts:35-41`): `low`
when there are >= 2 surviving blockers or any `critical`; `high` when there are
none; `mid` otherwise. Watheq's `<35` / `>=55` score thresholds are calibrated
for a resume match, not for a digest whose delivery floor is 50 — re-tune them
here rather than copying the numbers.

### `cap_blocked_scores(jobs)`

Mirror `cap_teaser_scores` (`digest.py:1421-1438`): cap at
`BLOCKED_SCORE_CAP = 69` (same value and same reason as `TEASER_SCORE_CAP` —
hold it out of the strong tier), append `" (capped: N verified requirement
gaps)"` to `why` truncated at 140, return the capped jobs so `main()` can log
each one.

**The two caps can collide.** `cap_teaser_scores` already ran at
`digest.py:2050` and appends its own suffix. A job that is both a teaser and
blocked would take two suffixes on a 140-character field, and the second
truncation can eat the first. Skip the blocked suffix when the score is already
at the cap — both caps are the same number, so there is nothing left to say
about it. Test that case explicitly.

---

## 018C — Where it runs, and why exactly there

Insert **after `cap_per_company` (`digest.py:2085-2090`), before `split_tiers`
(`digest.py:2092`)**. That one position solves three problems at once:

- **`avg_score` is computed at `digest.py:2059`**, upstream. Drift monitoring
  keeps its current meaning and
  `test_the_drift_average_is_measured_before_the_floor_cuts` still passes. A
  pass mutating scores above that line would silently change what drift
  measures.
- **`drop_below_floor` ran at `digest.py:2061`**, also upstream. A capped score
  therefore cannot *cause* a job to be dropped or remembered as below-floor —
  "deliver it, flagged" falls out of the pipeline order for free, with no new
  branch in `drop_below_floor`. The job IS still remembered afterwards, like
  every delivery, which is what stops it being re-fetched and re-capped every
  morning.
- **`split_tiers` has not run yet.** A capped job lands in "Worth reviewing" on
  its own, instead of carrying a `Strong matches` label next to `fit 69`.

### Selecting the population

Extract `split_tiers`' strong predicate (`digest.py:1494-1499`) into
`is_strong(job)` and use the same helper for both the evidence-pass population
and `split_tiers` itself — one definition, no drift between them.

While extracting, rewrite the review tier as
`[job for job in jobs if not is_strong(job)]`. Today it is
`[job for job in jobs if job not in strong]` — dict `in`, so an O(n^2) deep
equality compare, and two structurally identical job dicts would collapse into
one. `plans/README.md:296` flagged this and deliberately did not plan it because
it is unreachable at n <= 25. That reasoning holds, but refactoring the
predicate out of this exact expression while leaving the membership test in
place is worse than either fixing it or not touching it.

Bound cost with `REALITY_CHECK_MAX_JOBS = 10`, highest score first, and one
shared deadline in the style of `RANK_BUDGET_SECONDS`.

## 018D — Snapshot and card

- **`make_snapshot`** (`job_archive.py:145`): add `ceiling` (the band) and
  `blockers` (surviving titles, max 3). Additive fields with `schema_version`
  staying 1 — the established idiom, documented at `job_archive.py:173-175`.
- **`job_card_payload`** (`job_archive.py:181`): append
  `" · Low ceiling, N blockers"` to the footer when the band is `low`, following
  the existing footer-qualifier chain. Put the blocker titles in a new embed
  field so they are readable rather than crammed into the footer.
- The webhook fallback digest (`digest.py:2109-2134`) gets the same flag through
  its existing `flags` list.

---

## Done criteria

- [ ] `rank_jobs` sends the resume as a labelled evidence block alongside the
      preferences block, and omits the block entirely when the resume is absent.
- [ ] `load_resume_evidence` returns `""` rather than raising on a missing file.
- [ ] The rubric weighs a stated year count and a stated credential.
- [ ] The gate has been run and its result recorded in `plans/README.md`.
- [ ] An unverifiable blocker is demoted to `unclear`; an unverifiable strength
      is deleted. Both proven by a test that fails without the check.
- [ ] A capped job is delivered, tiered `Worth reviewing`, and absent from
      `seen.json`.
- [ ] `avg_score` for a run equals the pre-cap average.
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` -> `OK`.
- [ ] `cd worker && npm test` -> unchanged at 44.
- [x] Gold set exists, and is verified to discriminate rather than assumed:
      an all-88 ranker passes 1 of 6, an all-30 ranker passes 4 of 6 and
      fails the ceiling, and moving only the boilerplate twin trips the
      invariance check.

## STOP conditions

- The evidence pass changes a score anywhere above `digest.py:2061` → stop. That
  breaks drift monitoring and the floor guarantee at once.
- A failed or empty evidence pass produces a ceiling or a cap → stop. Silence
  must mean "no verdict", never "bad verdict".
- The gate shows no movement, or movement inside run-to-run noise → stop and
  report. Do not build 018B on an unproven premise.
- You find yourself widening `DRIFT_SCORE_DROP_THRESHOLD` → stop. That disables
  the detector permanently to silence one expected event.
- A pre-existing test is edited rather than added to → stop and report.

## Explicitly out of scope

- ~~**Gold-set evals.**~~ **DONE** — built after 018A rather than deferred. See
  `eval/README.md`. Six frozen fixtures (floor, ceiling, the HungerStation case,
  the OmniOps case, a listed-only-skills guard, and a boilerplate-invariance
  pair), a weighted scorer with a hard `must_not_credit` gate, and `test_eval.py`
  covering the scorer offline. The `blockers` category is written but inert until
  018B emits typed blockers, at which point it switches on with no code change.

  The risk this was meant to close is closed: `check_drift` compares the daily
  average against its own recent history, so a slow inflation drift moves the
  baseline with it and never fires. The gold set has fixed expectations instead.
- Re-tuning `MIN_DELIVER_SCORE`, `STRONG_MATCH_MIN_SCORE`, `TEASER_SCORE_CAP`,
  or `40 + hits * 15`.
- Backfilling `jobs/*.json` or `stats.json`. History stays as it ran.
- The Tailor-CV path. `clean_gaps` / `clean_fit_notes` already pass an evidence
  check and are not what lied.
