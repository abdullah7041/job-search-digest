# `/resume` and `/tailor-jd` Command Design

> **Spike deliverable.** This document is the output of plan
> `plans/009-spike-resume-and-tailor-jd-commands.md`. It changes no
> behavior — no production file was modified to produce it. It sketches the
> implementation that a follow-up build plan should execute test-first.

**Goal:** Let `/resume` become the real master-CV upload-and-confirm trigger,
and give `/tailor-jd` a coherent job-bound purpose, without ever risking
`resume.md` corruption or a resume-text leak.

**Architecture:** Unchanged from
`docs/superpowers/plans/2026-08-03-discord-on-demand-tailoring.md`: Cloudflare
Worker + D1 coordinate short-lived task state; GitHub Actions
(`interactive_runner.py`) does the PDF/text work; the private repo holds
`resume.md` and immutable job snapshots only. This design adds no new
service, framework, or runtime dependency — everything below runs on
`requests`, `pypdf`, and `reportlab`, already in `requirements.txt`.

## Global Constraints (inherited, restated because they gate every decision below)

- `/resume` is the sole master-CV upload trigger; plain Discord attachments
  remain inert.
- Original uploaded PDFs are discarded after extraction; `resume.md` is
  updated only after confirmation.
- No PDF is generated without a full JD.
- Fail-soft: a failure costs the user the feature, never the run, and always
  says why.
- (New, made explicit by this spike) The confirmed master resume is the
  single most valuable piece of state in the repository. Every design choice
  below is evaluated first against "can this write a bad `resume.md`?", and
  only second against convenience.

---

## 1. What is broken today

A `/resume` or `/tailor-jd` invocation follows this exact path:

1. Discord posts the interaction to the Worker. `handleInteraction` in
   `worker/src/index.js` matches `payload.data?.name === "resume"` (line 280)
   or `"tailor-jd"` (line 281) and builds
   `task = { kind: "resume"|"tailor-jd", userId, input: { attachment: attachmentFrom(payload) } }`,
   where `attachmentFrom` (`worker/src/index.js:188-193`) pulls a Discord CDN
   URL, filename, content type, and size out of the interaction's resolved
   attachments.
2. `createTask` (`worker/src/index.js:168-176`) writes that task to D1 with
   `status = 'received'` and an expiry from `taskExpiryMilliseconds(task.kind)`
   — 24 hours for `resume`, 14 minutes for everything else
   (`worker/src/index.js:21-23`). `dispatchWorkflow`
   (`worker/src/index.js:178-186`) then fires `interactive.yml` via
   `workflow_dispatch`.
3. The workflow runs `interactive_runner.py`. `main()` → `process_task()`
   (`interactive_runner.py:128-146`) fetches the task body and calls
   `run_task(task)`. `run_task` (`interactive_runner.py:73-77`) begins:

   ```python
   def run_task(task: dict) -> dict:
       """Validate the exact archived JD before any later tailoring can run."""
       snapshot = task.get("input", {}).get("job_snapshot")
       if not isinstance(snapshot, dict):
           raise ValueError("The task has no immutable job snapshot.")
   ```

   `run_task` has no branch on `task["kind"]` — `grep -n "kind"
   interactive_runner.py` returns nothing. A `resume`/`tailor-jd` task's
   `input` is `{"attachment": {...}}`, never `{"job_snapshot": {...}}`, so
   this always raises.
4. `process_task`'s `except` clause (`interactive_runner.py:138-142`) catches
   it and builds
   `result = {"status": "failed", "content": f"Interactive task failed safely: {str(error)[:1000]}"}`.
   The exact string the user receives is:

   > **`Interactive task failed safely: The task has no immutable job snapshot.`**

   raised at `interactive_runner.py:77`, formatted at `interactive_runner.py:141`.
5. That result is POSTed back to the Worker's `/internal/tasks/{id}/result`,
   claimed by `claimTaskResult`, and delivered by `deliverResult`
   (`worker/src/index.js:245-258`), which `PATCH`es
   `https://discord.com/api/v10/webhooks/{DISCORD_APPLICATION_ID}/{token}/messages/@original`
   with `{content: result.content, flags: 64}` (ephemeral). The user sees the
   string above as an ephemeral Discord message, roughly 5 seconds to a few
   minutes after uploading a PDF they expected to be read.

