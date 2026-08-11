# Plan 007: Remove the dead auto-tailoring code from digest.py

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

## ⚠️ Sequencing requirement — read before starting

There is uncommitted work in the worktree at `.worktrees/discord-tailoring`
(branch `codex/discord-tailoring`) that modifies `digest.py`. **Do not execute
this plan until that branch has been merged or abandoned**, and confirm with
the operator first.

The collision is manageable but real: that branch adds a `fetch_posting_page`
helper around line 700, which is close to the block this plan deletes
(lines 743–818). Deleting a large region in one branch while another edits
nearby is the classic recipe for a bad merge.

It has been verified that the branch does **not** use any of the symbols this
plan removes — `resume_tailor.py` on that branch defines its own
`TAILOR_MODEL` at line 24 (`os.environ.get("TAILOR_MODEL", "gemini-3.6-flash")`)
rather than importing `digest.TAILOR_MODEL`. So the deletion is safe *after*
the merge. Re-verify with the command in step 1 before deleting anything.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (after the sequencing requirement is met)
- **Depends on**: the `codex/discord-tailoring` worktree being merged or abandoned
- **Category**: tech-debt
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

Resume tailoring used to run automatically inside the daily digest: the top
strong matches were rewritten by Gemini and the result was committed into
`tailored/<date>/`. That design was deliberately replaced. The current
architecture is stated in `digest.py:179-181`:

```python
# Per-job resume tailoring is now an explicit Discord interaction.  The daily
# run archives JD evidence and posts jobs only; it never spends tailoring
# credits or writes private resumes into the repository.
```

The comment landed but the code did not. `main()` no longer calls any of it,
yet roughly 90 lines of the old pipeline remain in `digest.py` — including a
complete Gemini prompt for resume rewriting that nothing will ever send.

Worse, `test_digest.py` still exercises three of these symbols, so the dead
code has passing tests. Anyone reading the file — human or agent — has every
reason to believe `tailor_resume` is live. The next person to change tailoring
behavior will reasonably edit the wrong function, in the wrong file, and their
tests will pass.

The design document for the replacement
(`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`) is explicit
about where tailoring now lives: "GitHub Actions keeps job discovery and
executes PDF extraction, Gemini tailoring, verification, and rendering" — via
`interactive_runner.py`, not `digest.py`.

## Current state

Files involved:

- `digest.py` — the daily engine. Contains the dead code.
- `test_digest.py` — three tests exercise dead symbols.

**The dead symbols and their exact locations at `6c7541e`:**

| Symbol | Location | Referenced by |
|---|---|---|
| `TAILOR_MODEL` | `digest.py:174` | only `tailor_resume` |
| `RESUME_FILE` | `digest.py:182` | only `load_resume` |
| `TAILOR_DIR` | `digest.py:183` | nothing |
| `TAILOR_MAX` | `digest.py:184` | nothing (comment at :170 mentions it) |
| `load_resume` | `digest.py:743-754` | nothing |
| `slugify` | `digest.py:757-759` | only `test_digest.py:119` |
| `tailor_resume` | `digest.py:762-808` | only `test_digest.py:272` (a mock target) |
| `tailored_url` | `digest.py:811-818` | only `test_digest.py:124` |

**Symbols that look similar but are LIVE — do not touch:**

| Symbol | Location | Used by |
|---|---|---|
| `RANK_MODEL` | `digest.py:173` | `gemini_rank` — the daily scoring call |
| `JD_MAX_CHARS` | `digest.py:185` | `get_description` |
| `JD_RANK_CHARS` | `digest.py:186` | `gemini_rank` |
| `MAX_JOBS_IN_DIGEST` | `digest.py:176` | `main()` |
| `STRONG_MATCH_MIN_SCORE` | `digest.py:177` | `split_tiers` |
| `post_discord` | `digest.py:958` | `main()` — the webhook fallback |

The constants block as it exists today, `digest.py:158-186`:

