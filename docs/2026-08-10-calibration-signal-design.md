# A click-based calibration signal for the score threshold — design (2026-08-10)

Grounded in the repo as it stands today: `digest.py` sorts jobs with one
number, `STRONG_MATCH_MIN_SCORE = 80` (`digest.py:177`), and the only feedback
on whether that number is right is the operator's own memory, captured weekly
in `docs/weekly-job-review-template.md`. Meanwhile every "Tailor CV" click
already writes a `job_key` to D1 `tasks`, and every delivered job's score and
tier is already archived in `jobs/<date>.json`. This spike asks whether
joining the two can tell the operator something the weekly template can't,
and if so, how small a system can do it.

## What the archive already shows

Three archive files exist today: `jobs/2026-08-08.json` (26 jobs),
`jobs/2026-08-09.json` (12 jobs), `jobs/2026-08-10.json` (12 jobs) — 50
delivered jobs total. `split_tiers` (`digest.py:731-740`) puts a job in
"Strong matches" only if all three hold: `ranking_verified`, `score >= 80`,
and a non-empty JD. Recomputing that rule directly against the archived
fields reproduces the recorded `tier` exactly — the recomputed strong set and
the recorded strong set are the same 25 `job_key`s, byte for byte. That
equality is what makes the counterfactual-threshold analysis below trustworthy
rather than decorative.

**Reason breakdown for the 25 "Worth reviewing" jobs:**

| Reason              | Count |
|----------------------|------:|
| Low score only        | 23 |
| Low score + unverified ranking | 2 |
| Missing JD             | 0 |

Every review-tier job is there for a low score. `missing_jd` is zero across
all 50 jobs (36 have `jd.status: captured`, 14 `incomplete` — both mean the
JD text is non-empty, which is all `split_tiers` checks). The two
unverified-ranking jobs both scored 55, far below any threshold this spike
considers, so unverified ranking never becomes the binding constraint either.
**This inverts the concern the plan opened with**: the worry was that moving
the threshold might change nothing because JD-absence or unverified ranking
dominates the review tier. In this sample it doesn't. Score is the only lever
that currently does anything.

**Threshold sweep, 60–90 step 5** (`blocked_by_other_conditions` = jobs that
clear the score bar but would still be excluded by the other two conditions):

| Threshold | Jobs scoring ≥ threshold | Would be Strong (all 3 conditions) | Blocked by JD/verification |
|-----------|--------------------------:|------------------------------------:|----------------------------:|
| 60 | 39 | 39 | 0 |
| 65 | 36 | 36 | 0 |
| 70 | 34 | 34 | 0 |
| 75 | 30 | 30 | 0 |
| 80 | 25 | 25 | 0 |
| 85 | 20 | 20 | 0 |
| 90 | 8 | 8 | 0 |

`blocked_by_other_conditions` is 0 at every threshold — the cleanest number in
this analysis. It confirms the score gate is the whole story here, but it
also means this data cannot yet say whether 80 specifically is right; it can
only say that moving it would have a real, mechanical effect.

**Score quantization.** All 50 scores are multiples of 5: `{20:1, 30:2, 40:3,
45:1, 50:2, 55:2, 60:3, 65:2, 70:4, 75:5, 80:5, 85:12, 90:2, 95:6}`. A
quarter of the entire sample sits on the single value 85. The threshold is
not a continuous dial: moving it from 80 to 84 changes nothing, 80 to 85 drops
5 jobs, 80 to 75 adds 5. Any click signal built on this data can at best say
which side of a coarse 5-point boundary the clicks fall on — it cannot
discriminate within a bucket, and a quarter of the sample lives in one bucket.

**2026-08-07's zero-strong day.** `jobs/2026-08-07.json` does not exist in
this repository — confirmed by checking the file directly, not inferred.
Why it's absent (never written, or written and later removed) isn't
established from what's on disk today. `stats.json` has the aggregate row:
`avg_score: 55.0, strong:
0, review: 12, matched: 26`. The per-job breakdown (which 12 jobs, what scores)
is not recoverable from this row alone, and it's not established whether
`avg_score` averages over the 26 matched or the 12 delivered — so this design
does not extrapolate a distribution from it. It stands only as: on that day,
every job that passed the hard filters scored under 80.

**A data-quality note that affects methodology, not conclusions:**
`stats.json` has four rows for `2026-08-08` (`strong`/`review` values 0+0,
4+4, 4+4, 4+6, summing to the 12/14 split recorded in `jobs/2026-08-08.json`).
Plan 005 ("Keep one stats row per day so drift measures days") is written to
fix exactly this — `stats.json` is currently one row per *run*, not per day,
and is unmerged. Until it lands, per-day counts in this document and in any
future click-rate reporting should come from `jobs/*.json`, not from summing
or reading a single `stats.json` row per date.

**Is the sample large enough?** No. Three days, 50 delivered jobs, one
operator. The reason-breakdown finding (score is the only binding condition)
is solid because it's a fact about the scoring rule intersected with this
data, not a claim about the right threshold. But no click data exists yet to
compare against any of this — see the next section — so there is nothing to
calibrate against regardless of sample size. The honest state of this spike:
**the dial matters, and there is currently zero information about which way
to turn it.**

