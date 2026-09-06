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

| Fixture | Band | What it catches |
|---|---|---|
| `floor-aml-officer` | 0–39 | Wrong function entirely (compliance). Inflating this is the most dangerous regression, because `drop_below_floor` only fires below 50. |
| `hungerstation-senior-data-engineer-i` | 35–68 | **The case this pass exists for.** Delivered at fit 85; Watheq scored it 58. Every missing qualification is stated in the posting. |
| `hungerstation-with-benefits-boilerplate` | 35–68 | Identical requirements plus the Additional Information block, which says *"We offer relocation support globally"*. The rubric's 0–39 band names relocation, so a model reading a benefit as a location tanks a valid Riyadh role. |
| `inflation-omniops-solution-engineer` | 40–72 | Archived at 85. Its own reason admits presales experience is "lighter" and never mentions the required CS degree. |
| `inflation-listed-only-skills` | 0–50 | Every requirement appears in `resume.md`'s `## SKILLS` line and in **no** experience bullet. Catches a regression back to scoring the blurb. |
| `ceiling-senior-genai-engineer` | 75–100 | **The counterweight.** 018A lowers scores by design; without a fixture that must stay high, the set would only punish optimism and an over-correction would pass clean. |

Verified to discriminate, not assumed — by feeding the scorer deliberately
broken rankers:

| Injected ranker | Passes |
|---|---|
| Inflated and confident (88 everywhere, "skills align well") | **1 of 6** — only the ceiling, which is where 88 belongs |
| Inflated but vague (55 everywhere, names no gap) | **0 of 6** |
| Over-corrected into pessimism (30 everywhere, gaps named) | **4 of 6** — the ceiling and OmniOps fail, the two that must stay high |
| Correct (in band, names a gap) | **6 of 6** |
| Correct but boasting a forbidden strength (018B shape) | **3 of 6** — the hard gate bites |
| Boilerplate moves only the noisy twin | invariance fires, spread 35 > 8 |

The "correct" row matters as much as the first: a set no correct ranker can pass
is not a gold set, it is a ratchet. It was 4 of 6 during development, because
`must_not_credit` was being checked against `why` and failed a ranker for
naming a required gap. That is the bug the Grading section below describes.

The pessimism row is the weakest of the six, and honestly so: a blanket 30 sits
only 5 points under the HungerStation band, which the decay curve correctly
treats as a near miss rather than a failure. The ceiling fixture is what makes
that row bite at all.

## Grading

`eval/score.py`, weighted, with one hard gate:

- **`score_band`** (weight 3) — 1.0 inside the band, decaying to 0 across 20
  points. The number that reaches the card and decides the tier.
- **`reason_gap`** (weight 1) — does `why` name at least one expected gap? The
  reason is capped at 15 words, so naming one is the bar.
- **`blockers`** (weight 2) — **not graded yet.** Switches on automatically once
  plan 018B emits typed blockers.
- **`must_not_credit`** — a **hard failure**, not a weighted category, matching
  Watheq's treatment of keyword-stuffing fixtures. Presenting an unproven skill
  as a strength fails the fixture whatever the number did. Graded against typed
  strengths, so like `blockers` it is **inert until 018B**.

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

## What this cannot grade today

Watheq grades `strongMatches` / `missingKeywords` arrays
(`eval/match-score.mjs`). This ranker returns `{score, why}` and nothing else,
so those arrays do not exist to grade. The `blockers` category is the slot
018B fills; until then the set leans on the band, which is the part that
actually reaches the reader.

## Adding a fixture

Freeze the JD text into the fixture rather than pointing at `jobs/*.json` — the
archive prunes on a 120-day TTL and a fixture must outlive it. Say in
`description` **what regression it catches**, not what the job is. A fixture
that cannot fail is worse than no fixture, because it reads as coverage.
