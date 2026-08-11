# Reliable resume ingestion and state persistence

**Status:** Approved design awaiting user review

## Goal

Make `/resume` safely replace the confirmed master CV after a human preview
and confirmation. D1 is the live, per-user source of truth; the daily digest
mirrors a confirmed D1 record to `resume.md` for version history and recovery.
At the same time, make the digest's state commit resilient to normal `main`
branch movement. No workflow triggered by a Discord interaction receives
repository write permissions.

This release unregisters `/tailor-jd`. Its current task shape always fails in
the runner, so it must not be advertised until its distinct handler exists.

## Scope and boundaries

Included:

- Retry-safe persistence in `.github/workflows/digest.yml`.
- D1 tables and authenticated internal Worker endpoints for confirmed resumes
  and short-lived pending previews.
- `/resume` PDF download, text extraction, preview, confirmation, and D1
  write.
- Tailoring reads the current confirmed resume from D1 when it exists.
- One-way D1-to-`resume.md` synchronization during the daily digest commit.
- Removing `tailor-jd` from `worker/commands.js` and updating command tests.

Excluded:

- `/tailor-jd`, modal JD recovery, attachments other than a resume PDF, and
  live command registration or deployment.
- Storing PDF bytes, raw Discord attachment URLs after processing, or resume
  text in Git before confirmation.
- Granting `contents: write` to `interactive.yml`.

## Design

### 1. Safe daily persistence

The digest workflow remains the only GitHub Actions workflow with
`contents: write`. Its persistence step stages `seen.json`, `stats.json`,
`jobs/`, and a changed `resume.md`, creates one commit, and pushes it with a
bounded retry loop.

Each retry fetches `origin/main`, rebases the local state commit onto it, and
pushes. A rebase conflict or exhausted retry budget fails the workflow; it
never reports a successful run while dropping generated state. The existing
`job-digest` concurrency group continues to prevent two digest runs racing.
The process must not retry a failed application/digest step, only the final
Git persistence operation.

Before staging, the digest calls a signed Worker endpoint that returns the
current confirmed resume for the Worker's configured private Discord user. It
replaces `resume.md` only when D1 has a confirmed record whose content differs.
A missing D1 record does nothing, preserving the tracked legacy resume during
migration.

### 2. D1 resume state

Two narrowly scoped tables are added:

- `confirmed_resumes`: one row per Discord user, containing Markdown, a full
  SHA-256 digest, an integer version, and an update timestamp.
- `resume_previews`: a pending preview ID, user ID, Markdown, full SHA-256,
  base confirmed-resume version, status, and expiry. It has no Discord token
  or PDF URL.

`resume_previews` expires after 24 hours and is deleted immediately after
confirmation or cancellation. Retention pruning also removes any expired
record, so extracted resume text cannot persist beyond its pending window.

Confirmation is compare-and-set: the Worker accepts it only if the preview is
pending and its recorded base version still equals the current confirmed
resume version. A second, newer confirmation therefore cannot be overwritten
by an older preview. The Worker changes the preview state and the confirmed
resume in one D1 transaction/batch; an incomplete write is not observable.

### 3. Internal Worker contract

All internal endpoints remain HMAC-authenticated and are usable only by the
trusted Actions runner. The interactive runner receives resume material
through task-scoped endpoints; the daily digest has one narrow synchronization
read that always resolves the Worker-configured private user and accepts no
user ID from its caller.

- `GET /internal/confirmed-resume`: returns the current confirmed Markdown
  only for the Worker-configured private Discord user; it is used by the daily
  synchronization step.
- `GET /internal/tasks/{id}/resume`: returns the current confirmed Markdown
  for that task's authorized user, or a clear not-found result.
- `PUT /internal/tasks/{id}/resume-preview`: stores the extracted Markdown
  after stripping the attachment URL from task input, returning a preview ID
  and digest.
- `POST /internal/resume-previews/{id}/confirm`: atomically confirms a
  pending preview when its digest and base version match.
- `POST /internal/resume-previews/{id}/cancel`: deletes a pending preview.

The task input may contain a Discord CDN URL only until the runner starts.
The runner downloads it once, extracts text, then replaces that input with
non-sensitive preview metadata. Neither PDF bytes nor a reusable URL is
persisted.

### 4. `/resume` interaction flow

1. The Worker acknowledges `/resume` immediately and dispatches an existing
   24-hour `resume` task.
2. The runner downloads the attached PDF immediately, rejects an invalid or
   unreadable document, extracts text with `pypdf`, and uses
   `prepare_resume_preview` to normalize it.
3. The runner stores the pending Markdown through the task-scoped endpoint.
   It returns a private Discord preview truncated safely below the message
   limit, with `Confirm` and `Cancel` components.
4. Component IDs include both the preview ID and the first 32 hexadecimal
   characters of the full source digest. The Worker routes these prefixes
   before the existing `tailor:` button parser.
5. Confirm creates a short-lived `resume-confirm` task. The runner calls the
   compare-and-set endpoint, then reports a private success/failure message.
   Cancel deletes the preview immediately and reports a private cancellation.
6. Future tailor tasks ask the Worker for a confirmed D1 resume. Until the
   first confirmation, they fall back to the checked-out `resume.md`; this is
   an explicit migration compatibility path, not a second live source of
   truth.

`deliverResult` must pass Discord components in both its JSON and multipart
PDF branches. Result delivery remains ephemeral.

### 5. Command registration

`worker/commands.js` defines only `/resume` in this release. The registration
script's bulk replacement will remove `/tailor-jd` when the operator runs it
after the Worker deploy. The unfinished Worker branch for an old slash command
may remain defensive, but it is not treated as supported functionality.

## Failure behavior

All new paths are fail-soft and explicit:

- Download, PDF extraction, and preview validation failures leave no preview
  and do not change the confirmed resume.
- Missing D1 state makes legacy tailoring use `resume.md`; an empty or absent
  legacy file produces the existing clear error.
- A stale/digest-mismatched confirm reports that the preview is no longer
  current and leaves the newer confirmed resume intact.
- A D1 write failure leaves the prior confirmed row and preview state intact
  (or retries safely); it never produces a partial resume.
- A digest rebase conflict or retry exhaustion fails the workflow visibly,
  leaving remote state unchanged rather than silently discarding it.

## Tests and verification

Python tests cover immediate download/extraction behavior, no URL retained
after preview storage, preview truncation, confirmation success, hash mismatch,
and stale confirmation after a newer confirmed version. Existing button
tailoring tests prove legacy behavior remains unchanged before the first D1
record and D1-backed tailoring is used afterward.

Worker tests cover the schema helpers and compare-and-set state transition;
custom-ID routing; component delivery through JSON and multipart payloads;
task-scoped authorization; expiry/cancellation deletion; and a command-list
assertion that `/tailor-jd` is absent while every registered command has a
handler.

Workflow validation exercises the persistence script in a disposable Git
repository: one successful rebase-and-push retry, a bounded push failure, and
a conflict that returns non-zero. The existing full suites remain required:

```text
python -m unittest test_digest.py test_interactive.py
cd worker && node --test
```

## Rollout and rollback

Rollout order is: merge and push, deploy the Worker, then run command
registration. Do not register commands before the Worker deployment.

Rollback is safe: redeploy the previous Worker and restore the prior command
definition if needed. D1 confirmed records are additive and do not alter the
tracked `resume.md` until a later daily digest synchronization; the previous
checked-in resume remains recoverable through Git history. No secret,
permission, or live Discord configuration change is included in the code
change.
