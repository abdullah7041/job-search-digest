# Plan 005: Keep one stats row per day so drift detection measures days

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- digest.py test_digest.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first and confirm
> `git rev-parse --short HEAD` prints `6c7541e` or later.
>
> **Concurrent work warning**: an uncommitted worktree at
> `.worktrees/discord-tailoring` also modifies `digest.py` (near line 700).
> Your change is at lines 920–955 and should not conflict textually, but
> coordinate with the operator before merging either branch. If plan 002 has
> already landed, `save_stats` will have gained a `delivered=` metric — that is
> compatible with this change.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

`stats.json` exists to make a silently-stale profile visible as a trend, per
the module docstring: "stats.json holds one row per run (fetched/matched/new/avg
fit score) so a silently-stale profile shows up as a trend instead of looking
healthy."

`check_drift` then compares the trailing 7 **rows** against the 7 rows before
them and warns if average fit dropped by 15 points or more.

The problem is that a run is not a day. `save_stats` appends unconditionally,
and the workflow has a manual `workflow_dispatch` button, so re-running the
digest writes another row for the same date. The live `stats.json` already
shows this — four separate rows dated `2026-08-08`:

```json
{"avg_score": null, "date": "2026-08-08", "fetched": 184, "matched": 13, "new": 0, ...}
{"avg_score": 70.6, "date": "2026-08-08", "fetched": 214, "matched": 21, "new": 8, ...}
{"avg_score": 76.9, "date": "2026-08-08", "fetched": 214, "matched": 21, "new": 8, ...}
{"avg_score": 68.0, "date": "2026-08-08", "fetched": 214, "matched": 23, "new": 10, ...}
```

Three of those carry a score, so a single day of testing consumed three of the
seven slots in the "trailing week" window. The consequences are concrete:

- A day with unusual scores that happened to be re-run several times dominates
  a window it should contribute one row to.
- `DRIFT_MIN_SCORED_RUNS = 14`, the guard meant to mean "two full weeks of
  history", is reached in fewer than 14 days.
- The 120-day `STATS_TTL_DAYS` prune bounds the file by date, so duplicates
  also inflate it beyond the intended ~120 rows.

After this plan, a re-run replaces that day's row instead of appending, and
`check_drift` defensively collapses any duplicates already in the file so the
existing history stops skewing the window without anyone editing `stats.json`
by hand.

## Current state

Files involved:

- `digest.py` — `save_stats` at lines 920–932, `check_drift` at lines 935–955.
- `test_digest.py` — offline regression tests.
- `stats.json` — live state, committed by the workflow. **Not to be edited.**

`digest.py:920-932` exactly as it exists today:

```python
def save_stats(stats, today, fetched, matched, new, avg_score, **metrics):
    """Append today's run stats and prune rows older than STATS_TTL_DAYS.
    avg_score is None on a no-new-jobs day (nothing was scored)."""
    row = {
        "date": today, "fetched": fetched, "matched": matched,
        "new": new, "avg_score": avg_score,
    }
    row.update(metrics)
    stats.append(row)
    cutoff = (datetime.date.today() - datetime.timedelta(days=STATS_TTL_DAYS)).isoformat()
    pruned = [s for s in stats if s["date"] >= cutoff]
    with open(STATS_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)
```

`digest.py:935-955` exactly as it exists today:

```python
def check_drift(stats):
    """Compare the trailing DRIFT_WINDOW_DAYS avg fit score against the prior
    window of the same size, using history recorded BEFORE today's run. Returns
    a warning string, or None if there isn't enough history yet or fit hasn't
    dropped past the threshold. Fail-soft: never raises, never blocks delivery."""
    scored = sorted(
        (s for s in stats if s.get("avg_score") is not None),
        key=lambda s: s["date"],
    )
    if len(scored) < DRIFT_MIN_SCORED_RUNS:
        return None
    recent = scored[-DRIFT_WINDOW_DAYS:]
    prior = scored[-2 * DRIFT_WINDOW_DAYS:-DRIFT_WINDOW_DAYS]
    recent_avg = sum(s["avg_score"] for s in recent) / len(recent)
    prior_avg = sum(s["avg_score"] for s in prior) / len(prior)
    if prior_avg - recent_avg >= DRIFT_SCORE_DROP_THRESHOLD:
        return (
            f"Fit trending down ({DRIFT_WINDOW_DAYS}d avg {recent_avg:.0f} vs "
            f"prior {prior_avg:.0f}) — profile may be stale. Check profile.json."
        )
    return None
```

The relevant constants, `digest.py:195-199`:

```python
STATS_FILE = "stats.json"
STATS_TTL_DAYS = 120
DRIFT_WINDOW_DAYS = 7          # trailing window compared against the prior one
DRIFT_MIN_SCORED_RUNS = 14     # need two full windows before trusting a comparison
DRIFT_SCORE_DROP_THRESHOLD = 15  # point drop (7d avg vs prior 7d avg) that triggers a warning
```

Call sites of `save_stats` in `main()` — there are two, and both must keep
working unchanged:

