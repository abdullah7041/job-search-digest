# Plan 012: Record a webhook-fallback delivery so a Worker outage stops re-posting the same digest every day

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 23de025..HEAD -- digest.py test_digest.py`
> If either file changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `23de025`, 2026-08-30

## Why this matters

The daily digest delivers Discord cards through a Cloudflare Worker. When that
call raises — the Worker is down, DNS fails, the HMAC secret is wrong, a 500
comes back — `digest.py` catches the exception, posts the whole digest through
the plain `DISCORD_WEBHOOK_URL` instead, and then **records nothing in
`seen.json`**. The user gets their jobs, but the run believes it delivered
zero.

The next morning the same postings are fetched again, their descriptions are
re-fetched over the network, they are re-scored by a paid model call, and the
identical digest is posted again. This repeats every single day for as long as
the Worker is unreachable, and the log line for each of those days reads
`Delivered 0 jobs. seen.json now holds 0 ids.` — which looks like a quiet
pipeline rather than a stuck one.

This was reproduced against the current tree, not inferred. See "Current state"
for the exact reproduction and its output.

After this plan: a digest that the webhook accepted is remembered, so a Worker
outage costs the user the interactive "Tailor CV" buttons for a day — never a
duplicated digest and never a repeated model bill.

## Current state

### Files

- `digest.py` — the whole daily engine. The delivery block is `main()` lines
  1580–1615. Nothing else in this plan's scope.
- `test_digest.py` — offline tests, no network. The delivery tests live in
  `class TestEndToEnd` (starts at line 1134) and share the `run_main` helper
  defined at line 734.

### The defect, verbatim (`digest.py:1580-1615`)

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

    for j in fresh:
        if j["id"] in delivered_ids:
            remember(seen, j, today)
```

The bug is the two lines `if not worker_url: delivered_ids = {...}`. When
`INTERACTIVE_WORKER_URL` **is** configured (which is the production
configuration) and the Worker call raised, `post_discord` runs and succeeds but
`delivered_ids` stays the empty set from line 1582.

### Reproduction observed at `23de025`

Running `digest.main()` with `INTERACTIVE_WORKER_URL` set and
`post_interactive_cards` raising `RuntimeError("worker unreachable")` produced:

```
WARN interactive Discord delivery failed: worker unreachable; falling back to webhook
Delivered 0 jobs. seen.json now holds 0 ids.
webhook posts: 1
seen.json after webhook fallback: {}
day-2 webhook posts: 1        <- the identical digest, same two jobs
```

### Why `post_discord` returning is proof of delivery

`digest.py:1356-1370`:

```python
def post_discord(webhook, lines):
    chunks, cur = [], ""
    ...
    for ch in chunks:
        r = requests.post(webhook, json={"content": ch}, timeout=30)
        if r.status_code not in (200, 204):
            log(f"ERROR Discord HTTP {r.status_code}: {r.text[:200]}")
            sys.exit(1)  # fail loud
        time.sleep(1)
```

Every chunk must return 200/204 or the process exits non-zero. So control
reaching the line *after* `post_discord(webhook, lines)` means Discord accepted
every chunk of the digest. That is exactly the condition under which the jobs
should be remembered.

### Repo conventions this must match

- Comments explain **why**, in full sentences, and name the concrete failure
  they prevent. The existing comment at lines 1596–1599 is the house style —
  match its voice and density.
- The governing instinct, stated in `README.md` and honored throughout
  `digest.py`: *a failure costs the user the feature, never the run, and always
  says why.* Falling back to the webhook is that instinct working. This plan
  does not change whether the fallback happens — only whether its result is
  recorded.
- Tests are `unittest`, offline, no network. Every fetcher, verifier and model
  call is mocked. See `run_main` at `test_digest.py:734-771` and the delivery
  tests at `test_digest.py:1225-1300` for the exact pattern.

### The existing test this one is modelled on (`test_digest.py:1225-1246`)

```python
    def test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def retryable(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [],
                "existing": [],
                "in_progress": [snapshots[0]["job_key"]],
                "retryable": [snapshots[1]["job_key"]],
                "failed": [],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=retryable):
            posts, work = run_main(env, ranking)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})
```

