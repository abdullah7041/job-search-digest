# Plan 003: Prune the daily job snapshot archive on a TTL

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- job_archive.py test_interactive.py .github/workflows/digest.yml`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first and confirm
> `git rev-parse --short HEAD` prints `6c7541e` or later.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

Every daily run writes `jobs/<date>.json` containing a full snapshot of each
delivered job, including the complete job-description text (up to 6,000
characters per job, up to 25 jobs). The GitHub Actions workflow commits that
file back to the repository on every run.

Nothing ever deletes these files. Two days of real output already occupy
190 KB (`jobs/2026-08-08.json` is 130.6 KB, `jobs/2026-08-09.json` is 59.7 KB).
At that rate the archive adds roughly 25–35 MB per year to the working tree,
and considerably more to git history because each daily commit stores a new
blob. A repository that is otherwise a single Python file plus a small Worker
becomes dominated by machine-generated JSON.

This is inconsistent with how the project treats its other two state files —
both already prune on a 120-day TTL:

- `digest.py:190` — `STATE_TTL_DAYS = 120  # prune seen ids older than this to bound file size`
- `digest.py:196` — `STATS_TTL_DAYS = 120`

The archive exists so that a "Tailor CV" button clicked days after a card was
posted still resolves to the exact job description that was captured. Once a
job's id has aged out of `seen.json` after 120 days, retaining its snapshot
serves nothing. Matching the existing TTL keeps one number to reason about.

## Current state

Files involved:

- `job_archive.py` — writes the snapshots. `write_snapshots()` is the only
  writer; it is called once per run from `digest.py:1078`.
- `test_interactive.py` — contains `TestJobArchive`, the tests for this module.
- `.github/workflows/digest.yml` — commits `jobs/` back to the repository.

`job_archive.py:100-129` exactly as it exists today:

```python
def write_snapshots(jobs: list[dict], date: str, archive_dir: str = "jobs") -> str:
    """Append new same-day evidence without replacing an existing job snapshot."""
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"{date}.json")
    existing_jobs = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
        if existing.get("schema_version") != 1 or existing.get("date") != date:
            raise ValueError(f"Existing snapshot archive {path} has an incompatible schema or date.")
        existing_jobs = existing.get("jobs") or []

    merged = list(existing_jobs)
    existing_keys = {snapshot.get("job_key") for snapshot in existing_jobs}
    for job in jobs:
        snapshot = make_snapshot(job, f"{date}T00:00:00Z")
        if snapshot["job_key"] not in existing_keys:
            merged.append(snapshot)
            existing_keys.add(snapshot["job_key"])
    archive = {
        "schema_version": 1,
        "date": date,
        "jobs": merged,
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(archive, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return path
```

Note the append-not-replace behavior and the atomic `.tmp` + `os.replace`
write. Both must be preserved — there is a test for the first
(`test_write_snapshots_appends_new_jobs_without_mutating_existing_same_day_evidence`).

The top of `job_archive.py` (the constants block you will extend):

```python
JD_MIN_CHARS = 400
JD_MIN_WORDS = 60
RESPONSIBILITY_MARKERS = ("responsibil", "you will", "duties", "about the role")
REQUIREMENT_MARKERS = ("requirement", "qualification", "experience", "skills")
```

The workflow step that commits the directory, `.github/workflows/digest.yml`:

```yaml
      - name: Persist state and immutable job snapshots
        run: |
          if [[ -n "$(git status --porcelain -- seen.json stats.json jobs/)" ]]; then
            git config user.name "github-actions"
            git config user.email "actions@github.com"
            git add -- seen.json stats.json jobs/
            git commit -m "update state and job snapshots [skip ci]"
            git push
          else
            echo "no state change"
          fi
```

`git add -- jobs/` stages deletions as well as additions (this has been git's
behavior since 2.0), and `git status --porcelain -- jobs/` reports deletions,
so the existing workflow will commit prunes with no change. **Do not modify the
workflow.** Verify this claim in step 4 rather than assuming it.

The existing prune this should mirror, `digest.py:901-905`:

```python
def save_seen(seen):
    cutoff = (datetime.date.today() - datetime.timedelta(days=STATE_TTL_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(pruned, f, indent=0, sort_keys=True)
```

Repo conventions that apply here:

- `job_archive.py` uses `from __future__ import annotations` and full type
  annotations on every function. Match that — `digest.py` does not use
  annotations, but this module does.
- Every public function has a one-line docstring written as a statement of
  intent, not a restatement of the signature. Match the tone: "Append new
  same-day evidence without replacing an existing job snapshot."
- Date strings are ISO `YYYY-MM-DD` throughout, and files are named
  `<date>.json`, so lexicographic comparison is date comparison. `save_seen`
  above relies on exactly this.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Install      | `pip install -r requirements.txt`                         | exit 0                        |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0 (see step 0)     |
| Single class | `python -m unittest test_interactive.TestJobArchive`      | `OK`                          |
| Worker tests | `cd worker && node --test`                                | 0 fail                        |