```python
# Gemini models. Two tiers on purpose, verified against ai.google.dev/gemini-api/docs/models
# (page last updated 2026-07-30, read 2026-08-02) — both strings are listed STABLE:
#
#   RANK_MODEL   gemini-3.1-flash-lite — scoring is classification over <=25 short
#                records in one call. Flash-Lite is built for exactly this and is
#                the cheapest option. Keeping it.
#   TAILOR_MODEL gemini-3.6-flash — rewriting a full resume is long-form generation
#                under hard truth constraints, and Google's own guidance puts
#                Flash-Lite at "classification, extraction, basic summarization"
#                while Flash handles longer reasoning chains and sustained
#                coherence. Flash-Lite drifts on this job: it drops sections,
#                loosens the no-invention rule, and writes thinner Fit notes.
#                Costs more per call, but it runs at most TAILOR_MAX times a day.
#
# NOTE: gemini-3.1-flash-lite-PREVIEW is shut down; the stable id above is not.
RANK_MODEL = "gemini-3.1-flash-lite"
TAILOR_MODEL = "gemini-3.6-flash"

MAX_JOBS_IN_DIGEST = 25
STRONG_MATCH_MIN_SCORE = 80

# Per-job resume tailoring is now an explicit Discord interaction.  The daily
# run archives JD evidence and posts jobs only; it never spends tailoring
# credits or writes private resumes into the repository.
RESUME_FILE = "resume.md"
TAILOR_DIR = "tailored"
TAILOR_MAX = 10
JD_MAX_CHARS = 6000    # JD text handed to the tailor prompt
JD_RANK_CHARS = 900    # JD excerpt per job inside the one ranking call
```

The three tests to remove, in `test_digest.py`:

```python
    def test_slugify(self):                                    # line 119
        self.assertEqual(digest.slugify("Senior Software Engineer (AI Systems)"),
                         ...)
        self.assertEqual(digest.slugify("شركة"), "job")  # non-latin falls back safely

    def test_tailored_url_local_and_actions(self):              # line 124
        ...
            self.assertEqual(digest.tailored_url("tailored/x.md"), "tailored/x.md")
        ...
                digest.tailored_url("tailored/x.md"),
```

and a mock at `test_digest.py:272`, inside a `with` block in a helper:

```python
             mock.patch.object(digest, "tailor_resume", return_value=tailor_result), \
```

The module docstring of `test_digest.py` also lists coverage that will no
longer be accurate:

```
  - strip_html / slugify helpers
  ...
  - fail-soft paths: Gemini down -> keyword scores, no key -> no tailoring,
    tailor failure -> job still delivered
```

Repo conventions that apply here:

- `digest.py` is intentionally one file with a config block at the top and
  narrative comments explaining decisions with dates. When removing a comment,
  remove the whole decision it documented — do not leave an orphaned fragment.
- Deletions should not reflow surrounding code. Keep the two-blank-line
  separation between top-level functions.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Install      | `pip install -r requirements.txt`                         | exit 0                        |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0 (see step 1)     |
| Worker tests | `cd worker && node --test`                                | 0 fail                        |
| Import check | `python -c "import digest"`                               | exit 0, no output             |

## Scope

**In scope** (the only files you should modify):

- `digest.py`
- `test_digest.py`

**Out of scope** (do NOT touch, even though they look related):

- `resume.md` — still the master resume, read by `interactive_runner.py`. The
  file stays; only `digest.py`'s reader of it goes.
- `tailored/` — the directory of previously generated resumes. Historical
  output; leave it. Its removal is the operator's call, not a code change.
- `interactive_runner.py`, `resume_source.py`, `resume_ingest.py` — the live
  tailoring path.
- `RANK_MODEL` and everything in `gemini_rank`. Daily scoring is untouched.
- `.github/workflows/digest.yml` — it already commits `jobs/` rather than
  `tailored/`.

## Git workflow

- Branch: `advisor/007-remove-dead-tailoring`
- Commit style: Conventional Commits.
  Suggested message: `refactor: drop the superseded in-digest tailoring path`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baseline and re-verify the symbols are dead

Other plans in `plans/` add tests to this same suite, and you may be running
after one of them. Do not assume the count is 45.

```bash
python -m unittest test_digest.py test_interactive.py 2>&1 | tail -3
```

Record the number after `Ran`. Call it **N**. This plan's net change is
**−2 tests**. If the run does not end in `OK`, STOP — the baseline is red.

Then run this and confirm the results before deleting anything:

```bash
grep -rn "tailor_resume\|load_resume\|slugify\|tailored_url\|TAILOR_DIR\|TAILOR_MAX\|TAILOR_MODEL\|RESUME_FILE" --include="*.py" .
```