- `digest.py:1044-1048` — the quiet-day heartbeat path, passes `avg_score=None`.
- `digest.py:1136-1140` — the normal delivery path.

Repo conventions that apply here:

- `digest.py` uses no type annotations and no dataclasses. Plain dicts and
  plain functions. Match that — do not introduce typing here.
- Comments explain *why*, often citing the date a bug was observed
  (e.g. "The 2026-07-31 digest scored Lead/Staff roles 85-98 because…"). When
  you add the dedupe comment, name the concrete cause: repeated
  `workflow_dispatch` runs on 2026-08-08.
- `json.dump(..., indent=0, sort_keys=True)` is the established on-disk format
  for both state files. Do not change it.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Install      | `pip install -r requirements.txt`                         | exit 0                        |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0 (see step 0)     |
| Worker tests | `cd worker && node --test`                                | 0 fail                        |

## Scope

**In scope** (the only files you should modify):

- `digest.py` — only `save_stats` and `check_drift`
- `test_digest.py`

**Out of scope** (do NOT touch, even though they look related):

- `stats.json` — live state committed by the workflow. Do **not** hand-edit it
  to remove the existing duplicates. The `check_drift` change in step 2 makes
  the existing history read correctly, and the next run's `save_stats` will
  collapse the duplicate 2026-08-08 rows on its own.
- `seen.json` and `save_seen` — a different file with a different shape; it is
  a dict keyed by id, so it has no duplicate problem.
- The drift thresholds (`DRIFT_WINDOW_DAYS`, `DRIFT_MIN_SCORED_RUNS`,
  `DRIFT_SCORE_DROP_THRESHOLD`). Tuning them is a judgment call for the
  operator, not part of this fix.

## Git workflow

- Branch: `advisor/005-one-stats-row-per-day`
- Commit style: Conventional Commits.
  Suggested message: `fix: keep one stats row per date so drift measures days`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 0: Record the baseline test count

Other plans in `plans/` also add tests to this same suite, and you may be
running after one of them. Do not assume the count is 45.

```bash
python -m unittest test_digest.py test_interactive.py 2>&1 | tail -3
```

Record the number after `Ran`. Call it **N**. This plan's net change is
**+4 tests**.

**Verify**: the run ends in `OK`. If it does not, STOP — the baseline is red.

### Step 1: Make save_stats replace the day's row

Replace `digest.py:920-932` with:

```python
def save_stats(stats, today, fetched, matched, new, avg_score, **metrics):
    """Record today's run and prune rows older than STATS_TTL_DAYS.
    avg_score is None on a no-new-jobs day (nothing was scored).

    One row per DATE, not per run. The workflow has a manual dispatch button,
    and 2026-08-08 was re-run four times, so appending gave a single day three
    of the seven slots in check_drift's trailing window. A re-run now replaces
    that day's row."""
    row = {
        "date": today, "fetched": fetched, "matched": matched,
        "new": new, "avg_score": avg_score,
    }
    row.update(metrics)
    cutoff = (datetime.date.today() - datetime.timedelta(days=STATS_TTL_DAYS)).isoformat()
    pruned = [s for s in stats if s["date"] >= cutoff and s["date"] != today]
    pruned.append(row)
    pruned.sort(key=lambda s: s["date"])
    with open(STATS_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)
```

Note what changed: rows matching `today` are filtered out before the new row is
appended, and the result is sorted by date so the file stays chronological even
after a replacement. The TTL prune and the on-disk format are unchanged.

**Verify**: `python -m unittest test_digest.py` → `OK`. If any existing test
fails here, it is asserting the append behavior — STOP and report which test,
rather than weakening it.

### Step 2: Make check_drift collapse duplicates defensively

Replace the `scored = sorted(...)` block at the start of `check_drift` with a
version that keeps only the last row per date:

```python
def check_drift(stats):
    """Compare the trailing DRIFT_WINDOW_DAYS avg fit score against the prior
    window of the same size, using history recorded BEFORE today's run. Returns
    a warning string, or None if there isn't enough history yet or fit hasn't
    dropped past the threshold. Fail-soft: never raises, never blocks delivery.

    Collapses any duplicate dates already on disk (written before save_stats
    enforced one row per date) so an old, heavily re-run day cannot fill the
    window on its own."""
    by_date = {}
    for s in stats:
        if s.get("avg_score") is not None:
            by_date[s["date"]] = s
    scored = sorted(by_date.values(), key=lambda s: s["date"])
    if len(scored) < DRIFT_MIN_SCORED_RUNS:
        return None
    recent = scored[-DRIFT_WINDOW_DAYS:]
    prior = scored[-2 * DRIFT_WINDOW_DAYS:-DRIFT_WINDOW_DAYS]
    recent_avg = sum(s["avg_score"] for s in recent) / len(recent)
    prior_avg = sum(s["avg_score"] for s in prior) / len(prior)
    if prior_avg - recent_avg >= DRIFT_SCORE_DROP_THRESHOLD:
        return (
            f"Fit trending down ({DRIFT_WINDOW_DAYS}d avg {recent_avg:.0f} vs "
            f"prior {prior_avg:.0f}) — profile may be stale. Check profile.json."
        )
    return None
```

