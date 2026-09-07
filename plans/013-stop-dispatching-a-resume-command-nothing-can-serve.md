# Plan 013: Stop dispatching a `/resume` command nothing can serve, and guard the Worker→runner task contract

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 23de025..HEAD -- worker/src/index.js worker/test/interaction.test.js interactive_runner.py test_interactive.py README.md`
> If any of those changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `23de025`, 2026-08-30

## Why this matters

`/resume` is in the repository's registered command set and **cannot succeed**.
The Worker accepts it, writes a task row, encrypts the interaction token, and
dispatches a full GitHub Actions run — which runs both test suites, installs
every dependency, and then fails, because the Python runner has no handler for
a resume task. What the user sees, after roughly two minutes of
"Reading your CV privately…", is:

```
Interactive task failed safely: The task has no immutable job snapshot.
```

That message is not just a failure, it is a *misleading* failure: it describes
a missing job snapshot for a command that has nothing to do with jobs. Every
invocation burns an Actions run and a D1 row for a guaranteed error.

**Two things this plan does not claim, because they cannot be checked from the
repository.** Whether `/resume` is visible in Discord right now depends on
whether `npm run register --prefix worker` has been run since it entered
`worker/commands.js` — `git log` shows a registration-related change
(`59c05c1 fix: unregister unsupported tailor command`), but the live command
list is Discord's state, not the repo's. And the Worker does not ship by
merging: `worker/src/index.js` reaches production only when the operator runs
`npx wrangler deploy` (see step 6). If you cannot reproduce the wasted Actions
run, that is one of these two, **not** drift — the change is correct either
way, because the repo is the source of truth for what gets registered next.

Underneath it sits a half-built feature. The Worker exports
`createResumePreview`, `confirmResumePreview` and `resumeComponentAction`, the
D1 schema has `resume_previews` and `confirmed_resumes` tables, and
`resume_ingest.prepare_resume_preview` exists in Python — all unit-tested, and
**none of them reachable from any production code path**. `handleInteraction`
routes every button through `buttonTask`, whose regex only matches
`tailor:<32 hex>`, so a `resume-ok:` confirmation button throws
"Invalid tailoring button." before any of that code can run.

This plan does not finish the feature — that needs decisions the operator owns
(see "Maintenance notes"). It makes the broken surface honest and cheap: the
user gets an immediate, accurate answer instead of a two-minute wrong one, and
a new test makes it impossible to add a task kind the runner cannot serve
without a test failing.

## Current state

### The three places involved

- `worker/commands.js:19-31` — registers `/resume` with a required PDF
  attachment option. This file is the source of truth for what Discord shows.
- `worker/src/index.js:516-520` — the dispatch branch that creates the task.
- `interactive_runner.py:227-238` — `run_task`, which rejects it.

### `worker/src/index.js:515-520`, verbatim

```javascript
    else if (payload.type === 5) task = await manualTaskFromModal(payload, allowed.userId);
    else if (payload.data?.name === "resume") {
      const attachment = attachmentFrom(payload, "file");
      if (!attachment) throw new Error("Attach a PDF to this command.");
      task = { kind: "resume", userId: allowed.userId, input: { attachment } };
    }
```

Control then falls through to the shared tail of `handleInteraction`:

```javascript
    const taskId = await createTask(env, task, payload.token);
    // Discord discards the interaction if it is not acknowledged within 3
    // seconds. The GitHub dispatch is an external round-trip, so it runs after
    // the acknowledgement; a failure is reported back through the same message.
    const dispatch = dispatchWorkflow(env, taskId).catch((error) =>
      reportDispatchFailure(env, payload.token, `Could not start that task: ${error.message}`)
    );
    background(ctx, dispatch);
    return json({ type: 5, data: { content: task.kind === "resume" ? "Reading your CV privately…" : "Preparing your tailored CV privately…", flags: 64 } });
```

Anything thrown inside the surrounding `try` is caught by:

```javascript
  } catch (error) {
    return json({ type: 4, data: { content: `Could not start that task: ${error.message}`, flags: 64 } });
  }