**This is not the whole picture.** `git ls-files resume.md` confirms
`resume.md` is tracked, and it is 4.3 KB on disk in this worktree — not
empty. So the *third* interaction type, the "Tailor CV" button
(`payload.type === 3`, handled at `worker/src/index.js:269-279`, verified by
`snapshot["jd"]["status"] == "captured"` and dispatched into `run_task` with a
real `job_snapshot`), is the one path that works end-to-end today, provided
the JD was captured. The plan's framing — "three interaction types, only one
works" — holds.

---

## 2. Decisions

### Q1 — Are the commands registered with Discord?

**Unknown from the code, and this spike does not check live.** There is no
registration script anywhere in the repository
(`grep -rniE "applications/.*commands|register.*command"` across `*.js`,
`*.py`, `*.md`, `*.yml` returns only the unrelated webhook-delivery endpoint
at `worker/src/index.js:247`). `README.md` documents exactly one Discord
setup step (line 125: add `DISCORD_WEBHOOK_URL` as an Actions secret) and says
nothing about slash-command registration. Whatever registered `/resume` and
`/tailor-jd` — if anything did — happened outside this repository: a manual
call through the Discord Developer Portal, or a one-off script that was never
committed.

**Recommendation:** commit a small, idempotent registration script (Node,
alongside the Worker, using `PUT
https://discord.com/api/v10/applications/{id}/commands` to bulk-overwrite
guild commands) and document running it once after any command-shape change,
including the `job_ref` option this design proposes for `/tailor-jd` (Q5).
This is the operator's action to take, not this spike's — the script should
be written, reviewed, and run by the operator in the build phase, never
executed by an agent.

### Q2 — How does the runner obtain the uploaded file?

