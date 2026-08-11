# Plan 009 (SPIKE): Design the `/resume` and `/tailor-jd` command paths

> **Executor instructions**: This is a **design spike, not an implementation
> plan**. Its deliverable is a written design document plus a small number of
> targeted experiments — not a working feature. Do not build the feature. If
> you find yourself writing production code beyond the throwaway probes named
> in step 3, stop: you have left the scope of this plan.
>
> Follow the steps in order. Run every verification command. If anything in the
> "STOP conditions" section occurs, stop and report. When done, update the
> status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- worker/src/index.js interactive_runner.py resume_ingest.py .github/workflows/interactive.yml`
> If any of these changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first.

## Status

- **Priority**: P3
- **Effort**: M (spike only; the implementation it scopes is separate and larger)
- **Risk**: LOW (produces a document, changes no behavior)
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

The Worker advertises three interaction types. Only one of them works.

```javascript
    else if (payload.data?.name === "resume") task = { kind: "resume", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
    else if (payload.data?.name === "tailor-jd") task = { kind: "tailor-jd", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
```

Both branches build a task, write it to D1, and dispatch the GitHub workflow.
The workflow then runs `interactive_runner.py`, whose `run_task` begins:

```python
def run_task(task: dict) -> dict:
    """Validate the exact archived JD before any later tailoring can run."""
    snapshot = task.get("input", {}).get("job_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("The task has no immutable job snapshot.")
```

A `resume` or `tailor-jd` task has no `job_snapshot`, so it always raises. The
runner catches it and reports back
`"Interactive task failed safely: The task has no immutable job snapshot."`
The user uploaded a PDF, waited, and got an error that describes an internal
data-shape problem.

This is not neglect — it is the unfinished half of a deliberate design.
`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md` lists it as
task 2 of 5: "Add confirmed resume ingestion, fact extraction, and
preview/confirmation artifacts." Supporting pieces already exist:
`resume_ingest.prepare_resume_preview` (written, tested, imported by nothing in
production), `pypdf` in `requirements.txt`, and a 24-hour task expiry that
`taskExpiryMilliseconds` grants specifically to `kind === "resume"`.

Two constraints from that same design document make this harder than it looks,
and are the reason this is a spike rather than a build plan:

- "`/resume` is the sole master-CV upload trigger; plain Discord attachments
  remain inert."
- "Original uploaded PDFs are discarded after extraction; `resume.md` is
  updated only after confirmation."

"Updated only after confirmation" means a second round-trip with the user, and
`resume.md` lives in a git repository that the interactive workflow currently
cannot write to. Getting that wrong quietly corrupts the single source of truth
that every tailored CV is built from. It is worth designing before building.

## Current state

Files involved:

- `worker/src/index.js` — accepts the commands; `attachmentFrom` at lines
  188–193 extracts the upload:

```javascript
function attachmentFrom(payload) {
  const option = payload.data?.options?.find((item) => item.type === 11);
  const attachment = option && payload.data?.resolved?.attachments?.[option.value];
  if (!attachment?.url) throw new Error("Attach a PDF to this command.");
  return { url: attachment.url, filename: attachment.filename || "resume.pdf", contentType: attachment.content_type || "", size: attachment.size || 0 };
}
```

- `worker/src/index.js:21-23` — the longer expiry already reserved for uploads:

```javascript
export function taskExpiryMilliseconds(kind) {
  return kind === "resume" ? 24 * 60 * 60 * 1000 : 14 * 60 * 1000;
}
```

- `interactive_runner.py:73-104` — `run_task`, snapshot-only, no `kind`
  handling anywhere in the file (`grep -n "kind" interactive_runner.py` returns
  nothing).

- `resume_ingest.py` in full — the piece that exists and is unused in
  production:

```python
def prepare_resume_preview(raw_text: str, filename: str) -> dict:
    """Create a human-reviewable Markdown preview without retaining the PDF."""
    readable = raw_text.strip()
    if len(readable) < 40:
        raise ValueError("The uploaded PDF contains too little readable text to review.")
    markdown = re.sub(r"\n{3,}", "\n\n", readable)
    if not markdown.startswith("#"):
        first, _, rest = markdown.partition("\n")
        markdown = f"# {first.strip()}\n{rest}".rstrip() + "\n"
    else:
        markdown = markdown.rstrip() + "\n"
    return {
        "markdown": markdown,
        "metadata": {
            "schema_version": 1,
            "filename": filename,
            "source_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        },
        "warnings": [],
    }
```

- `.github/workflows/interactive.yml` — note the permission, which is the crux
  of the `resume.md` write problem:

```yaml
permissions:
  contents: read
```

There is no commit step in that workflow, and it passes only
`INTERACTIVE_WORKER_URL`, `WORKER_CALLBACK_SECRET`, and `TASK_ID`.

- **No command registration exists anywhere in the repository.** The Worker
  never calls Discord's `applications/{id}/commands` endpoint, and there is no
  registration script. Whether `/resume` and `/tailor-jd` are registered with
  the Discord application at all is unknown from the code and is the first thing
  this spike must establish.

Repo conventions that apply here:

- Design records live in `docs/superpowers/plans/` and `docs/superpowers/specs/`,
  named `YYYY-MM-DD-<slug>.md`. Look at
  `docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md` for the
  house style: short, declarative, a Goal line, an Architecture line, explicit
  Global Constraints, then numbered tasks.
- Every behavior is test-first (`"keep every behavior test-first"` is stated in
  that document's header).
- Fail-soft is the project's governing instinct: a failure costs the user the
  feature, never the run, and always says why.

## Commands you will need

| Purpose      | Command                                                   | Expected on success        |
|--------------|-----------------------------------------------------------|----------------------------|
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0               |
| Worker tests | `cd worker && node --test`                                | 0 fail                     |
| Probe script | `python <throwaway script in a scratch dir>`              | as described in step 3     |

## Scope

**In scope**:

- `docs/<YYYY-MM-DD>-resume-command-design.md` (create) — the deliverable.
- Throwaway probe scripts written **outside the repository** (a temp directory).
  They are not committed.

**Out of scope** (do NOT modify):

- `worker/src/index.js`, `interactive_runner.py`, `resume_ingest.py`,
  `.github/workflows/interactive.yml`, `requirements.txt` — every production
  file. This spike changes no behavior.
- `resume.md` — under no circumstances. It is the master source every tailored
  CV is built from.
- `docs/superpowers/**` — historical design records, not to be edited.
- Registering, modifying, or deleting Discord slash commands. Investigating
  whether they are registered is in scope; changing them is not — that is an
  outward-facing change for the operator to make.

## Git workflow

- Branch: `advisor/009-spike-resume-command`
- Commit style: Conventional Commits.
  Suggested message: `docs: design the /resume and /tailor-jd command paths`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Establish what happens today, precisely

Write down, with file:line evidence, the exact path a `/resume` invocation
takes from Discord to the error the user sees. Confirm each hop by reading the
code, not by assuming:

1. Which Worker branch handles it, and what task row it writes.
2. What `interactive_runner.py` does with that task.
3. What text reaches the user, and through which endpoint.

**Verify**: you can quote the exact error string the user receives and name the
line that produces it.

### Step 2: Answer the open questions

These are the decisions the design must make. Answer each from evidence, and
mark clearly which ones need the operator rather than the code.

1. **Are the commands registered with Discord?** There is no registration code
   in the repository. Determine whether registration is manual, and record what
   the design needs: a committed registration script, or a documented manual
   step. (Do not register anything.)
2. **How does the runner obtain the uploaded file?** `attachmentFrom` stores a
   Discord CDN URL in the task input. Discord CDN attachment URLs carry
   expiring signed query parameters. Establish: how long they last, whether
   they survive the D1 round-trip and workflow dispatch delay, and whether the
   download needs authentication. This is the single most likely thing to make
   a naive implementation fail intermittently.
3. **Where does the confirmed resume go?** The design says `resume.md` is
   updated only after confirmation, but `interactive.yml` runs with
   `contents: read` and has no commit step. Enumerate the options and recommend
   one: (a) grant `contents: write` and commit from the interactive workflow;
   (b) store the confirmed resume in D1 and have the runner read it from there;
   (c) have the runner open a pull request. State the trade-offs — concurrency
   with the digest workflow's own commits, auditability, and what happens if
   two uploads race.
4. **What does confirmation look like?** A preview plus a confirm button is a
   second interaction round-trip, which needs a `custom_id` scheme, a new task
   kind, and a way to hold the pending preview between the two. Sketch it.
   Note that `custom_id` is capped at 100 characters — the existing button uses
   a 32-hex-character hash for exactly this reason.
5. **What is `/tailor-jd` actually for?** The Worker treats it identically to
   `/resume` (an attachment upload), but the name and the error message in
   `tailoringInputFromSnapshotRow` — "A full job description is required; use
   /tailor-jd to provide it." — say it supplies a missing JD for a job whose
   snapshot has `jd.status != "captured"`. Reconcile those two readings.
   Determine how the supplied JD would be bound to the right job, given that
   the command carries no job reference today.
6. **What is the failure mode for each new path?** For each, state what the user
   sees and what state is left behind. The project's rule is that failure costs
   the feature, never the run, and always explains itself.

**Verify**: every question has an answer or is explicitly marked "needs a
decision from the operator", with the options laid out.

### Step 3: Probe only what reading cannot settle

Write throwaway scripts **in a temp directory outside the repository**. Probe
only question 2 (attachment URL lifetime and access) and, if needed, the shape
of a real Discord `APPLICATION_COMMAND` interaction payload with an attachment
option.

Use a file the operator provides or a PDF you generate locally with the
`reportlab` already installed. **Do not upload the operator's real resume
anywhere, and do not commit any probe output.**

**Verify**: probe results are recorded in the design document as observations
with dates, matching the repo's convention ("verified live 24 Jun 2026").

### Step 4: Write the design document

Create `docs/<today>-resume-command-design.md`. Follow the house style of
`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`: a Goal line,
an Architecture line, Global Constraints, then numbered tasks.

It must contain:

- **What is broken today** — one paragraph, with the file:line evidence from
  step 1.
- **Decisions** — the answers to all six questions from step 2, each with its
  reasoning. Where a decision belongs to the operator, say so and give a
  recommendation.
- **The task shape** — how `run_task` should dispatch on task kind, written as
  a sketch, not an implementation. Include what `interactive_runner.py`'s
  contract becomes when it handles three kinds instead of one.
- **Test plan for the implementation** — which behaviors get tests, in which
  file, following which existing test. `test_interactive.py`'s
  `TestResumeIngestion` and `TestInteractiveRunner` are the models.
- **Open questions that remain** — honestly listed. A spike that ends with no
  open questions usually means they were assumed away.
- **What this design explicitly does not do** — at minimum: it does not change
  the daily digest, does not auto-apply to jobs, and does not put a resume PDF
  in the repository.

**Verify**: `test -e docs/<today>-resume-command-design.md` and the document
answers all six questions.

### Step 5: Confirm nothing changed

**Verify**:
- `python -m unittest test_digest.py test_interactive.py` → `OK`, count unchanged
- `cd worker && node --test` → 0 fail, count unchanged
- `git status --porcelain` → exactly one new file, the design document

## Test plan

No code tests — this spike ships a document. The test plan for the *implementation*
is itself part of the deliverable (step 4).

Verification is: both existing suites still pass unchanged, and the design
document exists and answers every question in step 2.

## Done criteria

ALL must hold:

- [ ] `docs/<YYYY-MM-DD>-resume-command-design.md` exists
- [ ] It answers all six questions from step 2, or marks them as operator decisions with options
- [ ] It contains the exact user-visible error string from step 1, with its file:line
- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with its
      count unchanged (this plan adds no tests)
- [ ] `cd worker && node --test` exits 0 with 0 fail and its count unchanged
- [ ] `git status --porcelain` lists exactly one added file and no modifications
- [ ] No resume content, PDF, or probe output is committed
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- You are about to modify any production file. This spike modifies none.
- You are about to register, edit, or delete a Discord slash command. That is an
  outward-facing change and belongs to the operator.
- You are about to upload, transmit, or commit the operator's resume.
- Question 3 (where the confirmed resume is stored) cannot be answered without
  granting `contents: write` to the interactive workflow. That is a meaningful
  permission escalation on a workflow triggered by a Discord button — write it
  up as a recommendation with its risks, do not implement it.
- The design starts to require a new runtime dependency or a new service. The
  project's stated principle is "no framework, no queue, no browser automation";
  a design that needs one has to justify it explicitly.

## Maintenance notes

- `taskExpiryMilliseconds` already grants `kind === "resume"` a 24-hour window
  versus 14 minutes for tailoring. Any confirmation flow must fit inside it, or
  change it deliberately.
- Plan 004 prunes `tasks` rows 7 days past expiry. A confirmation flow that
  parks a pending preview in `tasks` inherits that retention — check the
  interaction before relying on it.
- `resume_source.validate_tailored_resume` exists and is tested but is unused in
  production on `main`. The branch `codex/discord-tailoring` wires it into the
  tailoring path. If that branch has merged by the time this spike runs, read it
  first: it may already have changed what "the confirmed master resume" means.
- Reviewer should read the design document for one thing above all: whether the
  path that writes `resume.md` can ever corrupt it. Everything else in this
  feature is recoverable; that is not.
