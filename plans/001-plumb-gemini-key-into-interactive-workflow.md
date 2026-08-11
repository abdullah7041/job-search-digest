# Plan 001: Pass GEMINI_API_KEY to the interactive task workflow

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- .github/workflows/interactive.yml interactive_runner.py test_interactive.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). A local clone may be behind. Run `git pull --ff-only`
> first and confirm `git rev-parse --short HEAD` prints `6c7541e` or later.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

`.github/workflows/interactive.yml` is the workflow that runs when the user
clicks the "Tailor CV" button on a Discord job card. It passes exactly three
environment variables to `interactive_runner.py`, and `GEMINI_API_KEY` is not
one of them.

Work currently in progress on the branch `codex/discord-tailoring` replaces the
runner's placeholder tailoring with a real Gemini-backed one that begins:

```python
api_key = os.environ.get("GEMINI_API_KEY", "")
if not api_key:
    return base64.b64encode(render_pdf(document)).decode("ascii"), [], [], \
        "GEMINI_API_KEY is not configured, so this is the untailored master resume."
```

That fallback is well designed — it still returns a PDF and says why it is not
tailored. But because the workflow never provides the key, **every real button
click in production will take that branch**. The user will receive their plain
master resume with a warning note, every single time, and the entire tailoring
feature will appear to be broken while every test passes.

The repository already holds a `GEMINI_API_KEY` secret — `.github/workflows/digest.yml`
uses it for daily ranking. This plan is one line of YAML plus a regression test
that keeps the line from being dropped again.

## Current state

Files involved:

- `.github/workflows/interactive.yml` — the workflow dispatched by the Cloudflare
  Worker when a Discord button is clicked. Runs `interactive_runner.py` once.
- `test_interactive.py` — offline contract tests for the interactive pipeline.
  Uses `unittest`, no network, no mocks of the filesystem.

`.github/workflows/interactive.yml` in full, as it exists today:

```yaml
name: Interactive job task

on:
  workflow_dispatch:
    inputs:
      task_id:
        description: Worker task identifier
        required: true
        type: string

permissions:
  contents: read

concurrency:
  group: interactive-${{ inputs.task_id }}
  cancel-in-progress: false

jobs:
  run-task:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - name: Retrieve, validate, and report task
        env:
          INTERACTIVE_WORKER_URL: ${{ secrets.INTERACTIVE_WORKER_URL }}
          WORKER_CALLBACK_SECRET: ${{ secrets.WORKER_CALLBACK_SECRET }}
          TASK_ID: ${{ inputs.task_id }}
        run: python interactive_runner.py
```

For comparison, `.github/workflows/digest.yml` already reads the same secret
(this is the existing convention to match — do not invent a new secret name):

```yaml
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          INTERACTIVE_WORKER_URL: ${{ secrets.INTERACTIVE_WORKER_URL }}
          WORKER_CALLBACK_SECRET: ${{ secrets.WORKER_CALLBACK_SECRET }}
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python digest.py
```

Repo conventions that apply here:

- Tests are plain `unittest`, one `class Test<Thing>(unittest.TestCase)` per
  concern, descriptive method names written as full sentences
  (`test_runner_rejects_a_changed_jd`). Model your new test on the existing
  classes in `test_interactive.py`.
- No new dependencies. `requirements.txt` is `requests`, `pypdf`, `reportlab`
  only. **Do not add PyYAML** — read the workflow file as text.
- The project's stated design constraint (from
  `docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`) is:
  "GitHub Actions keeps job discovery and executes PDF extraction, Gemini
  tailoring, verification, and rendering." Gemini running inside Actions is the
  intended architecture, not a workaround.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Install      | `pip install -r requirements.txt`                         | exit 0                        |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0 (see step 0)     |
| Worker tests | `cd worker && node --test`                                | 0 fail                        |

## Scope

**In scope** (the only files you should modify):

- `.github/workflows/interactive.yml`
- `test_interactive.py`

**Out of scope** (do NOT touch, even though they look related):

- `interactive_runner.py` — the code that reads the key is being rewritten on
  another branch right now. Adding key-reading logic here will collide.
- `.github/workflows/digest.yml` — already correct.
- Any change to secret names, or any attempt to create/read/print secret
  **values**. You are adding a reference to an existing secret, nothing more.

## Git workflow

- Branch: `advisor/001-gemini-key-interactive`
- Commit style: Conventional Commits, matching recent history
  (`ci: test tiered digest before delivery`, `feat: deliver tiered job digest with quality metrics`).
  Suggested message: `ci: pass GEMINI_API_KEY to the interactive task workflow`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 0: Record the baseline test count

Other plans in `plans/` also add tests to this same suite, and you may be
running after one of them. Do not assume the count is 45.

```bash
python -m unittest test_digest.py test_interactive.py 2>&1 | tail -3
```

Record the number after `Ran`. Call it **N**. This plan's net change is
**+2 tests**.

**Verify**: the run ends in `OK`. If it does not, STOP — the baseline is red.

### Step 1: Add the environment variable to the workflow