`attachmentFrom` stores only a CDN URL — `https://cdn.discordapp.com/attachments/.../filename?ex=...&is=...&hm=...`
in modern Discord payloads, per the publicly documented attachment schema
(confirmed structurally by the local probe below; the actual signed-parameter
lifetime was not tested live). **Design the problem away rather than solving
it:** never store the file, never re-fetch it, and never let the URL survive
past the runner's first seconds of execution. The two Global Constraints
already require this for `/resume` ("original uploaded PDFs are discarded
after extraction"), so the design should apply the identical rule to
`/tailor-jd`'s attachment:

1. The Worker still only stores the URL in `input_json` (unavoidable — it
   cannot download the file itself without adding an outbound fetch to the
   hot interaction path, which risks the 3-second Discord ACK deadline;
   that's why `type: 5` deferred-response exists).
2. `interactive_runner.py`, in the first steps of handling `kind ==
   "resume"` or `kind == "tailor-jd"`, downloads the URL with `requests.get`
   and extracts text (`pypdf.PdfReader`) immediately, before doing anything
   else. The URL is used exactly once, within seconds of the runner
   starting.
3. Under that design, the URL only has to survive `dispatchWorkflow`'s
   GitHub API call plus Actions' queue-to-start latency — typically seconds,
   worst case a few minutes on a busy runner pool. That is comfortably
   inside any published Discord CDN URL lifetime, but "comfortably inside"
   is an assumption, not a measurement.

**Open question, not settled by this spike (see Hard Limits below):** the
exact lifetime and auth requirements of Discord's `ex`/`is`/`hm` signed CDN
parameters. Settling it requires a live Discord interaction (upload a real
attachment, capture the URL, wait, then attempt an unauthenticated fetch) —
exactly the kind of live probe this spike is barred from making. **This is an
explicit OPEN QUESTION requiring operator authorization for a live test**,
not a fact this document asserts.

### Q3 — Where does the confirmed resume go?

`interactive.yml` runs with `permissions: contents: read` (line 12) and has
no commit step — unlike `.github/workflows/digest.yml`, which has
`contents: write` (line 9) and a commit+push step (lines 46-56). Three
options, as the plan requires:

- **(a) Grant `contents: write` to `interactive.yml` and commit from there.**
  This is the option this spike is **explicitly barred from recommending as
  implementable** (STOP condition: "Question 3 cannot be answered without
  granting `contents: write` ... write it up as a recommendation with its
  risks, do not implement it"). The risk is concrete, not abstract:
  `digest.yml`'s push (line 53, `git push`) has **no retry and no rebase
  step** — a bare `git push` that fails on a non-fast-forward is left
  failed. `digest.yml`'s `concurrency: group: job-digest` (lines 12-14) does
  **not** cover `interactive.yml` (`concurrency: group:
  interactive-${{inputs.task_id}}`, line 15 of `interactive.yml`), so a
  resume-confirm commit landing between the digest workflow's checkout and
  its push would fail the digest workflow's push, not just the interactive
  one — silently dropping that day's `seen.json`/`jobs/` update. Any design
  that grants write access here must also fix `digest.yml`'s push to retry
  with rebase, which is itself a small production change worth calling out
  explicitly rather than smuggling in as a side effect.
- **(b) Store the confirmed resume in D1; runner reads it from there.**
  Requires changing `_tailored_pdf`'s read path
  (`interactive_runner.py:32-36`, currently `open(resume_path,
  encoding="utf-8").read()` against the checked-out file) to instead fetch
  from a new authenticated Worker endpoint. No repo write permission needed
  anywhere. Concurrency is solved for free (D1 already does atomic
  claims elsewhere, e.g. `claimTaskResult`). Loses git history / auditability
  of resume changes — D1 is not backed up the way the git repo is, and a bad
  write has no revert-by-`git revert`.
- **(c) Have the runner open a pull request.** Needs `contents: write` (to
  push a branch) **and** `pull-requests: write` — a broader grant than (a),
  not a narrower one. Buys a human review gate and full git history, at the
  cost of resume updates no longer being immediate (the operator must merge
  before the *next* tailoring run picks up the new resume) — which
  contradicts the presumed use case of "upload resume, tailor a JD five
  minutes later."

**Recommendation:** (b), with an explicit note that this is exactly the
permission-escalation trade the STOP condition flags, so it is the
**operator's decision**, not this spike's. (b) is recommended over (a)
because it needs no new repository write permission on a workflow triggered
by an untrusted-input surface (a Discord interaction), and it sidesteps the
`digest.yml` push race entirely rather than requiring a coordinated fix to
two workflows. It is recommended over (c) because (c) needs a **superset**
of (a)'s permissions for a benefit (async human review) the single-operator
project doesn't clearly need — this is a personal tool gated to one Discord
user (`authorizeInteraction`, `worker/src/index.js:7-12`), not a team
workflow.

### Q4 — What does confirmation look like?

A preview-then-confirm round trip needs: a place to park the pending preview,
a `custom_id` scheme for the confirm/cancel buttons, a new task kind (or two),
and a way to bind the confirm click back to the specific preview.

**Two hazards Q2's file-handling answer doesn't cover, found by reading the
delivery path, not assumed:**

- **`deliverResult` cannot send a button today.** `worker/src/index.js:245-258`
  PATCHes `@original` with only `content` and, optionally, one PDF file —
  there is no `components` field in either branch. Sending a preview *with* a
  confirm button requires extending the callback result schema (`result.components`,
  passed through in both the JSON and multipart branches). This is a required
  Worker change this design must name, not an implementation detail that
  falls out of the runner.
- **The button-interaction branch will collide with a new custom_id
  prefix.** `worker/src/index.js:269-279` unconditionally calls `buttonTask(payload)`
  for every `payload.type === 3` interaction, and `buttonTask`
  (`worker/src/index.js:14-19`) throws `"Invalid tailoring button."` for any
  `custom_id` that doesn't match `^tailor:([0-9a-f]{32})$`. A confirm button
  with a *different* prefix must be dispatched on **before** `buttonTask` is
  called, not routed into the existing tailor-only branch.

**Sketch, using the existing 32-hex-hash convention** (the plan's own
citation: "the existing button uses a 32-hex-character hash for exactly this
reason" — `custom_id` is capped at 100 characters):

1. `/resume` uploads a PDF → task `kind: "resume"`. The runner downloads and
   extracts text (Q2), calls the existing, already-tested
   `prepare_resume_preview(raw_text, filename)`
   (`resume_ingest.py:9-28`, currently written and unit-tested but unused in
   production). That function already returns
   `metadata.source_sha256` — the sha256 of the *extracted raw text*, before
   Markdown normalization.
2. The runner's result carries `status: "completed"`, the rendered preview
   as `content` (Discord messages cap at 2000 chars; long resumes need
   truncation with a "preview shortened" note — flagged, not solved, below),
   and two new buttons: `resume-ok:<source_sha256[:32]>` and
   `resume-cancel:<source_sha256[:32]>`. The **hash in the button is the
   anti-corruption primitive**: whatever gets confirmed must be the exact
   text that was previewed, byte for byte.
3. The pending preview's full Markdown is parked in the `tasks` row itself
   (`input_json` or a new column) under the *same* task id, with `status`
   left non-terminal so plan 004's expiry-based prune doesn't reap it
   mid-flight — the 24-hour `resume` expiry is what actually bounds this
   wait, not the 14-minute default.
4. **The interaction token from step 1-2 is dead by the time the user
   clicks confirm.** This is the load-bearing fact the maintenance note
   ("must fit inside the 24h window") understates: the Worker's own test
   (`worker/test/interaction.test.js:48-51`, literally named *"expires a
   tailoring interaction before Discord's token window"*) exists because
   Discord webhook tokens are valid for roughly 15 minutes, and
   `taskExpiryMilliseconds` (`worker/src/index.js:21-23`) deliberately keeps
   the 14-minute default *under* that window. The 24-hour grant for `kind
   === "resume"` sits **outside** that window on purpose, which means the
   confirm click cannot be delivered by patching the original message hours
   later — it arrives as a **brand-new interaction** with its own fresh
   token. That's fine (Discord buttons on an old message still fire live
   interactions no matter how old the message is), but it means the confirm
   flow is two independent Worker-to-Discord round trips, not one long
   session, and the design must not assume any shared token state between
   them.
5. Confirm click → new `payload.type === 3` interaction, `custom_id`
   matches `^resume-ok:([0-9a-f]{32})$` (checked *before* `buttonTask`, per
   the collision hazard above) → Worker creates a `kind: "resume-confirm"`
   task referencing the original task id, expiry short (14 min is fine — this
   leg is fast). The runner re-reads the parked preview, **re-verifies the
   hash still matches what's stored** (defends against a second `/resume`
   upload racing the first — see below), and only then performs the Q3 write.
6. Cancel click, or 24-hour expiry with no click, does nothing to
   `resume.md`, and the parked preview is deleted (or expires and is pruned
   by plan 004) — resolving the retention gap below.

**A retention gap this design must close, not inherit:** the Global
Constraint "original uploaded PDFs are discarded after extraction" says
nothing about the *extracted text*. If the full resume Markdown sits in
`tasks.input_json` for up to 24 hours (pending confirm) plus plan 004's
7-day post-expiry prune window, that's over a week of a user's full resume
text sitting in D1 in plaintext. **Recommendation:** delete the parked
preview's content (not just mark the task terminal) the moment it is
confirmed or cancelled, rather than relying on the 7-day prune as the only
cleanup path.

**Two-upload race:** if a second `/resume` lands while a first preview is
still pending confirm, both create independent tasks with independent
hashes — no shared mutable state to corrupt at *this* stage. The danger is
at confirm time: if task A's confirm click is delivered after task B's
resume has already been written, A's stale hash check (step 5) must compare
against the *current* `resume.md`/D1 state, not just its own parked copy, or
a stale confirm could silently revert a newer upload. This needs a single-
writer claim on the confirm write path, analogous to `claimTaskResult`'s
atomic `UPDATE ... WHERE status IN (...)` pattern already used for delivery
claims (`worker/src/index.js:150-158`).

### Q5 — What is `/tailor-jd` actually for?

Two readings exist in the code today, and they are not actually in tension —
one is finished, one is not:

- The **error message** at `worker/src/index.js:58`,
  `"A full job description is required; use /tailor-jd to provide it."`,
  fires from `tailoringInputFromSnapshotRow` when a snapshot's
  `jd.status !== "captured"`. Its intent: a job whose auto-scraped JD came
  back empty or truncated (`job_archive.py`'s
  `test_card_keeps_tailor_button_for_missing_jd_to_start_recovery` confirms
  the "Tailor CV" button is deliberately **never disabled** even when the JD
  is missing — `job_archive.py:67-97`, footer gains "· Job description
  needed before tailoring" but `disabled: False` at line 93) — is meant to
  be *recoverable* by supplying the JD out of band.
- The **Worker's current handling** (`worker/src/index.js:281`) treats
  `/tailor-jd` exactly like `/resume`: an attachment upload, with no job
  reference anywhere in the task. This is simply unfinished, not a
  competing design — there is no code path today that could bind an
  uploaded JD to a specific job at all.

**Reconciliation and recommendation:** use the *existing, never-disabled*
"Tailor CV" button as the primary recovery path, not a new command. When
that button is clicked on a snapshot whose `jd.status !== "captured"`, have
the Worker respond with a **Discord modal** (`type: 9`,
`INTERACTION_MODAL_CALLBACK`) containing one paragraph-style text input
asking the user to paste the job description. This binds to the job for
free — the button's existing lookup is already `job_snapshots WHERE
message_id = ?` (`worker/src/index.js:271-273`), so no new `job_ref` needs
to be surfaced anywhere. On modal submit (`payload.type === 5`, a type this
codebase does not yet branch on), the Worker builds a `kind: "tailor-jd"`
task carrying `{job_key, jd_text}` directly — no attachment, no CDN
round-trip, no Q2 exposure at all for this path.

`/tailor-jd` the slash command remains useful as the **fallback** for the
cases a modal can't handle: Discord's modal text-input components cap
content at 4000 characters (per Discord's documented component schema — not
verified live in this spike), while `jdIsComplete`
(`worker/src/index.js:42-49`) only requires ≥400 characters / ≥60 words —
most real JDs fit, but some (verbose postings, JDs with embedded benefits
copy) won't, and some users will only have a PDF or screenshot of the JD, not
pasteable text. For that fallback, `/tailor-jd` needs an explicit job
reference, since a slash command carries no `message_id` context the way a
button click does. The cleanest option is a required `job_ref` string
option matching the same 32-hex convention as the button's `custom_id`
(confirmed structurally compatible by the local probe below: an additive
string option alongside the existing attachment option doesn't disturb
`attachmentFrom`'s lookup). Surfacing that ref to the user requires adding
it to the embed footer in `job_card_payload`
(`job_archive.py:67-97`) — **naming `job_archive.py` as a required
production-file change this design introduces**, since it is currently
listed only as "out of scope: do not modify" for *this spike*, not for the
eventual build.

### Q6 — What is the failure mode for each new path?

Following the project's fail-soft rule (a failure costs the feature, never
the run, and always explains itself) and the existing
`except Exception ... status: "failed"` pattern in `process_task`
(`interactive_runner.py:135-142`):

| Path | Failure | User sees | State left behind |
|---|---|---|---|
| `/resume` download/extract | CDN fetch fails, or `pypdf` can't extract usable text | `"Interactive task failed safely: <reason>"` ephemeral message | Task row marked `failed`; no `resume.md` write attempted; no partial text stored |
| `/resume` preview | `prepare_resume_preview` rejects (`< 40 chars` — `resume_ingest.py:12-13`) | Same pattern, message names the extraction problem | Same — task `failed`, nothing parked |
| `/resume` confirm | Hash mismatch (stale confirm, or a second upload landed first) | `"This confirmation no longer matches the pending preview — run /resume again."` | `resume.md` **untouched** — this is the one failure mode this design treats as load-bearing; the whole hash scheme in Q4 exists to make this the only way a stale confirm can fail |
| `/resume` confirm write (Q3 option b) | D1 write fails | `"Interactive task failed safely: <reason>"`; task `failed` | `resume.md`/D1 confirmed-resume state unchanged from before the attempt — no partial write, since the design writes only after all validation passes |
| Modal-based `/tailor-jd` (button recovery) | Pasted text fails `jdIsComplete` | Ephemeral message naming which check failed (mirrors `worker/src/index.js:57-63`'s existing errors) | No task created — validated before dispatch, same as today's synchronous button-click validation |
| Slash-command `/tailor-jd` fallback | `job_ref` doesn't match any snapshot, or attachment download/extract fails | `"Interactive task failed safely: <reason>"` | Task `failed`; targeted job's snapshot JD status unchanged (still `"incomplete"` or absent) |
| Expiry (24h resume, 14min tailor) with no user action | No message — the interaction simply goes stale on Discord's side | Nothing (Discord's own "This interaction failed" if the user tries the dead token) | Task ages past `expires_at`; plan 004 prunes it 7 days later; parked preview text should be deleted at confirm/cancel, not left to the prune (Q4) |

---

## 3. The task shape

`run_task` needs to dispatch on `task["kind"]` instead of assuming every
task carries a `job_snapshot`. Sketch (not implementation):

```python
def run_task(task: dict) -> dict:
    kind = task.get("kind", "tailor")  # existing button-driven tasks carry no kind today
    if kind == "tailor":
        return _run_tailor(task)          # today's body, unchanged, snapshot-shaped
    if kind == "resume":
        return _run_resume_ingest(task)   # download, extract, preview, park, return buttons
    if kind == "resume-confirm":
        return _run_resume_confirm(task)  # re-verify hash, write via Q3's chosen path
    if kind == "tailor-jd":
        return _run_tailor_jd(task)       # bind job_key, accept jd_text OR download+extract attachment
    raise ValueError(f"Unknown task kind: {kind!r}")
```

`interactive_runner.py`'s contract changes from "validates one immutable JD
snapshot" to "validates and executes one of four task kinds, each fail-soft
and each independently testable." The download-and-extract-immediately rule
from Q2 becomes a shared helper (`_download_and_extract(url) -> str`) used by
both `_run_resume_ingest` and `_run_tailor_jd`'s attachment fallback — one
place that owns "fetch a Discord CDN URL exactly once, immediately,
never persist it."

`worker/src/index.js` needs, at minimum (named here so the build plan scopes
correctly, not implemented by this spike): a `custom_id` prefix dispatch
before `buttonTask` (Q4), a `components` field threaded through
`deliverResult` (Q4), a `type === 5` (modal submit) handler (Q5), and a
`job_ref` option reader alongside `attachmentFrom` for the `/tailor-jd`
fallback (Q5).

---

## 4. Test plan for the implementation

Following `test_interactive.py`'s existing model classes:

- **`TestResumeIngestion`** (extends the existing class, `test_interactive.py:156-168`):
  - accepts real extracted text end-to-end into `prepare_resume_preview`
    (already covered) — add: rejects when `_download_and_extract` raises
    (network/format failure), producing the fail-soft message from Q6.
  - confirm-hash matches: given a parked preview's `source_sha256`, a
    `resume-ok:<hash[:32]>` click with the matching hash succeeds; a
    mismatched hash is rejected without touching `resume.md` — this is the
    single most important test in the whole feature per the reviewer's
    stated concern.
  - stale-confirm-after-second-upload: two `/resume` tasks, confirm the
    first *after* the second has already written — must not revert the
    newer resume (the race from Q4).

- **`TestInteractiveRunner`** (extends `test_interactive.py:171-onward`):
  - `run_task` dispatches on `kind`; unknown `kind` raises cleanly (mirrors
    `test_runner_rejects_a_hash_valid_fragment_marked_as_captured`'s style
    of "one bad input, one specific error message").
  - existing `kind: "tailor"` (or missing-`kind`, for back-compat with any
    in-flight button tasks at deploy time) behavior is provably unchanged —
    same assertions as today's `test_runner_accepts_only_a_hash_verified_full_jd_snapshot`
    and `test_runner_returns_a_private_pdf_for_a_verified_snapshot`.
  - `kind: "tailor-jd"` with a `job_ref` binds to the correct snapshot and
    rejects a `job_ref` matching no snapshot, mirroring
    `test_runner_rejects_a_changed_jd`'s "one field wrong, specific error"
    style.

- **New `worker/test/interaction.test.js` cases**, mirroring the existing
  style (`assert.throws(..., /pattern/i)` for rejections,
  `assert.deepEqual` for accepted shapes):
  - `custom_id` prefix dispatch: `resume-ok:`/`resume-cancel:` route to the
    new handling *without* invoking `buttonTask`'s `tailor:`-only regex
    (regression test for the collision hazard in Q4).
  - `deliverResult` passes `components` through when present, in both the
    JSON and multipart branches.
  - a `job_ref` option alongside an attachment option doesn't break
    `attachmentFrom` (already probed locally below — promote the probe to a
    committed test).
  - modal submit (`type === 5`) builds a `tailor-jd` task bound to the
    clicked job's `message_id`, and rejects if the pasted text fails
    `jdIsComplete`.

---

## 5. Local probes (this spike's only executable work)

Both probes below ran **locally on 10 Aug 2026, with no live Discord,
GitHub, or other network call** — consistent with this spike's hard limit
against live network calls to any external service. Scripts are throwaway,
written outside the repository, and not committed.

**Probe 1 — PDF text-extraction round trip.** Generated a synthetic
single-column resume PDF with the installed `reportlab` (1832 bytes, 1
page), extracted its text with the installed `pypdf`
(`PdfReader(...).extract_text()`), and fed the result unmodified into the
real, unmodified `resume_ingest.prepare_resume_preview`. Result: extraction
preserved paragraph and section breaks cleanly, and `prepare_resume_preview`
accepted it and returned a valid Markdown preview with a matching
`source_sha256`. **Caveat, stated plainly:** this only validates the happy
path — a simple single-column layout that closely matches how `reportlab`
itself lays out text objects. It says nothing about real-world resumes with
two-column layouts, tables, or unusual fonts, which are known to garble
`pypdf` extraction ordering. That gap is exactly why the design keeps a
human-reviewable preview-before-confirm step rather than writing extracted
text straight to `resume.md` — the preview is the mitigation for this
probe's limitation, not a redundant step.

**Probe 2 — Attachment payload shape.** Built a synthetic
`APPLICATION_COMMAND` interaction payload matching Discord's documented
schema (`options` array with a `type: 11` ATTACHMENT option;
`resolved.attachments` keyed by the attachment's snowflake id) — documented
schema, not a live-captured payload. Ran the unexported `attachmentFrom`
function (copied verbatim from `worker/src/index.js:188-193`) against it:
parsed correctly. Then added a second, additive `type: 3` (STRING) `job_ref`
option ahead of the attachment option in the same `options` array and
re-ran: `attachmentFrom` was unaffected (its `.find(item => item.type ===
11)` lookup ignores option order and other option types), and the `job_ref`
value was independently readable via `options.find(item => item.name ===
"job_ref")`. This structurally supports the Q5 fallback design's `job_ref`
option without needing a live Discord round trip to confirm the parsing
logic.

---

## 6. Open questions that remain

- **Discord CDN attachment URL lifetime and auth requirements (Q2).** Not
  settled by this spike — settling it needs a live probe (upload a real
  attachment, capture the signed URL, test it after a delay) that this spike
  is barred from running. The mitigating design (download-and-extract
  immediately, never persist the URL) reduces this from "must know the exact
  number" to "must survive dispatch-to-runner-start latency," but the actual
  number is still unverified.
- **Whether `/resume` and `/tailor-jd` are registered with Discord at all
  (Q1).** No code answers this; only a live `GET
  /applications/{id}/commands` call (or the operator's own memory) can.
- **Which Q3 storage option the operator wants.** This spike recommends (b)
  but the choice carries a real trade-off (auditability vs. permission
  scope vs. workflow-coordination risk) that is the operator's to make, not
  this spike's.
- **Whether a 4000-character modal text input is enough for real JDs in
  practice.** The `jdIsComplete` floor (≥400 chars) is comfortably below
  4000, but no data exists in this repo on how long real captured JDs
  actually run — if verbose postings routinely exceed 4000 characters, the
  slash-command-with-attachment fallback stops being an edge case and
  becomes the common path, which would change the design's emphasis.
- **`pypdf` extraction fidelity on real-world resume layouts** (multi-column,
  tables, unusual fonts) is untested — Probe 1 only covers a `reportlab`-
  generated single-column PDF. The preview-before-confirm step exists
  specifically because this is unverified, but the build phase should still
  test against at least one real, messy resume PDF (with the operator's
  consent and without committing it) before shipping.
- **Discord message-length limits on the preview.** A full resume rendered
  as preview `content` may exceed Discord's 2000-character message cap;
  this design flags the need for truncation but does not specify the exact
  truncation strategy (e.g., first N sections plus a "preview shortened"
  note, vs. a follow-up ephemeral message with the rest).

## 7. What this design explicitly does not do

- It does not change the daily digest (`digest.py`, `digest.yml`) — the only
  overlap named is the `git push` race in Q3, which is a risk to flag, not a
  change this design makes on its own.
- It does not auto-apply a tailored resume to any job, and does not disable
  or bypass the confirm step under any circumstance, including retries.
- It does not put a resume PDF, or the raw extracted resume text, in the
  git repository at any point — only the final, user-confirmed Markdown
  ever reaches `resume.md` (or D1, under the recommended Q3 option), and
  original PDFs and parked preview text are discarded after use, not
  archived.
- It does not add a new runtime dependency, framework, queue, or browser
  automation step — every function named above (`requests.get`,
  `pypdf.PdfReader`, `reportlab`) already ships in `requirements.txt`.
- It does not register, modify, or delete any live Discord slash command —
  that remains the operator's action, guided by Q1's recommendation.
- It does not resolve the two explicit OPEN QUESTIONS above (CDN URL
  lifetime, command registration status) — both require a live probe or
  operator input this spike was not authorized to perform.