`run_main(env, ranking, work=work)` re-runs `main()` against a work directory a
previous call produced — that is how a second day is simulated. It returns
`(discord_posts, workdir)` where `discord_posts` is the list of webhook message
bodies.

## Commands you will need

| Purpose      | Command                                                                    | Expected on success |
|--------------|----------------------------------------------------------------------------|---------------------|
| Install      | `python -m pip install --require-hashes -r requirements.txt`                | exit 0              |
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` | `OK`               |
| Worker tests | `cd worker && npm test`                                                    | 0 fail              |

There is no lint, formatter or typecheck step in this repository.

**Record your observed baseline before changing anything.** At `23de025` the
Python suite reported `Ran 199 tests` and the Worker suite `pass 43`. Other
plans in `plans/` change those numbers, so do not treat 199 as fixed — write
down what *you* observe in step 1 and express your done criteria as a delta
from that.

## Scope

**In scope** (the only files you may modify):
- `digest.py` — the delivery block in `main()` only
- `test_digest.py` — one new test in `class TestEndToEnd`

**Out of scope** (do NOT touch, even though they look related):
- The `delivery_failed` branch at `digest.py:1600-1605`. That path deliberately
  does **not** fall back and deliberately does **not** mark anything seen,
  because the Worker definitively reported failures and may still hold the
  cards. Its behavior is correct and is covered by
  `test_failed_interactive_delivery_fails_loudly_without_falling_back`.
- `discord_delivery.py` — the transport. Nothing about signing or posting
  changes here.
- `post_heartbeat` (`digest.py:1332-1353`). The quiet-day path has its own
  Worker/webhook asymmetry; it is a separate known finding and is not this
  plan's job.
- `seen.json`, `stats.json`, `jobs/` in the repository root. These are
  generated state committed by the workflow. Never edit them by hand.

## Git workflow

- Branch: `advisor/012-record-webhook-fallback-delivery`
- Conventional Commits, as the repo uses. Recent examples from `git log`:
  `fix: only trust an apply link the employer's own token vouches for`,
  `fix: say when a relister blocks the apply-link lookup`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baseline

Run the full Python suite and write down the test count.

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ ends in `OK` (a `skipped=1` suffix is expected and fine). Note the
`Ran N tests` number as **N_before**.

### Step 2: Record a successful webhook fallback as a delivery

In `digest.py`, in the `if not interactive_delivery_claimed:` block
(currently lines 1606–1612), replace:

```python
        post_discord(webhook, lines)
        if not worker_url:
            delivered_ids = {job["id"] for job in fresh}
```

with an unconditional assignment after the post, carrying a comment that
explains the invariant. Produce this shape:

```python
        post_discord(webhook, lines)
        # Reaching this line means Discord accepted every chunk: post_discord
        # exits non-zero on any status outside 200/204. The jobs are therefore
        # delivered, whichever channel carried them, and have to be remembered.
        # They were not, when a Worker was configured and its call raised: the
        # webhook posted the digest, seen.json stayed empty, and the identical
        # digest went out again the next morning -- re-fetching every
        # description and paying for a second ranking call -- for as long as
        # the Worker stayed unreachable.
        delivered_ids = {job["id"] for job in fresh}
```

Change nothing else in the block. In particular leave the `if not webhook:`
guard and its `sys.exit(1)` exactly as they are.

**Verify**: `grep -n "if not worker_url:" digest.py` → **no matches**.
Then `python -m unittest test_digest.py` → `OK`, with the same test count as
step 1's `test_digest` contribution and no failures. (Every existing delivery
test must still pass: the retryable and failed paths never reach this line
because `interactive_delivery_claimed` is true for them.)

### Step 3: Add a regression test

Add one test to `class TestEndToEnd` in `test_digest.py`, immediately after
`test_failed_interactive_delivery_fails_loudly_without_falling_back` (which
ends around line 1278). Model it structurally on
`test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen`
quoted above.

Name it:

```python
    def test_a_webhook_fallback_delivery_is_remembered_so_it_is_not_resent(self):
```

It must:

1. Build `env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")`
   and the same two-entry `ranking` dict the neighbouring tests use
   (`jsearch_b` → 93, `jsearch_e` → 74).
2. Patch `digest.post_interactive_cards` with
   `side_effect=RuntimeError("worker unreachable")`.