## What is not on disk: a click record

`stats.json` has `tailored` and `tailor_failed` columns with nonzero values on
several days (`tailored: 4` on 08-03, `tailor_failed: 8` on 08-05,
`tailor_failed: 10` on 08-06). These are **not** Discord button clicks. In the
current `digest.py`, both call sites that write a stats row hardcode
`tailored=0, tailor_failed=0` (`digest.py:1047`, `digest.py:1139`), and no
other code path in the current file increments those columns — a full grep for
`tailored`/`tailor_failed` turns up only those two hardcoded writes plus
unrelated matches (`TAILOR_DIR`, `tailored_url`, docstring text). So the
nonzero historical values were written by an earlier version of `digest.py`
that no longer exists in this tree; whether that was the auto-tailoring path
plan 007 flags as dead code, or some other prior behavior, is not established
by this grep. What is established: the repository currently holds **no record
of a single Discord button click, anywhere**. There is nothing to backfill.
Whatever this design proposes has to be built forward from zero, and the
sample-size estimate in Step 4 has to start from that.

## Design: the click record

### 1. What is stored

The smallest useful row: `(job_key, clicked_at)`. Nothing else.

- **No user id.** There is exactly one authorized user (the private Discord
  channel gate in `handleInteraction` enforces this already). A user id column
  would carry zero bits of information and would be one more field to justify
  keeping.
- **Nothing from the interaction token.** The interaction token is a
  short-lived credential scoped to responding to that one Discord interaction.
  It has no calibration value and storing any fragment of it turns a harmless
  analytics row into something that needs the same handling as a secret.

### 2. Where it is stored

Three options considered:

| Option | Trade-off |
|---|---|
| New D1 table, own retention | Reuses existing infra (the Worker already writes D1 on every click), no new write path from a new place, retention is a `DELETE` sweep like the ones plan 004 already adds. |
| Append-only file in the repo (like `jobs/`) | Needs the interactive workflow to gain repo write access. `.github/workflows/interactive.yml` currently runs with `permissions: contents: read` (plan 009 confirms — no commit step exists). Granting `contents: write` to a workflow triggered by an arbitrary Discord button press is a real permission escalation on the attack surface a Discord-triggered workflow already has. Rejected for that reason alone. |
| Counter folded into the existing snapshot archive | The archive (`job_snapshots` in D1, `jobs/*.json` on disk) is written once per digest run, by the digest process, not by the interactive workflow. A click happens asynchronously, hours or days after the snapshot was written, so there's no natural moment to fold it in without the digest run reading D1 back — which duplicates the new-table option's read path anyway, for no simplicity gain. |

**Recommendation: a new D1 table**, e.g.:

```sql
CREATE TABLE IF NOT EXISTS click_events (
  job_key TEXT NOT NULL,
  clicked_at TEXT NOT NULL
);
```

No primary key beyond default rowid — a job clicked twice is two data points,
not an error. Written from the same place that already writes a `tasks` row
for a `kind: "tailor"` interaction (`worker/src/index.js:173`), one extra
`INSERT` alongside it.

### 3. How the digest run reads it

No route today reads data back out of D1: the Worker exposes exactly two
routes, `/interactions` (Discord signature-verified) and `/internal/digest`
(HMAC-verified via `internalRequestIsValid`, `worker/src/index.js:129-140`).
Two ways to expose the click data, both reusing that existing HMAC scheme —
no new auth is invented either way:

- **A new route**, e.g. `GET /internal/clicks?since=<ISO8601>`, gated by the
  same `internalRequestIsValid` check already applied at line 297.
- **Fold the summary into the existing `/internal/digest` response body.**
  That endpoint is already called once per digest run and is already
  authenticated and nonce-protected; returning a small
  `{clicked_last_7d: [...]}` payload alongside its existing response adds zero
  routes. The one constraint this option carries: `/internal/digest`'s handler
  posts the run's cards to Discord *during* that same request
  (`worker/src/index.js:299-312`), so a response body it returns arrives after
  the message is already sent — this data is necessarily one run stale, never
  usable for that run's own message.

Given the project's stated bias against complexity, **the fold-in is the
better default**, with its staleness accepted rather than engineered around —
see §5, which scopes the reported line to the weekly review template rather
than the daily digest message, so the one-run lag never matters. A dedicated
route only earns its keep if something needs same-run, fresher-than-one-day
click data, which nothing in this design requires.

### 4. Retention

Plan 004 (status: DONE, reviewed and approved on its own branch, not yet
merged to `main` — confirmed by grepping this tree's `worker/src/index.js` for
`TASK_RETENTION_DAYS`, which finds nothing) proposes
`TASK_RETENTION_DAYS = 7` for `tasks` rows, because a `tasks` row holds an
encrypted Discord interaction token — a credential with no reason to be kept
past its 24-hour usefulness window. `click_events` holds neither a credential
nor user content, so it is not bound by that constraint. The relevant
precedent is the other retention window the same plan sets:
`DELIVERY_RETENTION_DAYS = 120`, chosen to match `ARCHIVE_TTL_DAYS` in
`job_archive.py` (plan 003, same not-yet-merged status) and `STATE_TTL_DAYS`
in `digest.py` — all three are already agreed, in `plans/README.md`'s
dependency notes, to stay aligned at 120 days once both land.