**Verify**: every match is inside `digest.py` (the definitions listed in
"Current state") or one of the three `test_digest.py` sites. If any *other*
Python file references them — especially `interactive_runner.py`,
`resume_tailor.py`, or `resume_render.py` — **STOP**. That means the
`codex/discord-tailoring` branch merged a dependency on them and this plan
needs rewriting.

### Step 2: Remove the dead tests first

Working tests-first means the next step's deletion is verified by a suite that
no longer depends on what you are deleting.

In `test_digest.py`:

1. Delete the whole `test_slugify` method (line 119) and the whole
   `test_tailored_url_local_and_actions` method (line 124), from their `def`
   lines through their final assertion.
2. Remove the line
   `mock.patch.object(digest, "tailor_resume", return_value=tailor_result), \`
   from inside `run_main` (at `test_digest.py:246`, the mock is around line
   270). **The `with` statement is a backslash-continued chain of context
   managers** — after removing one line the chain must stay syntactically
   valid, and the last remaining context manager must not end with a trailing
   `\`. As it stands the chain ends with
   `mock.patch.object(digest.time, "sleep", lambda *_: None):`, so removing a
   line from the middle is safe.
3. Remove the now-unused `tailor_result` parameter from `run_main`. Its
   signature becomes:

   ```python
   def run_main(monkey_env, gemini_ranking, link_statuses=None):
   ```

   Then update all **seven** call sites, which pass it positionally as the
   third argument. At `6c7541e` they are at lines 326, 352, 361, 390, 410, 424
   and 435:

   - `run_main(env, ranking, None)` → `run_main(env, ranking)`
   - `run_main(self.ENV, ranking, "# Tailored resume\ncontent")` → `run_main(self.ENV, ranking)`
   - `run_main(self.ENV, None, None)` → `run_main(self.ENV, None)`
   - the two multi-line calls that also pass `link_statuses=` keep that keyword
     argument; only the third positional argument goes.

   **Verify before moving on**: `grep -c "run_main(" test_digest.py` returns
   `8` (one definition plus seven calls), and `grep -n "tailor_result"
   test_digest.py` returns no matches.
4. Update the module docstring at the top of `test_digest.py`: remove
   `slugify` from the "strip_html / slugify helpers" line, and remove the
   `no key -> no tailoring, tailor failure -> job still delivered` clauses from
   the fail-soft line. Leave the rest of the docstring intact.

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N−2 tests, where N is the count recorded in step 1.

### Step 3: Delete the dead functions

In `digest.py`, delete these four functions in full, keeping exactly two blank
lines between the functions that remain on either side:

- `load_resume` — lines 743–754
- `slugify` — lines 757–759
- `tailor_resume` — lines 762–808
- `tailored_url` — lines 811–818

After deletion, `split_tiers` (ending at line 740) should be followed by two
blank lines and then the `CLOSED_MARKERS` comment block that currently starts
at line 821.

**Verify**: `python -c "import digest"` → exit 0, no output. Then
`python -m unittest test_digest.py test_interactive.py` → `OK`, N−2 tests.

### Step 4: Delete the dead constants and repair their comments

In `digest.py`:

1. Delete line 174 (`TAILOR_MODEL = "gemini-3.6-flash"`).
2. Rewrite the comment block at lines 158–172 so it documents only the model
   that remains. Replace the whole block with:

```python
# Ranking model, verified against ai.google.dev/gemini-api/docs/models
# (page last updated 2026-07-30, read 2026-08-02) — listed STABLE:
#
#   RANK_MODEL   gemini-3.1-flash-lite — scoring is classification over <=25 short
#                records in one call. Flash-Lite is built for exactly this and is
#                the cheapest option. Keeping it.
#
# NOTE: gemini-3.1-flash-lite-PREVIEW is shut down; the stable id above is not.
# Resume tailoring runs in interactive_runner.py and picks its own model there.
RANK_MODEL = "gemini-3.1-flash-lite"
```

3. Delete lines 182–184 (`RESUME_FILE`, `TAILOR_DIR`, `TAILOR_MAX`) and rewrite
   the comment above them. The remaining block should read:

```python
MAX_JOBS_IN_DIGEST = 25
STRONG_MATCH_MIN_SCORE = 80

