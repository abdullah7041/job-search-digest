# Plan 019: Show the scorer the half of a posting that can lower a score

## Status

- **Priority**: P1
- **Effort**: S (019A, the run budget) + L (019B, the scoring)
- **Risk**: MEDIUM — changes what every delivered score means, and what the
  workflow is allowed to spend
- **Depends on**: 018 (the evidence gate this makes actually run)
- **Outcome**: **DONE**. 019A `85f878f`, 019B `f862ef5`.

## The complaint

A Ma'aden posting was delivered on 2026-09-08 as a **Strong match, fit 91**,
with the reason "Excellent senior AI/LLM IC role in Riyadh matching candidate's
GenAI, evaluation, and RAG skills."

Watheq scored the same posting **67** and named two gaps: a bachelor's degree
and model fine-tuning. Watheq was right.

## Why 91 happened

Not a worse formula. Watheq has no scoring arithmetic at all — `normalizeScore`
rescales a fraction, rounds, and clamps to 0-100, and that is the entire
server-side computation. Its 40/30/15/15 rubric is prompt text and its four
category scores are deliberately decoupled from the number. Watheq scored 67
because it truncates a job description at 5000 characters and therefore read the
whole posting.

This digest read a third of one. The posting is 5187 characters, and
`jd_signal(text, 2000)` sends the window from the first heading that introduces
the work:

| What the posting states | Offset | Inside the 217-2217 window |
|---|---|---|
| Responsibilities: GenAI, RAG, prompt engineering | 217 | yes |
| Minimum Qualifications: bachelor's degree | 3958 | no |
| 4-6 years of data science experience | 4104 | no |
| Model Fine-Tuning, TensorFlow, PyTorch | 4724 | no |

The model scored what it was shown. Three defects compounded it:

- **The grade label lied.** `jd_grade` runs on the stored text, so the prompt
  said `description_kind: "full"` while handing over a third of a posting. Across
  the archive that happened on 48 of the 49 postings whose requirements the
  window cut.
- **The reality check was blind too.** `fit_check` sliced `description[:4000]`,
  which cut mid-word at "Bachelor's degre". It found nothing quotable, returned
  zero blockers, and `ceiling_band` reported "high".
- **The reality check barely ran.** Scoped to jobs already scoring 80+, it
  produced one low ceiling and one high one across 208 archived snapshots.

Measured across the archive at the time of writing:

| Measure | Value |
|---|---|
| Archived postings carrying JD text | 208 |
| Longer than the old 2000-character window | 117 |
| Stating a degree this candidate does not hold | 87 (42%) |
| Of those, scored 80 or above | 29 |
| Of those, scored 90 or above | 10 |

Among the delivered strong matches the degree requirement sat outside the window
for Qualcomm (100), Master Works (96), Jobgether (95) and Ma'aden (91) — four of
the top seven scores in the archive.

## 019A — Finish the run on time (`85f878f`)

Shipped first, deliberately: 019B widens the ranking prompt and runs the reality
check on every delivered job, and both push the run closer to the wall.

The `execute` job carried `timeout-minutes: 10`, and a job timeout is not
graceful — it cancels the job, so the state artifact is never packaged and
`seen.json` never records cards Discord already holds. An `if: always()` step
cannot rescue that, so `digest.py` has to finish on its own.

- `timeout-minutes` 10 to 20. GitHub's own default is 360; the 10 was
  self-imposed.
- `RUN_BUDGET_SECONDS = 600` over the network phase, so the phases sum under the
  wall: 600 + `RANK_BUDGET_SECONDS` 120 + `CHECK_BUDGET_SECONDS` 120 = 840 of
  1200, leaving delivery and persistence unbounded. Checked in the company loop,
  both link-settling passes, before `discover_boards` and before
  `enrich_descriptions`; never at or after delivery.
- `MAX_BOARDS_PER_RUN = 40` on `confirmed_boards`, least-recently-read first. A
  growth backstop, not a ration: it does not bind at 22 confirmed boards.