**Recommendation: 120 days**, for the same reason `deliveries` and
`job_snapshots` are designed to use it — the join partner. A click record can
only ever be interpreted against the `jobs/*.json` snapshot of the job it
points at. `jobs/*.json` will be pruned at 120 days once plan 003 merges; a
click row surviving longer than its join partner is a row that can never be
interpreted again. Keeping `click_events` at 120 days costs nothing (it's a
two-field row, "two orders of magnitude smaller per day" than `jobs/*.json`,
per the plan's own note) and keeps the retention story for every D1 table
legible as one rule: **keep non-credential rows as long as their join partner
survives; keep credential rows only as long as they're useful.**

### 5. What is reported, and when

Not a dashboard — the project has no service beyond the daily Action and this
design should not be the thing that adds one. Smallest surface: one line
appended to the weekly review template's "Calibration decision" section
(not the daily digest message — see the staleness note in §3, which this
scoping sidesteps entirely), e.g.:

> Clicked 3 of 12 delivered in the last 7 days. Median clicked score 84,
> median unclicked 72.

Computed from `click_events` joined to the score already known to the digest
run for jobs delivered in the last 7 days (using `jobs/*.json`, not
`stats.json`, per the per-day-denominator note above until plan 005 lands).
No new scheduled job — this is computed inline during the digest run, which
already runs daily, and simply carried into the weekly artifact rather than
the daily one.

## The honest objections

**A click is not an application.** A click on "Tailor CV" measures curiosity
— the operator was willing to spend zero effort to see a tailored draft.
That's weaker than applying, weaker than the operator's own weekly note ("useful
/ noise / inaccessible"), and weaker than an offer. This signal can support
*"the operator's attention correlates with score X"* — a directional nudge.
It cannot support *"jobs above score X are worth applying to"* — that claim
needs the weekly template's judgment, which this design does not replace.

**Sample size.** At roughly 10-ish jobs delivered a day and a click rate that
could easily be under 20% (curiosity is cheap but not free), a week produces
maybe 5-15 clicks total — not enough to compare a clicked-score median against
an unclicked-score median with any confidence, especially given the score
quantization noted above (12 of 50 jobs already sit on one value, 85).
Recommendation: report the raw counts and medians every week starting from
day one (cheap, harmless, informative even as a trend line), but do not treat
any single week's median-score gap as actionable. A reasonable bar before
treating the gap as worth discussing: at least 4-6 weeks of accumulation, and
even then treat it as one input alongside the manual template, not a
trigger on its own.

**Feedback loop risk.** If the threshold ever moved automatically toward
whatever gets clicked, the system would narrow toward what it already shows
the operator — a closed loop that never surfaces anything outside the current
comfort zone. The mitigation is the same principle the README already states
for the manual template: **the workflow never changes selection criteria on
its own.** This design reports evidence (counts, medians) as an extra line in
existing surfaces; nothing in it writes to `STRONG_MATCH_MIN_SCORE` or any
other selection constant. The click record is read-only input to a human
decision, structurally identical in that respect to the weekly template it
sits beside.

**The manual template already exists.** `docs/weekly-job-review-template.md`
remains the authoritative feedback mechanism — this design does not replace
it or deprecate it. The honest relationship: the click line is a *prompt*,
not a substitute. "Clicked 3 of 12, median clicked score 84 vs. unclicked 72"
is a fact the operator can glance at when deciding whether to fill in the
template's "Keep or change `STRONG_MATCH_MIN_SCORE`" line at all, especially
on a week where nothing felt notable enough to prompt filling in the template
unprompted. It answers a question the template can only estimate from memory;
it does not answer whether the threshold should move.

## What this does not do

- It does not read or query the live D1 database — no such call was made to
  produce this document.
- It does not touch `digest.py`, `worker/src/index.js`, `worker/schema.sql`,
  or `job_archive.py`. No production file changed.
- It does not change `STRONG_MATCH_MIN_SCORE`. It recommends collecting
  evidence toward that decision; the decision stays the operator's.
- It does not build a data pipeline, a dashboard, a metrics service, or a new
  scheduled job. The only new infrastructure proposed is one D1 table and
  either one new HMAC-gated route or one extra field on an existing response.
- It does not backfill click history — none exists. The `tailored` /
  `tailor_failed` columns in `stats.json` are leftover from a dead
  auto-tailoring path (plan 007), not click data.
- It does not recommend a threshold value. The data available today shows
  score is the only condition currently gating the review tier in this
  sample, which tells the operator the dial is live and worth watching — it
  says nothing about which way to turn it, because no click ever happened
  yet to compare against.
