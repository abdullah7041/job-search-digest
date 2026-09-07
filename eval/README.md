# Ranker gold set

A frozen set of jobs with expected score bands, so a later prompt edit cannot
quietly walk back plan 018A's honesty fix.

## Why this exists

`check_drift` (`digest.py:337-339`) compares the daily average against its own
recent history. That catches a cliff and misses a slope: if scores inflate a
little every week, the baseline inflates with them and the detector never fires.
It also cannot say *which* job was scored wrong, only that the average moved.

The gold set is the opposite instrument. Fixed jobs, fixed expectations, and a
verdict per job.

## Running it

```bash
python -m unittest test_eval.py          # the scorer, offline, no key
OPENROUTER_API_KEY=sk-... python eval/run_eval.py --runs 3   # the model, live
```

`test_eval.py` runs in **`ci.yml` only**. It is deliberately NOT in the
preflight step of `digest.yml` or `interactive.yml`: those guard a live run
against broken code, and a malformed fixture should never be able to block the
daily digest or the Discord path. `run_eval.py` makes live calls and is in no
suite at all.

Every fixture goes into **one** `rank_jobs` call, because that is how production
scores a day's jobs. Grading them one at a time would measure something the
digest never does.

`chat_json` sends `seed=7` and no temperature (`openrouter.py:94-99`), so a run
is reproducible-ish, **not deterministic**. Use `--runs 3` before concluding
anything, and treat a move inside the spread as noise.

## The fixtures

Measured live against `google/gemini-3.7-flash` on 2026-09-07, 3 runs each.
**Spread was 0 on every fixture** — `seed=7` with no temperature was fully
reproducible for this set.

| Fixture | Band | Scored | What it catches |
|---|---|---|---|
| `floor-aml-officer` | 0–39 | **0** | Wrong function entirely. Inflating it is the worst case, because `drop_below_floor` only fires under 50. |
| `inflation-listed-only-skills` | 0–50 | **42** | Every requirement is in `resume.md`'s `## SKILLS` line and in no experience bullet. Its reason now reads *"Skills only listed; lacks demonstrated enterprise RAG…"* — the ported rule, working verbatim. |
| **`hungerstation-senior-data-engineer-i`** | 35–68 | **45** | **The case this pass exists for.** Delivered at fit 85; Watheq scored 58. |
| `hungerstation-with-benefits-boilerplate` | 35–68 | **45** | Identical requirements plus *"relocation support globally"*. Scored identically — the boilerplate did not move it. |
| `inflation-genai-credentialed` | 40–72 | **48** | Archived at 95. Requires a Master's, 5 years ML, PyTorch, Docker/K8s, Java/Ruby — none evidenced. |
| `inflation-omniops-solution-engineer` | 40–72 | **52** | Archived at 85. Its own old reason admitted presales was "lighter" and never mentioned the required CS degree. |
| `ceiling-senior-ai-product-engineer` | 80–100 | **98** | **The counterweight.** Requirements the resume genuinely demonstrates. Proves 018A rewards evidence rather than just deducting for gaps. |

### Two fixtures were wrong, and measurement is what found them

Both were caught by the first live run, and both are recorded rather than
quietly fixed — a gold set that gets edited until it passes is worthless.

- **The original ceiling was not a ceiling.** `ceiling-senior-genai-engineer`
  was a real posting archived at **95**, and its band `[75, 100]` came from that
  number. Live, it scored **55**. Reading its Qualifications explained why: a
  Master's in CS/AI/DS, a *minimum* of 5 years professional AI/ML,
  PyTorch/TensorFlow, transformers, Java, Ruby, Docker and Kubernetes — the
  resume evidences none of them. **The archived 95 was itself inflation**, so
  the band had been calibrated against the very disease 018A cures. It is now
  `inflation-genai-credentialed`, banded `[40, 72]` — *tighter* than before, not
  wider.
- **The listed-only fixture was testing the wrong path.** Its JD was 484
  characters, which `jd_grade` calls `incomplete`, and the prompt tells the model
  to judge those on title and function alone. It scored 65 and its reason opened
  *"Incomplete posting"* — the demonstrated-vs-listed rule never ran. The JD is
  now long enough to grade `full`.

The replacement ceiling's band is **probed, not assumed**: that exact JD was run
through 018A before being frozen, and scored 98.

## Grading

`eval/score.py`, weighted, with one hard gate:

- **`score_band`** (weight 3) — 1.0 inside the band, decaying to 0 across 20
  points. The number that reaches the card and decides the tier.
- **`reason_gap`** (weight 1) — does `why` name at least one expected gap? The
  reason is capped at 15 words, so naming one is the bar.
- **`blockers`** (weight 2) — graded when the result carries typed blockers,
  which `run_eval.py --check` supplies by running plan 018B's reality pass. The
  denominator is capped at `fit_check.MAX_BLOCKERS`: a fixture may name more
  expected gaps than the system can ever report, and scoring against the longer
  list makes full credit unreachable.
- **`must_not_credit`** — a **hard failure**, not a weighted category, matching
  Watheq's treatment of keyword-stuffing fixtures. Presenting an unproven skill
  as a strength fails the fixture whatever the number did. Graded against typed
  strengths, which `run_eval.py --check` now supplies.

  It first checked `why`, and that was wrong. `why` is 15 words, and this is a
  claim about *framing*, not presence: "lacks data modeling" and "strong data
  modeling" carry the same term. The two lists also legitimately overlap — the
  HungerStation posting requires data modeling and `resume.md` carries it only
  in `## SKILLS`, so it is at once a gap the ranker should name and a skill it
  must not credit. A correct answer was failing the fixture. Until 018B, the
  **tight `score_band` is what defends these fixtures**: a ranker that credits
  listed-only skills scores them high and misses the band.

A fixture passes at **0.8**, and that number is arithmetic, not taste. With only
two categories live and weights 3 and 1, a fixture lands exactly two ways: in
band but naming no gap (0.75), or naming a gap while out of band (0.25). A 0.7
threshold would pass the first, making `reason_gap` decorative and letting an
inflated-but-vague ranker through the fixture built to catch it. At 0.8, a
fixture that expects gaps must both land in band **and** name one. Fixtures that
expect no gaps (the ceiling) are judged on band alone.
`test_eval.py::TestThresholdArithmetic` pins this.

Groups sharing an `invariance_group` must land within 8 points of each other.
Same idea as Watheq's `BAND_CROSS_TOLERANCE`: identical requirements should
score identically, but seed-only reproducibility means a small spread is noise.

## Two modes, and what each can grade

Without `--check` the runner sees only `{score, why}`, so `blockers` and
`must_not_credit` are skipped and the verdict rests on `score_band` plus
`reason_gap`. That is the cheap mode: one model call for the whole set.

With `--check` it also runs plan 018B's reality pass, one extra call per
fixture, and both dormant categories switch on. Use it when changing
`fit_check.py` or the evidence gate; the cheap mode is enough for a prompt or
rubric change.

Note the threshold interaction, documented at `run_eval.py`'s `PASS_THRESHOLD`:
under `--check`, in-band with correct blockers and a vague `why` scores 0.833
and passes, so `--check` no longer forces the reason to name a gap. Typed
blockers are better evidence than fifteen words of prose, but it is a real
loosening.

## Adding a fixture

Freeze the JD text into the fixture rather than pointing at `jobs/*.json` — the
archive prunes on a 120-day TTL and a fixture must outlive it. Say in
`description` **what regression it catches**, not what the job is. A fixture
that cannot fail is worse than no fixture, because it reads as coverage.
