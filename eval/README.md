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

`test_eval.py` runs in CI and needs no key. `run_eval.py` makes live calls and
is deliberately not in any suite.

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
| `inflation-listed-only-skills` | 0–60 | Every requirement appears in `resume.md`'s `## SKILLS` line and in **no** experience bullet. Catches a regression back to scoring the blurb. |
| `ceiling-senior-genai-engineer` | 75–100 | **The counterweight.** 018A lowers scores by design; without a fixture that must stay high, the set would only punish optimism and an over-correction would pass clean. |

Verified to discriminate, not assumed: a ranker returning 88 for everything
passes 1 of 6; one returning 30 for everything passes 4 of 6 and fails the
ceiling; and moving only the boilerplate twin trips the invariance check.

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
  as a strength fails the fixture whatever the number did.

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