In `.github/workflows/interactive.yml`, in the `env:` block of the step named
`Retrieve, validate, and report task`, add one line so the block reads:

```yaml
        env:
          INTERACTIVE_WORKER_URL: ${{ secrets.INTERACTIVE_WORKER_URL }}
          WORKER_CALLBACK_SECRET: ${{ secrets.WORKER_CALLBACK_SECRET }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          TASK_ID: ${{ inputs.task_id }}
```

Change nothing else in the file. Do not alter `permissions:`, `timeout-minutes:`,
or the `concurrency:` group.

**Verify**: `grep -n "GEMINI_API_KEY" .github/workflows/interactive.yml`
→ exactly one match, inside the `env:` block.

### Step 2: Add a regression test that pins the wiring

Append a new test class to `test_interactive.py`, before the
`if __name__ == "__main__":` block at the bottom of the file. It reads the
workflow file as text — no YAML parser, no new dependency:

```python
class TestInteractiveWorkflowWiring(unittest.TestCase):
    """The runner's tailoring silently degrades to an untailored resume when
    GEMINI_API_KEY is absent, so the workflow must supply it."""

    WORKFLOW = os.path.join(".github", "workflows", "interactive.yml")

    def _workflow_text(self):
        with open(self.WORKFLOW, encoding="utf-8") as handle:
            return handle.read()

    def test_workflow_passes_the_gemini_key_to_the_runner(self):
        self.assertIn("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", self._workflow_text())

    def test_workflow_still_passes_the_worker_callback_configuration(self):
        text = self._workflow_text()
        for name in ("INTERACTIVE_WORKER_URL", "WORKER_CALLBACK_SECRET", "TASK_ID"):
            self.assertIn(f"{name}:", text)
```

`os` is already imported at the top of `test_interactive.py` — confirm before
adding a duplicate import.

Note: these tests read a path relative to the working directory. They pass when
run from the repository root, which is how CI runs them
(`.github/workflows/digest.yml` runs `python -m unittest test_digest.py test_interactive.py`
with no `working-directory:` override).

**Verify**: `python -m unittest test_interactive.py` → `OK`, and the run
includes `test_workflow_passes_the_gemini_key_to_the_runner`.

### Step 3: Confirm nothing else broke

**Verify**: `python -m unittest test_digest.py test_interactive.py`
→ `OK`, N+2 tests, where N is the count recorded in step 0.

## Test plan

- New tests, in `test_interactive.py`, class `TestInteractiveWorkflowWiring`:
  - `test_workflow_passes_the_gemini_key_to_the_runner` — the regression this
    plan exists to prevent.
  - `test_workflow_still_passes_the_worker_callback_configuration` — guards
    against someone "cleaning up" the env block and dropping the callback
    configuration instead.
- Structural pattern to follow: `TestResumeIngestion` in `test_interactive.py`
  (small class, two focused tests, plain assertions).
- Verification: `python -m unittest test_digest.py test_interactive.py` → all
  pass, N+2 tests.

## Done criteria

ALL must hold:

- [ ] `grep -c "GEMINI_API_KEY" .github/workflows/interactive.yml` returns `1`
- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with N+2
      tests, where N is the count recorded in step 0
- [ ] `cd worker && node --test` still passes with 0 fail and the same
      count as before your change (this plan does not touch the Worker suite)
- [ ] `git status --porcelain` lists only `.github/workflows/interactive.yml`
      and `test_interactive.py`
- [ ] No secret value appears anywhere in the diff — only the
      `${{ secrets.GEMINI_API_KEY }}` reference
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `.github/workflows/interactive.yml` does not match the excerpt in "Current
  state" — someone has already changed the workflow.
- `python -m unittest test_digest.py test_interactive.py` fails **before** you
  make any change. The baseline is green at `6c7541e`; a red baseline means the
  repository moved and this plan needs re-verification.
- You find yourself needing to edit `interactive_runner.py` to make the test
  pass. You do not — the test reads the workflow file, not the runner.
- You cannot confirm a `GEMINI_API_KEY` secret exists in the repository. The
  workflow reference is still correct to add, but say so in your report: an
  absent secret resolves to an empty string and reproduces the exact bug this
  plan fixes.

## Maintenance notes

- **This is the pre-merge half of a two-part fix.** The other half is the
  Gemini-backed tailoring on `codex/discord-tailoring`. Landing that branch
  without this plan ships a feature that never tailors anything.
- `resume_tailor.py` on that branch reads `TAILOR_MODEL` from the environment
  with a default of `gemini-3.6-flash`. Only `GEMINI_API_KEY` is required; add
  a `TAILOR_MODEL` env line only if the operator wants to override the model
  from the workflow.
- Reviewer should check: exactly one new env line, no change to `permissions:`
  (the interactive workflow is intentionally `contents: read` — it must not
  gain write access), and no secret value in the diff.
- Deferred out of this plan: the interactive workflow has no failure alerting.
  If `interactive_runner.py` exits non-zero the user sees only Discord's
  generic message. That is a separate concern.