## Scope

**In scope** (the only files you should modify):

- `job_archive.py`
- `test_interactive.py` — only `TestJobArchive`

**Out of scope** (do NOT touch, even though they look related):

- `.github/workflows/digest.yml` — `git add -- jobs/` already handles
  deletions. Changing it is unnecessary risk.
- `digest.py` — an uncommitted worktree at `.worktrees/discord-tailoring` is
  modifying that file right now. `write_snapshots` is already called from
  there; the prune goes inside `write_snapshots`, so `digest.py` needs no edit.
- The existing `jobs/*.json` files. Do **not** delete any of them by hand. The
  TTL is 120 days and the oldest file is from 2026-08-08, so a correct
  implementation deletes nothing today. If your change removes an existing
  file, it is wrong.
- `worker/schema.sql` and the D1 `job_snapshots` table — separate storage with
  its own retention problem, covered by plan 004.

## Git workflow

- Branch: `advisor/003-prune-job-archive`
- Commit style: Conventional Commits.
  Suggested message: `feat: prune job snapshot archive on the 120-day state TTL`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 0: Record the baseline test count

Other plans in `plans/` also add tests to this same suite, and you may be
running after one of them. Do not assume the count is 45.

```bash
python -m unittest test_digest.py test_interactive.py 2>&1 | tail -3
```

Record the number after `Ran`. Call it **N**. This plan's net change is
**+3 tests**.

**Verify**: the run ends in `OK`. If it does not, STOP — the baseline is red.

### Step 1: Add the TTL constant

At the top of `job_archive.py`, below `REQUIREMENT_MARKERS`, add:

```python
# Snapshots exist so a "Tailor CV" button clicked days later still resolves to
# the exact captured job description. Once a job id has aged out of seen.json
# there is nothing left to resolve, so this matches STATE_TTL_DAYS in digest.py.
ARCHIVE_TTL_DAYS = 120
```

Add `import datetime` to the imports at the top of the file. The current
imports are `hashlib`, `json`, `os`, `re` — keep them alphabetical:
`datetime`, `hashlib`, `json`, `os`, `re`.

**Verify**: `python -c "import job_archive; print(job_archive.ARCHIVE_TTL_DAYS)"`
→ prints `120`.

### Step 2: Add the prune function

Add this function to `job_archive.py`, immediately **above** `write_snapshots`:

```python
def prune_snapshots(archive_dir: str = "jobs", ttl_days: int = ARCHIVE_TTL_DAYS,
                    today: datetime.date | None = None) -> list[str]:
    """Delete archive files older than the TTL and return the removed paths."""
    if not os.path.isdir(archive_dir):
        return []
    cutoff = ((today or datetime.date.today()) - datetime.timedelta(days=ttl_days)).isoformat()
    removed = []
    for name in sorted(os.listdir(archive_dir)):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        # Only prune files this module names. An unexpected filename is left
        # alone rather than guessed at.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
            continue
        if stem < cutoff:
            path = os.path.join(archive_dir, name)
            os.remove(path)
            removed.append(path)
    return removed
```

The `today` parameter exists so tests can pin the clock without patching
`datetime`. The regex guard means a stray file such as `jobs/notes.json` is
never deleted.

**Verify**: `python -m unittest test_interactive.TestJobArchive` → `OK`
(no behavior change yet — nothing calls this).

### Step 3: Call the prune from write_snapshots

In `write_snapshots`, insert one line immediately after
`os.makedirs(archive_dir, exist_ok=True)`:

```python
    os.makedirs(archive_dir, exist_ok=True)
    prune_snapshots(archive_dir)
```

Pruning before the write means the day's own file is written after the sweep
and can never be removed by it, regardless of clock skew.

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N tests (no tests added yet). The end-to-end tests in `test_digest.py` exercise
`write_snapshots` through `digest.main()`; they must still pass unchanged.

### Step 4: Add tests

Add these to `TestJobArchive` in `test_interactive.py`. `datetime`, `os`,
`json` and `tempfile` handling: check the file's existing imports first and add
only what is missing (`datetime` is likely missing).

**Note, corrected after the first execution**: the fixture files below must be
written directly with `json.dump`, NOT via `write_snapshots`. `write_snapshots`
prunes against **wall-clock today** on every call (step 3), so backfilling an
old date through it deletes the `2026-01-01` fixture during setup, before this
test's own pinned-clock `prune_snapshots` call ever runs. The assertions are
unchanged.

