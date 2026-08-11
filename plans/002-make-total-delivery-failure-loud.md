# Plan 002: Fail the run loudly when no job card reaches Discord

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
> `.worktrees/discord-tailoring` also modifies `digest.py` (it adds a
> `fetch_posting_page` helper around line 700). Your change is in `main()`
> around line 1108 and should not conflict textually, but coordinate with the
> operator before merging either branch.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

When the daily digest delivers through the Cloudflare Worker, the Worker
answers with a per-card summary such as
`{"posted": [...], "existing": [...], "in_progress": [...], "retryable": [...], "failed": [...]}`.

If **every** card lands in `failed` — for example Discord returns 503 for each
message — the current code treats that as "the interactive path claimed this
delivery" and therefore skips the webhook fallback, writes a normal-looking
stats row, and exits 0. The result: the user receives no digest at all, the
GitHub Actions run shows a green check, and the only evidence is one log line
nobody reads.

Not falling back is deliberate and correct — `test_digest.py` pins it, because
posting the same jobs twice through two different channels is worse than
posting them late, and the jobs are not marked seen so they are retried the
next day. The defect is not the missing fallback. **The defect is that a
total delivery failure is indistinguishable from a successful run.**

Two documents state the intended behavior and both are currently contradicted:

- `README.md:104` — "A failed POST exits non-zero so a broken run is loud, not
  silent."
- `docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`, task 1 —
  "…retain the plain webhook fallback."

After this plan: a run where nothing was delivered and at least one card
definitively failed exits non-zero, so GitHub Actions emails a failure and the
workflow badge goes red. Uncertain outcomes (`in_progress`, `retryable`) still
exit 0, because the Worker may yet complete those posts.

## Current state

Files involved:

- `digest.py` — the whole engine. The delivery decision is in `main()`, lines
  1108–1141.
- `test_digest.py` — offline regression tests. `TestEndToEnd` drives
  `digest.main()` against a temporary working directory with everything mocked.

`digest.py:1108-1141` exactly as it exists today:

```python
    interactive_delivery_claimed = False
    delivered_ids = set()
    if worker_url:
        try:
            delivery = post_interactive_cards(worker_url, worker_secret, snapshots)
            accepted_keys = set(delivery.get("posted", []) + delivery.get("existing", []))
            uncertain_keys = set(delivery.get("in_progress", []) + delivery.get("retryable", []))
            failed = delivery.get("failed", [])
            interactive_delivery_claimed = bool(accepted_keys or uncertain_keys or failed)
            delivered_ids = {snapshot["source_id"] for snapshot in snapshots if snapshot["job_key"] in accepted_keys}
            log(
                f"Interactive delivery accepted {len(accepted_keys)} of {len(snapshots)} cards "
                f"({len(failed)} failed)"
            )
        except Exception as error:
            log(f"WARN interactive Discord delivery failed: {error}; falling back to webhook")
    if not interactive_delivery_claimed:
        if not webhook:
            log("FATAL interactive delivery failed and DISCORD_WEBHOOK_URL is not set")
            sys.exit(1)
        post_discord(webhook, lines)
        if not worker_url:
            delivered_ids = {job["id"] for job in fresh}

    for j in fresh:
        if j["id"] in delivered_ids:
            seen[j["id"]] = today
    save_seen(seen)
    save_stats(
        stats, today, len(all_jobs), len(matched), new_count, avg_score,
        link_closed=len(closed), link_unknown=unknown_count,
        strong=len(strong), review=len(review), tailored=0, tailor_failed=0,
    )
    log(f"Delivered {len(delivered_ids)} jobs. seen.json now holds {len(seen)} ids.")
```

Note the existing exception path at line 1122: a Worker that is *unreachable*
(connection error, non-2xx — `post_interactive_cards` calls
`response.raise_for_status()`) already falls through to the webhook correctly.
That behavior must be preserved. Only the "Worker answered 200 but every card
failed" case changes.

The two existing tests that pin current behavior, in `test_digest.py`:

```python
    def test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
        ...
        def retryable(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [], "existing": [],
                "in_progress": [snapshots[0]["job_key"]],
                "retryable": [snapshots[1]["job_key"]],
                "failed": [],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=retryable):
            posts, work = run_main(env, ranking, None)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_failed_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
        ...
        def failed(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [], "existing": [], "in_progress": [], "retryable": [],
                "failed": [
                    {"job_key": snapshot["job_key"], "error": "Discord card post failed: 503"}
                    for snapshot in snapshots
                ],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=failed):
            posts, work = run_main(env, ranking, None)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})
```

The **retryable** test must keep passing unchanged. The **failed** test must be
updated to expect a non-zero exit while keeping both of its existing
assertions (no webhook fallback, nothing marked seen).

Repo conventions that apply here:

- Logging goes through `log(...)` (defined at `digest.py:209`). Severity is a
  bare prefix in the message string: `WARN ...`, `ERROR ...`, `FATAL ...`.
  There is no logging framework — match the prefix convention.
- Hard failures use `sys.exit(1)` after a `log("FATAL ...")` line. See
  `digest.py:978-980` and `post_discord` at `digest.py:968-971`.
- State must be written **before** exiting, so a failed run still records what
  it learned. This is the reason step 2 moves the exit to the end.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Install      | `pip install -r requirements.txt`                         | exit 0                        |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0 (see step 0)     |
| Single test  | `python -m unittest test_digest.TestEndToEnd`             | `OK`                          |
| Worker tests | `cd worker && node --test`                                | 0 fail                        |

## Scope

**In scope** (the only files you should modify):

- `digest.py` — only the block at lines 1108–1141 inside `main()`
- `test_digest.py` — only `TestEndToEnd`

**Out of scope** (do NOT touch, even though they look related):

- `discord_delivery.py` — the transport is correct; `raise_for_status()` there
  is what makes the existing unreachable-Worker fallback work.
- `worker/src/index.js` — the Worker's per-card summary shape is the contract
  this code reads. Changing it would require changing the Worker's tests too.
- The no-fallback behavior itself. Do **not** make a total failure post through
  the webhook. Double-posting is the outcome that behavior exists to prevent.
- The quiet-day heartbeat path at `digest.py:1023-1050` — a heartbeat failure
  is a separate case and is out of scope here.

## Git workflow

- Branch: `advisor/002-loud-delivery-failure`
- Commit style: Conventional Commits
  (example from history: `feat: deliver tiered job digest with quality metrics`).
  Suggested message: `fix: exit non-zero when no job card reaches Discord`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 0: Record the baseline test count

Other plans in `plans/` also add tests to this same suite, and you may be
running after one of them. Do not assume the count is 45.

```bash
python -m unittest test_digest.py test_interactive.py 2>&1 | tail -3
```

Record the number after `Ran`. Call it **N**. Every count below is expressed
relative to N. This plan's net change is **+1 test**.

**Verify**: the run ends in `OK`. If it does not, STOP — the baseline is red.

### Step 1: Track whether anything was actually delivered

Replace `digest.py:1108-1130` with the following. The changes are: a new
`delivery_failed` flag, an `ERROR` log line naming the failure, and a
`delivered=` metric so the stats row records reality.