## 019B — Make the score honest (`f862ef5`)

**What the scorer reads.** `job_archive.jd_requirements` reads requirement-bearing
*lines* out of the whole description rather than a span, because no contiguous
window reaches both ends of that posting and anchoring on the last heading lands
on `Skills` and skips the qualifications above it. `JD_RANK_CHARS` 2000 to 2600
(the median archived posting is 2650). Each job carries `coverage`, computed on
what is actually sent. `fit_check` reads the same two parts from the same
helpers.

| Extraction | Postings losing a stated qualification, of 120 |
|---|---|
| `jd_signal(text, 2000)` | 49 |
| A head-plus-tail two-window slice | 50 |
| `jd_requirements` | 11 |

The two-window row is why this reads lines. `scripts/verify_requirement_recall.py`
replays it over every committed archive and fails below an 88% recall floor.

The rule that an unstated requirement is unknown rather than a gap stays
**unconditional**. Relaxing it on a windowed posting would drop the
anti-invention guard on exactly the 11 postings the extractor still misses.

**The number.** Watheq's band anchors verbatim — 80+ hireable today, 60-79
competitive with gaps, below 60 significant gaps, never above 90 unless every
stated requirement is met with evidence. Four rubric categories reported
alongside, education among them, validated score-within-max and never reconciled
against the total (Watheq documents its models' sums running 30 points above
their stated overall on partial matches).

`fit_check.blocked_score_ceiling` replaces the flat `BLOCKED_SCORE_CAP`:

| Verified blocker | Ceiling |
|---|---|
| any `critical` | 55 |
| any `high` | 69 |
| any `education_certification_gap`, at any severity | 69 |
| any `medium`, or a blocker with no severity | 79 |

Lowest ceiling wins and nothing stacks — three gaps are not three times worse
than the worst of them, and subtraction took this posting to 46, well past
Watheq's 67. `unknown` still caps nothing: silence means no verdict.

The check runs on every delivered job, so `unknown` is now ordinary and the card
says **requirements not checked** rather than rendering like one checked and
found clean. Gaps are named on any card that has them.

**Delivery.** `MIN_DELIVER_SCORE` 50 to 40, because honest scoring moves a
degree-gated role down 15-20 points. A below-floor job gets a seven-day `floor:`
cooldown on both its identity and its description key, rather than the 120-day
state TTL — the archives hold one description scored 40, 40, 40, 45, 50, 50 and
75 unchanged. Three tiers, because `is_strong` needs 80 and every ceiling sits
below it, so two would have shipped a permanently empty "Strong matches" heading.

**Profile.** The blurb states the education fact rather than leaving it to be
inferred. `target_titles` becomes named `tracks`, each **pointing at** the resume
entry to judge it by rather than restating capability — they land inside
`<candidate_preferences>`, which the prompt says proves nothing, so an assertion
there would either do nothing or put 018's defect back.

## What is verified, and what is not

Verified offline, no API key:

- 109 of 120 archived postings that state a qualification have it recovered, up
  from 71 at the old window (`scripts/verify_requirement_recall.py`).
- The real Ma'aden posting's prompt now carries the degree line, the 4-6 years,
  the strict anchors, the education category and the candidate's stated lack of
  a degree, labelled `coverage: windowed`.
- 511 tests across the five modules, plus the Worker suite.

**Not verified**: no model has scored that posting under the new prompt. There is
no `OPENROUTER_API_KEY` in the working copy this was written in. The inputs are
fixed and measured; the resulting number is not yet observed.

## Follow-ups

- Re-score Ma'aden, Qualcomm (100), Master Works (96) and Jobgether (95) with the
  new prompt wherever the key lives, and record where they land.
- Per-job reality-check calls went from roughly two a day to up to twelve. The
  README costs the ranker but not this pass.
- `TestTheCardStatesThePostingAge` is flaky across local midnight: `RECENT` is
  computed at module import from `date.today()`, so a long run that straddles the
  boundary sees "posted 2 days ago". Pre-existing, unrelated to this plan.