```python
    def test_prune_removes_archives_older_than_the_ttl_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as root:
            # Written directly rather than via write_snapshots: write_snapshots
            # prunes against wall-clock today on every call, so backfilling an
            # old date through it would delete the 2026-01-01 fixture before
            # this test's own pinned-clock prune ever runs.
            for date in ("2026-01-01", "2026-08-01", "2026-08-09"):
                with open(os.path.join(root, f"{date}.json"), "w", encoding="utf-8") as handle:
                    json.dump({"schema_version": 1, "date": date, "jobs": []}, handle)
            removed = prune_snapshots(root, ttl_days=120, today=datetime.date(2026, 8, 9))

            remaining = sorted(os.listdir(root))

        self.assertEqual([os.path.basename(path) for path in removed], ["2026-01-01.json"])
        self.assertEqual(remaining, ["2026-08-01.json", "2026-08-09.json"])

    def test_prune_ignores_files_it_does_not_recognise(self):
        with tempfile.TemporaryDirectory() as root:
            write_snapshots([JOB], "2026-01-01", root)
            with open(os.path.join(root, "notes.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            prune_snapshots(root, ttl_days=120, today=datetime.date(2026, 8, 9))

            remaining = sorted(os.listdir(root))

        self.assertEqual(remaining, ["notes.json"])

    def test_writing_todays_archive_never_prunes_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_snapshots([JOB], "2026-08-09", root)
            self.assertTrue(os.path.exists(path))
```

Update the import line at the top of `test_interactive.py` to include the new
function:

```python
from job_archive import make_snapshot, job_card_payload, prune_snapshots, write_snapshots
```

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N+3 tests.

### Step 5: Confirm the workflow stages deletions

This step verifies the claim that `.github/workflows/digest.yml` needs no
change. Run in the repository root, on your branch, without committing:

```bash
mkdir -p jobs && touch jobs/2020-01-01.json && git add -- jobs/ && git rm --cached -q jobs/2020-01-01.json && rm jobs/2020-01-01.json && git status --porcelain -- jobs/
```

Simpler and sufficient: confirm your git version is 2.0 or newer with
`git --version`. If it is, `git add -- jobs/` stages deletions and the workflow
is correct as written.

**Verify**: `git --version` → 2.0 or newer. Record the version in your report.
If it is older than 2.0, STOP and report — the workflow would need
`git add -A -- jobs/` and that is an out-of-scope file.

## Test plan

- New tests in `test_interactive.py`, class `TestJobArchive`:
  - `test_prune_removes_archives_older_than_the_ttl_and_keeps_the_rest` — the
    core behavior, with a pinned clock.
  - `test_prune_ignores_files_it_does_not_recognise` — a non-date filename is
    never deleted.
  - `test_writing_todays_archive_never_prunes_it` — the ordering guarantee from
    step 3.
- Structural pattern: the existing
  `test_write_snapshots_uses_one_date_file_with_full_jd` in the same class —
  same `tempfile.TemporaryDirectory()` shape, same assertion style.
- Verification: `python -m unittest test_digest.py test_interactive.py` → all
  pass, N+3 tests.

## Done criteria

ALL must hold:

- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with N+3
      tests, where N is the count recorded in step 0
- [ ] `cd worker && node --test` still passes with 0 fail and the same
      count as before your change (this plan does not touch the Worker suite)
- [ ] `python -c "import job_archive; print(job_archive.ARCHIVE_TTL_DAYS)"` prints `120`
- [ ] `ls jobs/` still lists every file it listed before your change — a correct
      implementation prunes nothing today
- [ ] `git status --porcelain` lists only `job_archive.py` and
      `test_interactive.py`; **no deletions under `jobs/`**
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `job_archive.py:100-129` does not match the excerpt in "Current state".
- `python -m unittest test_digest.py test_interactive.py` fails before you make
  any change.
- Your change deletes any file currently in `jobs/`. That means the cutoff
  arithmetic is inverted — the oldest file is 2026-08-08 and the TTL is 120
  days.
- `git --version` is older than 2.0 (see step 5).
- You conclude the prune belongs in `digest.py` rather than `job_archive.py`.
  It does not — `digest.py` is being modified concurrently on another branch.

## Maintenance notes

- **`write_snapshots` prunes relative to wall-clock today, not to its `date`
  argument.** Harmless for the only production caller (`digest.py:1078`, which
  always passes today's date), but it means a backfill call with an old date
  has its own file deleted by the very next write. If backfilling ever becomes
  a real use case, give `prune_snapshots` an explicit opt-out. Surfaced during
  the first execution of this plan.
- `ARCHIVE_TTL_DAYS` in `job_archive.py` and `STATE_TTL_DAYS` in `digest.py`
  are deliberately the same number in two places, because the modules do not
  import each other. If one changes, change both, and say why in the commit.
- The D1 `job_snapshots` table (`worker/schema.sql`) holds the same evidence
  server-side and has **no** retention at all. Plan 004 covers it. Keep the two
  retention windows aligned, or a button will resolve from one store and not
  the other.
- A second, larger contributor to archive size is out of scope here: JSearch
  job ids are ~400-character base64 blobs and are stored twice per snapshot
  (`job_key` and `source_id`). Hashing them would shrink both `jobs/*.json` and
  `seen.json` substantially, but it would invalidate every existing id in
  `seen.json` and cause a one-time re-delivery of everything. Worth doing
  deliberately, not as a side effect of this plan.
- Reviewer should scrutinize: that the prune runs before the write, and that
  the filename regex cannot match anything but a date.