```python
    interactive_delivery_claimed = False
    delivery_failed = False
    delivered_ids = set()
    if worker_url:
        try:
            delivery = post_interactive_cards(worker_url, worker_secret, snapshots)
            accepted_keys = set(delivery.get("posted", []) + delivery.get("existing", []))
            uncertain_keys = set(delivery.get("in_progress", []) + delivery.get("retryable", []))
            failed = delivery.get("failed", [])
            interactive_delivery_claimed = bool(accepted_keys or uncertain_keys or failed)
            delivered_ids = {snapshot["source_id"] for snapshot in snapshots if snapshot["job_key"] in accepted_keys}
            log(
                f"Interactive delivery accepted {len(accepted_keys)} of {len(snapshots)} cards "
                f"({len(failed)} failed)"
            )
            # Nothing reached Discord and at least one card definitively failed.
            # Falling back to the webhook would risk double-posting the jobs the
            # Worker may still hold, so the run stays single-channel and instead
            # fails loudly: seen.json is untouched, so tomorrow retries them.
            if not accepted_keys and not uncertain_keys and failed:
                delivery_failed = True
                log(f"ERROR no job card reached Discord; {len(failed)} of {len(snapshots)} cards failed")
                for item in failed[:5]:
                    log(f"  failed card {item.get('job_key')}: {item.get('error')}")
        except Exception as error:
            log(f"WARN interactive Discord delivery failed: {error}; falling back to webhook")
    if not interactive_delivery_claimed:
        if not webhook:
            log("FATAL interactive delivery failed and DISCORD_WEBHOOK_URL is not set")
            sys.exit(1)
        post_discord(webhook, lines)
        if not worker_url:
            delivered_ids = {job["id"] for job in fresh}
```

**Verify**: `python -m unittest test_digest.TestEndToEnd` → the retryable test
still passes; the failed test still passes (this step changes logging only, not
control flow).

### Step 2: Record the outcome in stats, then exit non-zero

Replace `digest.py:1132-1141` (everything from `for j in fresh:` to the final
`log(...)`) with:

```python
    for j in fresh:
        if j["id"] in delivered_ids:
            seen[j["id"]] = today
    save_seen(seen)
    save_stats(
        stats, today, len(all_jobs), len(matched), new_count, avg_score,
        link_closed=len(closed), link_unknown=unknown_count,
        strong=len(strong), review=len(review), tailored=0, tailor_failed=0,
        delivered=len(delivered_ids),
    )
    log(f"Delivered {len(delivered_ids)} jobs. seen.json now holds {len(seen)} ids.")
    if delivery_failed:
        log("FATAL digest produced jobs but none were delivered; failing the run so it is visible")
        sys.exit(1)
```

Order matters: `save_seen` and `save_stats` run **before** the exit, so the
failed run still records that it fetched and matched jobs and delivered zero.
`save_stats` already accepts arbitrary extra metrics via `**metrics`
(`digest.py:920`), so `delivered=` needs no signature change.

**Verify**: `python -m unittest test_digest.TestEndToEnd` → the *retryable*
test still passes; the *failed* test now fails with `SystemExit: 1`. That
failure is expected at this point and is fixed in step 3.

### Step 3: Update the failed-delivery test to expect a loud failure

`run_main` at `test_digest.py:246` returns `(posts, work)` on success. Once
`main()` raises `SystemExit`, it never returns, so the test cannot get `work`
and cannot read `seen.json` from disk. (For reference, `run_main`'s `finally`
block only does `os.chdir(old_cwd)` — it does not delete the directory — but
the return value is still lost.)

Assert on mocks instead. Both original guarantees are preserved exactly:
`post_discord` not being called proves there was no webhook fallback, and
`save_seen` being called with an empty dict proves nothing was marked seen.

Rename the test and replace its body with this, in full:

```python
    def test_failed_interactive_delivery_fails_loudly_without_falling_back(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def failed(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [],
                "existing": [],
                "in_progress": [],
                "retryable": [],
                "failed": [
                    {"job_key": snapshot["job_key"], "error": "Discord card post failed: 503"}
                    for snapshot in snapshots
                ],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=failed), \
             mock.patch.object(digest, "post_discord") as post_webhook, \
             mock.patch.object(digest, "save_seen") as save_seen:
            with self.assertRaises(SystemExit) as raised:
                run_main(env, ranking, None)

        # The run fails loudly...
        self.assertEqual(raised.exception.code, 1)
        # ...without double-posting through the webhook...
        post_webhook.assert_not_called()
        # ...and without marking anything seen, so tomorrow retries these jobs.
        save_seen.assert_called_once()
        self.assertEqual(save_seen.call_args[0][0], {})
```