# Per-job resume tailoring is an explicit Discord interaction handled by
# interactive_runner.py. The daily run archives JD evidence and posts jobs
# only; it never spends tailoring credits or writes private resumes into the
# repository. These two bounds are what the daily run still needs from the JD.
JD_MAX_CHARS = 6000    # JD text stored per job in the snapshot archive
JD_RANK_CHARS = 900    # JD excerpt per job inside the one ranking call
```

Note the `JD_MAX_CHARS` comment changed: it no longer feeds a "tailor prompt",
it bounds what `get_description` stores.

**Verify**:

```bash
python -c "import digest; print(digest.RANK_MODEL, digest.JD_MAX_CHARS, digest.JD_RANK_CHARS)"
```
→ prints `gemini-3.1-flash-lite 6000 900`, exit 0.

Then:
```bash
grep -c "TAILOR_MODEL\|TAILOR_DIR\|TAILOR_MAX\|RESUME_FILE" digest.py
```
→ `0`.

### Step 5: Full verification

**Verify**:
- `python -m unittest test_digest.py test_interactive.py` → `OK`, N−2 tests
- `cd worker && node --test` → 0 fail, count unchanged
- `git diff --stat` → only `digest.py` and `test_digest.py`, with roughly
  90 deletions in `digest.py`

## Test plan

No new tests. This plan removes tests for removed code and must not reduce
coverage of anything live:

- Removed: `test_slugify`, `test_tailored_url_local_and_actions`, and one mock
  target. All three covered symbols with no production caller.
- Must keep passing unchanged: every test in `TestFilters`, `TestLinkEvidence`,
  `TestGetDescription`, `TestTierClassification`, and `TestEndToEnd`. In
  particular `test_full_run_archives_jobs_without_auto_tailoring` asserts
  `os.path.exists(os.path.join(work, "tailored"))` is `False` — that assertion
  is the real regression guard for this plan's intent and must survive.
- Verification: `python -m unittest test_digest.py test_interactive.py` → N−2
  tests, all pass.

## Done criteria

ALL must hold:

- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with N−2
      tests, where N is the count recorded in step 1
- [ ] `grep -c "tailor_result" test_digest.py` returns `0`
- [ ] `cd worker && node --test` still passes with 0 fail and the same
      count as before your change (this plan does not touch the Worker suite)
- [ ] `python -c "import digest"` exits 0
- [ ] `grep -c "TAILOR_MODEL\|TAILOR_DIR\|TAILOR_MAX\|RESUME_FILE" digest.py` returns `0`
- [ ] `grep -c "def tailor_resume\|def load_resume\|def slugify\|def tailored_url" digest.py` returns `0`
- [ ] `grep -c "RANK_MODEL" digest.py` returns `3` or more — the ranking model
      must still be defined and used
- [ ] `test_full_run_archives_jobs_without_auto_tailoring` still exists and passes
- [ ] `git status --porcelain` lists only `digest.py` and `test_digest.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The `codex/discord-tailoring` worktree has not been merged or abandoned, and
  the operator has not confirmed it is safe to proceed.
- Step 1 finds a reference to any dead symbol outside `digest.py` and
  `test_digest.py`.
- `digest.py:158-186` or `:743-818` do not match the excerpts in "Current state".
- `python -m unittest test_digest.py test_interactive.py` fails before you make
  any change.
- Removing the mock at `test_digest.py:272` cannot be done without restructuring
  the enclosing helper for other tests. Report the shape of the problem rather
  than rewriting shared test infrastructure.
- Deleting a function causes an `ImportError` or `NameError` anywhere. That
  means it was not dead; report which caller you found.

## Maintenance notes

- After this lands, `digest.py` contains no resume logic at all. That is the
  intended boundary: `digest.py` discovers, filters, ranks, archives, and
  delivers; `interactive_runner.py` and its helpers do everything resume-shaped.
  Keep it that way — a future "just one small resume helper" in `digest.py` is
  how this drifted the first time.
- `resume.md` at the repository root is still load-bearing. `interactive_runner.py`
  reads it by relative path. Do not let a later cleanup sweep it up as
  "unused by digest.py".
- The `tailored/` directory still holds output from the old pipeline
  (`tailored/2026-07-31/`). Nothing writes to it now. Whether to keep it as a
  record or delete it is the operator's decision, deliberately left out of this
  plan.
- Reviewer should scrutinize: that `RANK_MODEL` and both `JD_*` constants
  survived, that the rewritten comments do not describe removed behavior, and
  that the test-count drop is exactly 2.