3. Call `posts, work = run_main(env, ranking)` and assert `len(posts) == 1`
   (the digest went out over the webhook).
4. Read `os.path.join(work, "seen.json")` and assert the loaded dict is
   **not** empty — assert `len(seen) >= 2`, since `remember()` writes both a
   job identity key and a `jd:` description key per delivered job.
5. Re-run the same day against the same work directory with the same failing
   patch: `posts_again, _ = run_main(env, ranking, work=work)`. Assert
   `posts_again == []` — nothing is re-delivered, because the jobs are now
   remembered.

**Verify**: `python -m unittest test_digest.TestEndToEnd.test_a_webhook_fallback_delivery_is_remembered_so_it_is_not_resent`
→ `OK`, `Ran 1 test`.

### Step 4: Prove the test catches the regression

Temporarily restore the two removed lines (`if not worker_url:` guarding the
assignment), re-run the new test, and confirm it **fails**. Then restore your
fix.

**Verify**: with the old code in place the new test fails; with the fix in
place `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK` with `Ran (N_before + 1) tests`.

## Test plan

- **New test**: `test_a_webhook_fallback_delivery_is_remembered_so_it_is_not_resent`
  in `test_digest.py`, `class TestEndToEnd`. Covers the exception path
  (`post_interactive_cards` raises) end to end, across two runs, asserting both
  that the webhook delivered and that the second run is quiet.
- **Structural pattern**: `test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen`
  (`test_digest.py:1225`) for the mocking shape, and any existing two-day test
  using `run_main(..., work=work)` for the second-run shape.
- **Existing tests that must keep passing unchanged** (they cover the paths this
  plan must not disturb):
  - `test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen`
  - `test_failed_interactive_delivery_fails_loudly_without_falling_back`
  - `test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen`
  - `test_worker_only_quiet_day_does_not_call_an_absent_webhook`
- **Verification**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
  → `OK`, exactly one more test than N_before.

## Done criteria

ALL must hold:

- [ ] `grep -n "if not worker_url:" digest.py` returns no matches
- [ ] `grep -c "delivered_ids = {job\[\"id\"\] for job in fresh}" digest.py` returns `1`
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` ends in `OK`
      with `Ran (N_before + 1) tests`
- [ ] `cd worker && npm test` reports `fail 0` (unchanged — no Worker file was touched)
- [ ] `git status --porcelain` lists only `digest.py` and `test_digest.py` as modified
- [ ] `plans/README.md` status row for 012 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The excerpt at `digest.py:1580-1615` does not match what is quoted above —
  the file has drifted since this plan was written.
- Removing the `if not worker_url:` guard breaks
  `test_worker_only_quiet_day_does_not_call_an_absent_webhook` or any other
  existing test. That would mean some path other than the fallback depends on
  `delivered_ids` staying empty, which this plan assumes is not the case.
- You conclude the fallback itself should be removed rather than recorded.
  That is a larger behavioral decision about what a Worker outage should cost
  the user, and it belongs to the operator, not to this plan.
- Step 4 shows the new test passing against the *unfixed* code — the test is
  not exercising the bug and needs rewriting before you continue.

## Maintenance notes

- **What interacts with this**: `remember()` writes two keys per job (the job
  identity and a `jd:<sha256>` description fingerprint). If `remember()` ever
  changes shape, the `len(seen) >= 2` assertion in the new test is the thing
  that will need revisiting — deliberately written as `>=` rather than `== 4`
  so it does not become a brittle count.
- **What a reviewer should scrutinize**: that the change is confined to the
  fallback branch and that the `delivery_failed` path (Worker reported explicit
  failures) still marks nothing seen. Those two branches look similar and mean
  opposite things: one is "Discord accepted the digest through another
  channel", the other is "nothing reached Discord at all".
- **Deferred out of this plan**: the same asymmetry exists on the quiet-day
  path — `post_heartbeat` (`digest.py:1332`) calls `post_interactive_cards`
  with no try/except at all, so a Worker outage on a day with no new jobs
  crashes the run instead of falling back to the webhook. It fails loudly and
  the watchdog catches it, so it is a lower-priority inconsistency rather than
  a silent bug. Recorded in `plans/README.md` under findings not planned.