`save_seen.call_args[0][0]` is the `seen` dict `main()` passes positionally at
the `save_seen(seen)` call site. It must be empty: `delivered_ids` is empty on
this path, so the loop above it adds nothing.

Note the `mock.patch.object(digest, "save_seen")` also stops the test writing
`seen.json` at all, which is why the on-disk assertion is no longer needed.

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N tests (the rename does not change the count).

### Step 4: Add a test that a partial success does NOT fail the run

Add this test next to the one you just edited. It guards the boundary: one card
posted and the rest failed is a normal, successful day.

```python
    def test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def partial(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [snapshots[0]["job_key"]],
                "existing": [],
                "in_progress": [],
                "retryable": [],
                "failed": [{"job_key": snapshots[1]["job_key"], "error": "Discord card post failed: 503"}],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=partial):
            posts, work = run_main(env, ranking, None)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)), 1)
```

**Verify**: `python -m unittest test_digest.py test_interactive.py` → `OK`,
N+1 tests.

## Test plan

- Modified test in `test_digest.py`:
  `test_failed_interactive_delivery_fails_loudly_without_falling_back` — total
  failure now exits 1, still no webhook fallback, still nothing marked seen.
- New test in `test_digest.py`:
  `test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen`
  — the boundary case; one success is not a failed run.
- Unchanged and must keep passing:
  `test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen`
  (uncertain outcomes stay exit 0) and
  `test_worker_only_quiet_day_does_not_call_an_absent_webhook`.
- Structural pattern: the existing tests in `TestEndToEnd` — same
  `mock.patch.object(digest, "post_interactive_cards", side_effect=...)` shape.
- Verification: `python -m unittest test_digest.py test_interactive.py` → all
  pass, N+1 tests.

## Done criteria

ALL must hold:

- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with N+1
      tests, where N is the count recorded in step 0
- [ ] `cd worker && node --test` still passes with 0 fail and the same
      count as before your change (this plan does not touch the Worker suite)
- [ ] `grep -n "delivery_failed" digest.py` returns 3 matches (init, set, check)
- [ ] `grep -n "delivered=len(delivered_ids)" digest.py` returns 1 match
- [ ] `grep -n "post_discord(webhook, lines)" digest.py | grep -v "^.*def "`
      returns exactly 2 matches — the quiet-day path and the fallback path. If
      it returns 3, you added a fallback that must not exist. (The `grep -v`
      filter is needed because the function's own `def post_discord(webhook, lines):`
      line matches the pattern as a substring; the raw grep returns 3 even on an
      unmodified file. Corrected during review of the first execution.)
- [ ] `git status --porcelain` lists only `digest.py` and `test_digest.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `digest.py:1108-1141` does not match the excerpt in "Current state".
- `python -m unittest test_digest.py test_interactive.py` fails before you make
  any change.
- Making the failed-delivery test pass appears to require changing
  `discord_delivery.py` or `worker/src/index.js`. It does not.
- You conclude the right fix is to fall back to the webhook on total failure.
  That is explicitly rejected — report your reasoning instead of implementing it.
- `run_main` in `test_digest.py` cannot be adapted to a `SystemExit` without
  restructuring the helper for every other test that uses it. Report the
  conflict rather than rewriting shared test infrastructure.

## Maintenance notes

- The `delivered` metric now in `stats.json` is new. Rows written before this
  change do not have it; any future code reading it must use
  `row.get("delivered")`, not `row["delivered"]`.
- If a Worker-side retry mechanism is ever added for `failed` cards, revisit
  this: `failed` would then mean "will be retried automatically", which belongs
  with `in_progress`/`retryable` and should stop failing the run.
- Reviewer should scrutinize: that `save_seen`/`save_stats` still run before
  `sys.exit(1)`, and that the fallback branch was not widened.
- Deferred: the quiet-day heartbeat (`digest.py:1023-1050`) calls
  `post_interactive_cards` without inspecting its summary at all, so a failed
  heartbeat is silent too. Smaller blast radius (no jobs are lost), left out to
  keep this change reviewable.