```

— an immediate ephemeral reply, with **no task row and no workflow dispatch**.
That is the mechanism this plan uses.

### `interactive_runner.py:227-238`, verbatim

```python
def run_task(task: dict, **resolvers) -> dict:
    """Settle which job description may be tailored against, then tailor it.

    ``resolvers`` forwards ``fetch`` and ``transcribe`` to resolve_jd, so a test
    can exercise the recovery paths without reaching the network.
    """
    task_input = task.get("input", {})
    snapshot = task_input.get("job_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("The task has no immutable job snapshot.")
```

There is no branch on `task["kind"]` anywhere in `interactive_runner.py`. The
only kind it can serve is `tailor`.

### Verified at `23de025`

```
$ python -c "import interactive_runner as ir; ir.run_task({'kind':'resume','input':{'attachment':{...}}})"
run_task raised: ValueError - The task has no immutable job snapshot.
```

```
$ grep -rn "createResumePreview\|confirmResumePreview\|resumeComponentAction" --include=*.js .
worker/src/index.js:21   (definition)
worker/src/index.js:209  (definition)
worker/src/index.js:232  (definition)
worker/test/interaction.test.js:15,16,17,59,71,92,122  (tests only)
```

No production caller. Same for `prepare_resume_preview` in Python: defined in
`resume_ingest.py:9`, referenced only by `test_interactive.py:26,201,210`.

### The existing guard that does *not* cover this (`worker/test/commands.test.js:59-73`)

```javascript
test("every registered command name has a dispatch branch in the worker", () => {
  // Guards the drift that would present to the user as "Unsupported command."
  const source = readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
  for (const name of COMMAND_NAMES) {
    assert.ok(
      source.includes(`payload.data?.name === "${name}"`),
      `worker/src/index.js has no dispatch branch for /${name}`,
    );
  }
  ...
});
```

It checks the Worker has a branch. It says nothing about whether the **Python
runner** can serve what that branch produces. That is precisely the gap that
let this ship.

### Repo conventions

- **Fail-soft is the governing instinct**: a failure costs the user the
  feature, never the run, and always says why. An honest immediate refusal is
  more in keeping with that than a two-minute misleading error.
- Comments explain *why*, name the concrete failure they prevent, and are
  written in full sentences. `worker/commands.js:1-17` is the house style.
- Worker tests are `node:test` with `assert/strict`, no dependencies. See
  `worker/test/interaction.test.js` for the fake-D1 and fake-fetch patterns.
- Python tests are `unittest`, offline. See `test_interactive.py`.
- Discord ephemeral replies use `flags: 64`.

## Commands you will need

| Purpose      | Command                                                                        | Expected on success |
|--------------|--------------------------------------------------------------------------------|---------------------|
| Install      | `python -m pip install --require-hashes -r requirements.txt`                    | exit 0              |
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`  | `OK`                |
| Worker tests | `cd worker && npm test`                                                        | `fail 0`            |

There is no lint, formatter or typecheck step in this repository.

**Record your observed baselines in step 1.** At `23de025` the Python suite
reported `Ran 199 tests` and the Worker suite `pass 43`. Other plans move those
numbers — express your done criteria as a delta from what you observe, never
from 199/43.

## Scope

**In scope** (the only files you may modify):
- `worker/src/index.js` — the `/resume` dispatch branch only
- `worker/test/interaction.test.js` — one new test
- `test_interactive.py` — one new test
- `README.md` — two lines, named exactly in step 5

**Out of scope** (do NOT touch, even though they look related):
- `worker/commands.js`. **Do not remove `/resume` from `COMMANDS`.** Removing
  it would make `worker/test/commands.test.js`'s bidirectional check demand
  that the dispatch branch also disappear, and running `npm run register` would
  then delete the command from Discord entirely. Keeping the command
  registered, with an honest immediate answer, is the deliberate choice here.
- `worker/schema.sql`, and the `createResumePreview` / `confirmResumePreview` /
  `resumeComponentAction` / `currentConfirmedResume` functions and the
  `/internal/confirmed-resume` route. They are unreachable today but they are
  the foundation the operator is building on. **Do not delete them.**
- `resume_ingest.py` — same reason.
- `interactive_runner.py`. Its rejection of a resume task is correct behavior
  for a runner that cannot serve one; the fix belongs on the dispatch side.
  You will *test* its contract, not change it.
- `.github/workflows/interactive.yml`.

## Git workflow

- Branch: `advisor/013-resume-command-honesty`
- Conventional Commits. Recent examples from `git log`:
  `fix: unregister unsupported tailor command`,
  `fix: only trust an apply link the employer's own token vouches for`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baselines

Run both suites and write down the counts.

**Verify**:
`python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`; record `Ran N tests` as **PY_before**.
`cd worker && npm test` → `fail 0`; record `pass M` as **JS_before**.

### Step 2: Refuse `/resume` before a task or a workflow is spent

In `worker/src/index.js`, replace the body of the `resume` branch (lines
516–520 in the excerpt above) so it throws instead of building a task. Keep the
`payload.data?.name === "resume"` condition exactly as it is — the
`commands.test.js` guard greps for that literal string.

Produce this shape:

```javascript
    else if (payload.data?.name === "resume") {
      // The Worker can build a resume task; nothing can run one. Uploading a
      // new master CV needs interactive_runner.run_task to accept a kind other
      // than "tailor", and it accepts only "tailor" -- so a dispatched resume
      // task spent a full Actions run and answered "The task has no immutable
      // job snapshot.", which is both a failure and a description of the wrong
      // problem. Throwing here is caught by handleInteraction's own catch: the
      // user gets an immediate, accurate answer, and no task row, no encrypted
      // token and no workflow dispatch are spent on it.
      throw new Error(
        "Uploading a new master CV is not wired up yet. Edit resume.md in the " +
        "repository and commit it; the next tailoring run will use it."
      );
    }
```

Do not change the `task.kind === "resume"` ternary in the acknowledgement line
further down — it is now unreachable for this branch but harmless, and touching
it widens the diff for no gain.

**Verify**: `cd worker && npm test` → `fail 0`, with `pass` equal to JS_before.
Then `node -e "const s=require('fs').readFileSync('worker/src/index.js','utf8'); console.log(s.includes('kind: \"resume\"'))"`
run from the repository root → prints `false`.

### Step 3: Test that `/resume` costs nothing

Add one test to `worker/test/interaction.test.js`.

**Use the right harness.** Two families of test live in that file and only one
of them proves what this step needs:

- The tests named `"… refused before a workflow is spent"`
  (`worker/test/interaction.test.js:824` and `:840`) call the exported helpers
  `manualTaskFromModal` / `manualTaskFromCommand` **directly**. They assert a
  rejection and nothing about D1 or dispatch. Do **not** model on these.
- `"acknowledges a button interaction without waiting for the GitHub dispatch"`
  (`:309`) and `"a failed GitHub dispatch reports back to the user's
  interaction"` (`:332`) drive the real `worker.fetch(request, env, ctx)` using
  a `buttonInteraction()` helper defined just above them. That helper builds a
  genuinely Ed25519-signed `POST /interactions` request, an `env` carrying
  `DISCORD_PUBLIC_KEY_BASE64`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`,
  `DISCORD_USER_ID`, `DISCORD_APPLICATION_ID`, `GITHUB_REPOSITORY`,
  `GITHUB_TOKEN`, `TASK_ENCRYPTION_KEY` and a fake `DB`, and a
  `ctx: { waitUntil: (promise) => backgrounded.push(promise) }`. **This is the
  model.** Read `buttonInteraction()` in full before writing anything.

Write a `resumeCommandInteraction()` helper alongside it, reusing
`buttonInteraction()`'s signing and `env` construction but with a slash-command
payload instead of a button one — `type: 2`, `data: { name: "resume", options:
[{ name: "file", type: 11, value: "<id>" }], resolved: { attachments: { "<id>":
{ url: "https://cdn.discordapp.test/cv.pdf", filename: "cv.pdf" } } } }`, plus
the `guild_id`, `channel_id` and `member.user.id` values `authorizeInteraction`
requires (they must match the `env` ids, or the test will assert on an
authorization refusal instead of the one you want).

`buttonInteraction()`'s fake `DB` does not record the SQL it is given, so build
a recording one for this test. Two existing tests already do exactly that —
`const writes = []` at `:100` and `const executed = []` at `:562` — copy
whichever reads more clearly and push each `sql` string into an array in
`prepare(sql)`.

Stub `globalThis.fetch` and restore it in a `finally`, exactly as `:311-328`
does.

Name it:

```javascript
test("uploading a CV is refused before a task row or a workflow is spent", async () => {
```

It must assert all three:

1. The response is an ephemeral message (`type: 4`, `data.flags === 64`) whose
   `data.content` contains `"not wired up yet"`.
2. No recorded SQL string matches `/INSERT INTO tasks/` — no task row was
   written.
3. No `fetch` call was made to `api.github.com` — no workflow was dispatched.
   Assert on the recorded URLs, and assert `backgrounded.length === 0` as well,
   since a refusal happens before `background(ctx, dispatch)` is reached.

**Verify**: `cd worker && npm test` → `fail 0`, `pass` = JS_before + 1.

### Step 4: Pin the runner's task contract in Python

Add one test to `test_interactive.py` that documents, in the test suite, which
task kinds the runner can serve. Put it near the other `run_task` tests.

Name it:

```python
    def test_the_runner_serves_only_a_tailoring_task_and_says_so(self):
```

It must call `interactive_runner.run_task({"kind": "resume", "input": {"attachment": {"url": "https://example.test/cv.pdf", "filename": "cv.pdf"}}})`
inside `assertRaises(ValueError)` and assert the message mentions a job
snapshot. Write a docstring on the test explaining that this is the contract
`worker/src/index.js` must not violate: any new task kind needs a handler here
first, and this test is the reminder.

**Verify**: `python -m unittest test_interactive.py` → `OK`, one more test than
`test_interactive`'s contribution to PY_before.

### Step 5: Correct the two README lines that claim this works

Two lines in `README.md` present the upload path as built.

`README.md:213` currently reads:

```
- `resume_ingest.py` — normalizes raw resume text (from a `/resume` upload) into the form `resume.md` needs, with a preview step before anything is accepted as the new master.
```

Rewrite it to say plainly that the module exists but nothing calls it yet, and
that `/resume` currently answers with a refusal — the master resume is updated
by editing and committing `resume.md`.

`README.md:224` currently reads:

```
- `worker/commands.js` — the command set registered with Discord (`/resume`, `/tailor-jd`). Source of truth; `npm run register --prefix worker` applies it.
```

Add, in the same sentence style, that `/resume` is registered but not yet
implemented and answers with an immediate refusal.

Change nothing else in `README.md`. In particular do not attempt a general
README refresh — that is a separate, larger piece of work.

**Verify**: `grep -n "not wired up\|not yet implemented" README.md` → at least
2 matches. `git diff --stat README.md` → 2 files' worth of small edits, well
under 15 changed lines.

### Step 6: Full suites green, and say plainly that this is not live yet

**Verify**:
`python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`, `Ran (PY_before + 1) tests`.
`cd worker && npm test` → `fail 0`, `pass` = JS_before + 1.

Then, in your completion report, state this in as many words:

> **This change does not take effect on merge.** `worker/src/index.js` runs on
> Cloudflare, which is not updated by merging or pushing. Until the operator
> runs `cd worker && npx wrangler deploy`, `/resume` keeps creating a task and
> dispatching a workflow exactly as before. Every done criterion below can pass
> while the user-facing behavior is unchanged.

Do **not** run `wrangler deploy` yourself. Deploying is the operator's action:
it needs their Cloudflare credentials, and it ships whatever else is currently
unshipped in `worker/` alongside this change.

## Test plan

- **New Worker test**: `"uploading a CV is refused before a task row or a workflow is spent"`
  in `worker/test/interaction.test.js`. Covers: the ephemeral refusal, no D1
  task insert, no workflow dispatch.
- **New Python test**: `test_the_runner_serves_only_a_tailoring_task_and_says_so`
  in `test_interactive.py`. Covers the runner's kind contract.
- **Structural patterns**: for the Worker test, the existing
  `"a paste that is not a full posting is refused before a workflow is spent"`
  test — it already asserts the "nothing was spent" shape. For the Python test,
  any existing `run_task` test in `test_interactive.py`.
- **Existing tests that must keep passing unchanged**: everything in
  `worker/test/commands.test.js`, in particular
  `"registers exactly the commands the worker can serve"` (which asserts
  `COMMAND_NAMES` is exactly `["resume", "tailor-jd"]`) and
  `"every registered command name has a dispatch branch in the worker"`. If
  either fails, you have edited `worker/commands.js` or removed the
  `payload.data?.name === "resume"` literal — both are out of scope.

## Done criteria

ALL must hold:

- [ ] `grep -c 'payload.data?.name === "resume"' worker/src/index.js` returns `1`
- [ ] `grep -c 'kind: "resume"' worker/src/index.js` returns `0`
- [ ] `grep -c 'not wired up yet' worker/src/index.js` returns `1`
- [ ] `cd worker && npm test` → `fail 0`, `pass` = JS_before + 1
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
      → `OK`, `Ran (PY_before + 1) tests`
- [ ] `git diff --name-only` lists exactly: `README.md`, `interactive`-side
      `test_interactive.py`, `worker/src/index.js`, `worker/test/interaction.test.js`
      — and nothing else
- [ ] `git diff --name-only | grep -c "worker/commands.js\|worker/schema.sql\|resume_ingest.py\|interactive_runner.py"` returns `0`
- [ ] `plans/README.md` status row for 013 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The `worker/src/index.js` excerpt does not match what is quoted above.
- Any test in `worker/test/commands.test.js` fails. That means the command
  registration contract moved, which is out of scope.
- You find a production caller of `createResumePreview`,
  `confirmResumePreview`, `resumeComponentAction` or `prepare_resume_preview`
  that this plan's "unreachable" claim missed. If any of those is actually
  wired, the premise is wrong and the plan needs rewriting.
- You are tempted to implement the upload path rather than refuse it. Do not.
  It requires an operator decision about where a confirmed resume is stored
  (see "Maintenance notes"), and shipping half of it is what produced this
  finding.
- Step 3's Worker test passes against the *unfixed* branch — it is not
  exercising the change.

## Maintenance notes

- **Merging is not shipping.** The Worker half of this change reaches users
  only via `cd worker && npx wrangler deploy`, run by the operator. Until then
  `/resume` behaves exactly as it does today. The README edits and the Python
  test do land on merge; the behavior change does not. Treat the plan as DONE
  when the code and tests are in, and track the deploy separately — that is how
  the previous pass handled plans 004, 006 and 011, which are merged and, as
  far as this repository can tell, still undeployed.
- **What this defers, and why.** Finishing `/resume` end to end is task 2 of
  `docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md` and is
  specified in `docs/2026-08-10-resume-command-design.md` and
  `docs/superpowers/specs/2026-08-12-reliable-resume-ingestion-design.md`. It
  is blocked on one decision the operator has to make and has not: **where the
  confirmed resume lives.** The standing recommendation in those docs is to
  write it to D1 from the Discord-triggered path (which must not hold repo
  write permission), then have the daily digest workflow — which already has
  `contents: write` and already commits — sync it back into `resume.md` in its
  existing commit step. Nothing implements that sync today; there is no
  `resume_sync.py` in the repository.
- **A second known trap in that design**: Discord CDN attachment URLs expire
  after 24 hours (signed `ex`/`is`/`hm` query parameters). A confirmation flow
  that parks an upload for hours must download the bytes immediately rather
  than storing the URL.
- **What a reviewer should scrutinize**: that nothing was deleted. The value of
  this change is that it stops spending runs on a broken path *without*
  discarding the groundwork — `resume_previews`, `confirmed_resumes`,
  `createResumePreview`, `confirmResumePreview`, `resumeComponentAction`,
  `prepare_resume_preview` and `/internal/confirmed-resume` must all still be
  present after this lands.
- **When the feature is finished**, the refusal added in step 2 and both tests
  added here are the things to delete, and the Python test from step 4 becomes
  the place to assert the resume kind *is* served.