Later rows win, because `dict` assignment overwrites and `stats` is read in
file order. Everything below `scored = ...` is unchanged.

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N tests (no tests added yet).

### Step 3: Add tests

Add a new test class to `test_digest.py`. Place it after the existing
`TestTierClassification` class and before `TestEndToEnd`. These tests call
`save_stats` and `check_drift` directly; `save_stats` writes to
`digest.STATS_FILE` in the current working directory, and `test_digest.py`
already `chdir`s into a temp directory at import time (see the module header),
so no extra isolation is needed — but write into a `TemporaryDirectory` anyway
so the tests do not leave state behind for each other.

```python
class TestStatsRows(unittest.TestCase):
    def _rows(self, work):
        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_rerunning_the_same_day_replaces_that_days_row(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            stats = []
            digest.save_stats(stats, "2026-08-08", 200, 20, 5, 70.0)
            stats = digest.load_stats()
            digest.save_stats(stats, "2026-08-08", 214, 21, 8, 76.9)
            rows = digest.load_stats()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["avg_score"], 76.9)
        self.assertEqual(rows[0]["fetched"], 214)

    def test_a_different_day_is_appended_and_history_stays_sorted(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            stats = []
            digest.save_stats(stats, "2026-08-09", 214, 21, 8, 68.3)
            stats = digest.load_stats()
            digest.save_stats(stats, "2026-08-08", 214, 21, 8, 76.9)
            rows = digest.load_stats()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        self.assertEqual([row["date"] for row in rows], ["2026-08-08", "2026-08-09"])

    def test_drift_counts_days_not_runs(self):
        # 14 distinct scored days are required before a comparison is trusted.
        # 13 days plus a duplicate must NOT be enough.
        rows = [{"date": f"2026-07-{day:02d}", "avg_score": 80.0} for day in range(1, 14)]
        rows.append({"date": "2026-07-13", "avg_score": 20.0})
        self.assertIsNone(digest.check_drift(rows))

    def test_drift_warns_on_a_real_multi_day_drop(self):
        rows = [{"date": f"2026-07-{day:02d}", "avg_score": 85.0} for day in range(1, 8)]
        rows += [{"date": f"2026-07-{day:02d}", "avg_score": 60.0} for day in range(8, 15)]
        warning = digest.check_drift(rows)
        self.assertIsNotNone(warning)
        self.assertIn("profile may be stale", warning)
```

`shutil` and `tempfile` are already imported at the top of `test_digest.py`.

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N+4 tests.

## Test plan

- New tests in `test_digest.py`, class `TestStatsRows`:
  - `test_rerunning_the_same_day_replaces_that_days_row` — the core fix.
  - `test_a_different_day_is_appended_and_history_stays_sorted` — the normal
    path is unaffected and ordering is maintained.
  - `test_drift_counts_days_not_runs` — a duplicate date does not help reach
    `DRIFT_MIN_SCORED_RUNS`.
  - `test_drift_warns_on_a_real_multi_day_drop` — the detector still fires on a
    genuine drop; guards against neutering it.
- Structural pattern: `TestTierClassification` in the same file for the class
  shape, and `TestEndToEnd`'s `os.chdir` + `shutil.rmtree` pattern for temp
  directory handling.
- Verification: `python -m unittest test_digest.py test_interactive.py` → all
  pass, N+4 tests.

## Done criteria

ALL must hold:

- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with N+4
      tests, where N is the count recorded in step 0
- [ ] `cd worker && node --test` still passes with 0 fail and the same
      count as before your change (this plan does not touch the Worker suite)
- [ ] `grep -n "stats.append(row)" digest.py` returns no matches
- [ ] `git status --porcelain` lists only `digest.py` and `test_digest.py` —
      **`stats.json` must not appear**
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `digest.py:920-955` does not match the excerpts in "Current state".
- `python -m unittest test_digest.py test_interactive.py` fails before you make
  any change.
- An existing test asserts that two rows with the same date can coexist. That
  would mean the append behavior is intentional and this plan's premise is
  wrong — report it instead of deleting the test.
- You are tempted to rewrite `stats.json` to remove the existing duplicates.
  Do not. Step 2 handles them at read time.

## Maintenance notes

- `check_drift` now tolerates duplicates permanently, not just during the
  transition. Keep that: it is one line of defensiveness that makes the
  detector independent of how `save_stats` behaves.
- The duplicate 2026-08-08 rows will disappear from `stats.json` naturally —
  either the next run on that date replaces them, or the 120-day TTL prunes
  them. Neither requires action.
- If a future change makes the digest legitimately run more than once a day
  with different results worth keeping (for example a second, evening run), this
  "one row per date" rule is the thing to revisit first — the key would need to
  become a timestamp, and `check_drift` would need to bucket by date explicitly.
- Reviewer should scrutinize: that the TTL prune still runs, that the on-disk
  format (`indent=0, sort_keys=True`) is unchanged, and that `stats.json` is not
  in the diff.
