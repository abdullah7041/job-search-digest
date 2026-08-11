# Reliable Resume Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely ingest and confirm `/resume` uploads in D1, synchronize the confirmed master back to Git during the daily digest, and make that Git persistence retry-safe.

**Architecture:** A small shared Python HMAC client serves both Actions entry points. The Worker owns live resume state in D1 and exposes only HMAC-authenticated internal operations; the interactive runner downloads a PDF once, stores only a pending Markdown preview, and confirms it with an atomic version check. The daily workflow synchronizes a confirmed record before a bounded-retry Git commit.

**Tech Stack:** Python 3.12 (`requests`, `pypdf`, `unittest`), GitHub Actions, Cloudflare Worker JavaScript, D1/SQLite, Discord Interactions, Node built-in test runner.

## Global Constraints

- `interactive.yml` keeps `contents: read`; a Discord-triggered workflow never gains repository write permission.
- `/tailor-jd` is absent from the registered command set until it has a real handler.
- Original PDF bytes and reusable Discord attachment URLs are never retained after extraction.
- Pending extracted Markdown is deleted on confirmation, cancellation, or 24-hour expiry.
- A stale confirmation must not overwrite a newer confirmed resume.
- All internal Worker requests are signed with `WORKER_CALLBACK_SECRET` and stay fail-soft with a user-visible reason.
- No new runtime dependency is introduced.
- Verification remains `python -m unittest test_digest.py test_interactive.py` and `cd worker && node --test`.

---

## File structure

- Create: `worker_client.py` — canonical HMAC request construction shared by the interactive runner and daily resume sync.
- Create: `resume_sync.py` — fetches the configured user's D1 record and conditionally writes `resume.md`.
- Create: `state_persistence.py` — stages state paths, commits once, and rebase-retries the push.
- Modify: `interactive_runner.py` — dispatches `tailor`, `resume`, and `resume-confirm` tasks; reads confirmed D1 state for tailoring.
- Modify: `worker/schema.sql` — adds confirmed and pending resume tables.
- Modify: `worker/src/index.js` — creates and confirms preview state, routes resume components, serves signed internal resume endpoints, and delivers components.
- Modify: `worker/commands.js` — declares only `/resume`.
- Modify: `worker/test/interaction.test.js` — Worker/D1/interaction contract coverage.
- Modify: `worker/test/commands.test.js` — command-set regression coverage.
- Modify: `test_interactive.py` — Python HMAC, resume task, and runner compatibility coverage.
- Modify: `test_digest.py` — Git persistence and daily sync coverage.
- Modify: `.github/workflows/digest.yml` — synchronizes D1 resume state, runs the expanded tests, then calls the persistence module.
- Modify: `README.md` — documents the source-of-truth, safe persistence behavior, `/resume` flow, and the deferred `/tailor-jd` command.

## Task 1: Extract the signed Worker client

**Files:**
- Create: `worker_client.py`
- Modify: `interactive_runner.py:126-146`
- Test: `test_interactive.py:195-302`

**Interfaces:**
- Produces: `signed_request(method: str, url: str, secret: str, body: bytes = b"") -> requests.Response`.
- Consumes: `WORKER_CALLBACK_SECRET` and the existing canonical HMAC format: `timestamp`, `nonce`, HTTP method, path, and raw body joined by newlines.
- Preserves: `interactive_runner.process_task(worker_url, secret, task_id) -> bool`.

- [ ] **Step 1: Write the failing HMAC-client test**

  Add a `TestWorkerClient` case to `test_interactive.py` with the existing local `ThreadingHTTPServer` pattern. Its handler must recompute the signature and assert that a `GET /internal/confirmed-resume` request signed by `signed_request` has an empty raw body and valid `X-Job-Digest-Signature`, timestamp, and nonce headers.

  ```python
  response = signed_request(
      "GET", f"http://127.0.0.1:{server.server_port}/internal/confirmed-resume", "callback-secret"
  )
  self.assertEqual(response.status_code, 200)
  self.assertTrue(received["signed"])
  ```

- [ ] **Step 2: Run the test to verify it fails**

  Run: `python -m unittest test_interactive.TestWorkerClient.test_signed_request_uses_the_existing_hmac_canonical_form`

  Expected: FAIL because `worker_client` and `signed_request` do not exist.

- [ ] **Step 3: Implement the shared client and refactor the runner**

  Create `worker_client.py` with the existing `_request` logic moved verbatim into the public function below. Use `urlparse(url).path` so the HMAC path exactly excludes host and query text, preserving the Worker verifier contract.

  ```python
  def signed_request(method: str, url: str, secret: str, body: bytes = b"") -> requests.Response:
      timestamp = str(int(time.time()))
      nonce = str(uuid.uuid4())
      path = urlparse(url).path
      canonical = f"{timestamp}\n{nonce}\n{method}\n{path}\n".encode("utf-8") + body
      signature = base64.b64encode(hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()).decode("ascii")
      return requests.request(method, url, data=body or None, headers={
          "X-Job-Digest-Signature": signature,
          "X-Job-Digest-Timestamp": timestamp,
          "X-Job-Digest-Nonce": nonce,
          "Content-Type": "application/json",
      }, timeout=30)
  ```

  Replace `_request` calls in `process_task` with `signed_request`, remove its duplicated imports and helper, and retain its exact result reporting behavior.

- [ ] **Step 4: Run focused regression tests**

  Run: `python -m unittest test_interactive.TestWorkerClient test_interactive.TestInteractiveRunner.test_process_task_uses_hmac_for_input_and_result_requests`

  Expected: PASS; both the new client test and the existing runner HMAC test pass.

- [ ] **Step 5: Commit the self-contained refactor**

  ```bash
  git add worker_client.py interactive_runner.py test_interactive.py
  git commit -m "refactor: share signed Worker request client"
  ```

## Task 2: Add D1 resume records and atomic preview confirmation

**Files:**
- Modify: `worker/schema.sql`
- Modify: `worker/src/index.js:21-29, 188-213, 302-315, 354-430`
- Test: `worker/test/interaction.test.js`

**Interfaces:**
- Produces: `resumeComponentAction(payload) -> { action: "confirm" | "cancel", previewId: string, digestPrefix: string, userId: string }`.
- Produces: `createResumePreview(db, userId, markdown, sourceSha256) -> { id: string, digestPrefix: string, baseVersion: number }`.
- Produces: `confirmResumePreview(db, userId, previewId, digestPrefix) -> { ok: boolean, reason?: string }`.
- Produces: `currentConfirmedResume(db, userId) -> { markdown: string, source_sha256: string, version: number } | null`.
- Consumes: Task-row `user_id` and `input_json`; only a signed internal task request may read that user's record.

- [ ] **Step 1: Write failing Worker helper tests**

  Add tests with the existing prepared-statement fake style in `worker/test/interaction.test.js` for all of these invariants:

  ```js
  assert.deepEqual(
    resumeComponentAction({ data: { custom_id: "resume-ok:preview-123:0123456789abcdef0123456789abcdef" }, member: { user: { id: "user-1" } } }),
    { action: "confirm", previewId: "preview-123", digestPrefix: "0123456789abcdef0123456789abcdef", userId: "user-1" },
  );
  assert.throws(() => resumeComponentAction({ data: { custom_id: "resume-ok:preview-123:bad" } }), /invalid resume/i);
  ```

  Add one confirmation test that records a D1 `batch` and expects an `INSERT ... ON CONFLICT ... WHERE` compare-and-set statement followed by preview deletion only when the version is current. Add the converse test where the first batch result has `meta.changes === 0`; it must return `{ ok: false, reason: "stale" }` and never delete the preview.

- [ ] **Step 2: Run the new Worker tests to verify they fail**

  Run: `cd worker && node --test test/interaction.test.js --test-name-pattern="resume"`

  Expected: FAIL because the exported helpers and schema-backed operations do not exist.

- [ ] **Step 3: Add schema and atomic helper implementation**

  Append these tables to `worker/schema.sql`:

  ```sql
  CREATE TABLE IF NOT EXISTS confirmed_resumes (
    user_id TEXT PRIMARY KEY,
    markdown TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS resume_previews (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    markdown TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status = 'pending')
  );
  ```

  In `worker/src/index.js`, add helpers beside the task utilities. A preview uses `crypto.randomUUID()`, expires in 24 hours, and computes `baseVersion` as `current.version` or `0`.

  Confirm with a single `env.DB.batch([...])`: first run one conditional `INSERT INTO confirmed_resumes ... SELECT ... FROM resume_previews WHERE id = ? AND user_id = ? AND status = 'pending' AND source_sha256 LIKE ?` with `ON CONFLICT(user_id) DO UPDATE` guarded by `confirmed_resumes.version = excluded.version - 1`; then delete that preview only when its `source_sha256` and resulting `version` match. Inspect the first result's `meta.changes`; return `stale` when it is zero. Cloudflare D1 documents `batch()` as a sequential transaction, so no observer can see only one of the successful write/delete pair. The cancellation path deletes the pending row directly rather than creating a terminal retained state.

  Make `pruneExpiredState` delete expired `resume_previews` and include its result in the returned summary. Never put Markdown into `tasks.input_json` after preview storage.

- [ ] **Step 4: Run the focused Worker suite**

  Run: `cd worker && node --test test/interaction.test.js`

  Expected: PASS, including existing nonce, task, delivery, and prune tests.

- [ ] **Step 5: Commit the D1 contract**

  ```bash
  git add worker/schema.sql worker/src/index.js worker/test/interaction.test.js
  git commit -m "feat: store confirmed resumes in D1"
  ```

## Task 3: Route resume preview and confirmation interactions

**Files:**
- Modify: `worker/src/index.js:245-350`
- Modify: `worker/commands.js`
- Modify: `worker/test/interaction.test.js`
- Modify: `worker/test/commands.test.js`

**Interfaces:**
- Consumes: runner result `{ status, content, components? }`.
- Produces: `POST /internal/tasks/{id}/resume-preview`, `GET /internal/tasks/{id}/resume`, and `POST /internal/tasks/{id}/resume-previews/{previewId}/confirm`.
- Produces: immediate private `resume-confirm` dispatch on a valid `resume-ok` component; valid cancellation deletes the preview without workflow dispatch.
- Preserves: opaque `tailor:<32 hex>` button behavior and `deliverResult` PDF delivery.

- [ ] **Step 1: Write failing interaction and command tests**

  Add tests that exercise an Ed25519-signed `type: 3` component interaction and assert that a `resume-ok:<uuid>:<32hex>` value is routed before `buttonTask`; it must return deferred ephemeral response data and dispatch a `resume-confirm` task. Add a cancel interaction test asserting no GitHub dispatch and one D1 deletion.

  Add two `deliverResult` tests: JSON callback payload contains `components`, and multipart `payload_json` contains the same components beside the PDF metadata. Add a command test asserting:

  ```js
  assert.deepEqual(COMMAND_NAMES, ["resume"]);
  assert.equal(COMMANDS[0].options[0].type, 11);
  ```

- [ ] **Step 2: Run targeted tests to verify they fail**

  Run: `cd worker && node --test test/interaction.test.js test/commands.test.js --test-name-pattern="resume|components|registered"`

  Expected: FAIL because all type-3 components currently flow through `buttonTask`, callbacks drop components, and `tailor-jd` is registered.

- [ ] **Step 3: Implement the routes and command change**

  Extend `deliverResult` so its common payload is:

  ```js
  const payload = { content: result.content, flags: 64, ...(result.components ? { components: result.components } : {}) };
  ```

  Serialize `payload` in both branches. In `handleInteraction`, inspect resume component IDs before `buttonTask`. For confirm, verify the private user, create a `resume-confirm` task carrying `{ preview_id, digest_prefix }`, acknowledge type `5`, and dispatch in the existing background path. For cancel, call the conditional delete helper and return an ephemeral `type: 4` response without dispatching.

  Implement signed internal routes after the existing task input/result routes:

  - `GET /internal/confirmed-resume` returns the configured `env.DISCORD_USER_ID` record or `404` with `{ "error": "no confirmed resume" }`.
  - `GET /internal/tasks/{id}/resume` loads the active task, verifies `taskAccessStatus(task) === 200`, then returns only that task user's confirmed record.
  - `PUT /internal/tasks/{id}/resume-preview` accepts `{ markdown, source_sha256 }`, verifies task kind is `resume`, saves a preview, replaces the task input with `{ "preview_id": id, "source_sha256": sha }`, and returns its ID/digest prefix.
  - `POST /internal/tasks/{taskId}/resume-previews/{previewId}/confirm` accepts `{ digest_prefix }`, verifies that the active task is `resume-confirm`, derives the private user from that task row, and returns `{ ok: true }` or `409` `{ "error": "stale" }`.

  Remove the `tailor-jd` object from `COMMANDS` and its explicit Worker dispatch branch. A stale manually registered command reaches the existing `Unsupported command.` default; the bidirectional command test therefore remains a real source-of-truth guard.

- [ ] **Step 4: Run Worker verification**

  Run: `cd worker && node --test`

  Expected: PASS with all pre-existing tests plus the new resume route, component, command, and prune coverage.

- [ ] **Step 5: Commit the interactive surface**

  ```bash
  git add worker/src/index.js worker/commands.js worker/test/interaction.test.js worker/test/commands.test.js
  git commit -m "feat: add resume preview confirmation flow"
  ```

## Task 4: Implement runner-side resume ingestion and D1-backed tailoring

**Files:**
- Modify: `interactive_runner.py:33-123, 147-180`
- Modify: `test_interactive.py:171-302`

**Interfaces:**
- Consumes: `task["kind"]` of `tailor`, `resume`, or `resume-confirm`.
- Produces: `_download_and_extract(attachment: dict) -> str` and `run_task(task: dict, worker_url: str = "", secret: str = "", task_id: str = "") -> dict`.
- Produces: preview result `{ "status": "completed", "content": str, "components": list }` and confirm result without PDF.
- Preserves: old task shape with missing `kind` is treated as `tailor`.

- [ ] **Step 1: Add failing runner tests**

  Add tests that monkeypatch `requests.get` and `pypdf.PdfReader` to prove a `resume` task downloads once, passes extracted text to `prepare_resume_preview`, and posts a preview result with `resume-ok:<preview-id>:<32hex>` and `resume-cancel:<preview-id>:<32hex>` buttons.

  Add a test where the preview-storage endpoint receives no attachment URL and only `{ "markdown", "source_sha256" }`. Add a confirm test where a `409` response produces `This confirmation no longer matches the pending preview — run /resume again.` and does not attempt a local `resume.md` write. Add one legacy tailor test where the task resume endpoint returns `404` and `_master_document` still reads a temporary local `resume.md`.

  ```python
  result = run_task({"kind": "resume", "input": {"attachment": attachment}}, worker_url, secret, "task-123")
  self.assertEqual(result["components"][0]["components"][0]["custom_id"], "resume-ok:preview-1:" + source_hash[:32])
  self.assertNotIn("url", posted_preview_body)
  ```

- [ ] **Step 2: Run targeted tests to verify they fail**

  Run: `python -m unittest test_interactive.TestResumeIngestion test_interactive.TestInteractiveRunner`

  Expected: FAIL because `run_task` accepts only a job snapshot and no preview/confirm endpoints exist.

- [ ] **Step 3: Implement the minimal task dispatcher**

  Keep the existing snapshot validation and PDF rendering in `_run_tailor`. Dispatch with:

  ```python
  kind = task.get("kind", "tailor")
  if kind == "tailor":
      return _run_tailor(task, worker_url, secret)
  if kind == "resume":
      return _run_resume_ingest(task, worker_url, secret)
  if kind == "resume-confirm":
      return _run_resume_confirm(task, worker_url, secret)
  raise ValueError(f"Unknown interactive task kind: {kind!r}")
  ```

  `process_task` calls `run_task(task, root, secret, task_id)`; direct legacy tests continue using its defaults. `_download_and_extract` validates an attachment URL and PDF content type/size metadata, calls `requests.get(url, timeout=30)`, rejects non-2xx or empty content, feeds bytes to `PdfReader(io.BytesIO(...))`, and rejects blank extracted text. It does not write the PDF to disk.

  `_run_resume_ingest` calls `prepare_resume_preview`, sends only Markdown and the full digest to the preview endpoint, truncates preview content at `MESSAGE_LIMIT` with `\n\n_Preview shortened; confirm only if the extracted text looks right._`, and builds two Discord button components. `_run_resume_confirm` posts the digest prefix to `/internal/tasks/{task_id}/resume-previews/{preview_id}/confirm` and maps `409` to the exact stale-preview message.

  Change `_master_document` to request `/internal/tasks/{id}/resume` for a tailor task before using local `resume.md`; use the local path only for a `404` migration fallback. Thread `worker_url`, `secret`, and task ID through `_tailored_pdf` and `_run_tailor`; do not change its evidence-validation or model-fallback behavior.

- [ ] **Step 4: Run the complete Python suite**

  Run: `python -m unittest test_digest.py test_interactive.py`

  Expected: PASS; existing tailored-PDF, JD integrity, and HMAC callback tests remain green alongside resume tests.

- [ ] **Step 5: Commit the runner work**

  ```bash
  git add interactive_runner.py test_interactive.py
  git commit -m "feat: ingest confirmed resumes through D1"
  ```

## Task 5: Synchronize D1 state and safely persist Git state

**Files:**
- Create: `resume_sync.py`
- Create: `state_persistence.py`
- Modify: `test_digest.py`
- Modify: `.github/workflows/digest.yml`

**Interfaces:**
- Produces: `sync_confirmed_resume(worker_url: str, secret: str, resume_path: str = "resume.md") -> bool`.
- Produces: `persist_state(branch: str, run: Callable = subprocess.run, sleep: Callable = time.sleep) -> bool`.
- Consumes: `INTERACTIVE_WORKER_URL`, `WORKER_CALLBACK_SECRET`, `GITHUB_REF_NAME`, and the four persisted paths `seen.json`, `stats.json`, `jobs/`, `resume.md`.
- Guarantees: `False` means no local change; a non-zero Git command raises and fails the workflow.

- [ ] **Step 1: Write failing daily-sync and persistence tests**

  In `test_digest.py`, add a local HTTP-server test for `sync_confirmed_resume`: a signed `200` JSON response with Markdown writes a temporary `resume.md` only when content differs; a signed `404` returns `False` and leaves the file byte-identical.

  Add a fake `run` test for `persist_state` that simulates a first `git push` failure then success. Assert the exact operation order includes one commit and this retry sequence:

  ```python
  self.assertEqual(calls[-4:], [
      ("git", "push", "origin", "HEAD:main"),
      ("git", "fetch", "origin", "main"),
      ("git", "rebase", "origin/main"),
      ("git", "push", "origin", "HEAD:main"),
  ])
  ```

  Add a rebase-failure test expecting `RuntimeError("state persistence rebase failed")`; it must never issue a second push.

- [ ] **Step 2: Run the focused tests to verify they fail**

  Run: `python -m unittest test_digest.TestResumeSync test_digest.TestStatePersistence`

  Expected: FAIL because neither module nor their public functions exist.

- [ ] **Step 3: Implement daily synchronization and bounded Git retry**

  `resume_sync.py` calls `signed_request("GET", f"{worker_url.rstrip('/')}/internal/confirmed-resume", secret)`. Return `False` on 404; call `raise_for_status()` otherwise. Require a non-empty string `markdown`, write it atomically through a temporary file in the target directory followed by `os.replace`, and return whether bytes changed. Its CLI reads the two existing Actions secrets and exits non-zero on an unexpected HTTP or malformed payload.

  `state_persistence.py` checks `git status --porcelain -- seen.json stats.json jobs/ resume.md`. When dirty, it configures the Actions identity, stages exactly those paths, commits `update state, snapshots, and resume [skip ci]`, then attempts `git push origin HEAD:<branch>` at most three times. After each failed push except the last, run `git fetch origin <branch>` and `git rebase origin/<branch>`; raise `RuntimeError("state persistence rebase failed")` immediately if that rebase fails. Use delays of 2 and 4 seconds between retry attempts. Its CLI defaults `branch` to `GITHUB_REF_NAME` or `main`.

  Update `digest.yml` to run the two new test modules in the existing offline regression step, then insert `Synchronize confirmed resume` after `Run digest` with only `INTERACTIVE_WORKER_URL` and `WORKER_CALLBACK_SECRET`, and replace the inline persistence block with `python state_persistence.py`.

- [ ] **Step 4: Run focused and full verification**

  Run: `python -m unittest test_digest.py test_interactive.py`

  Expected: PASS. Confirm `git diff --check` exits 0 and that the workflow includes no new `contents` permission or secret.

- [ ] **Step 5: Commit the daily integration**

  ```bash
  git add resume_sync.py state_persistence.py test_digest.py .github/workflows/digest.yml
  git commit -m "fix: make digest state persistence retry-safe"
  ```

## Task 6: Update operator documentation and verify the release candidate

**Files:**
- Modify: `README.md:125-152, 163-192`
- Test: `test_digest.py`
- Test: `test_interactive.py`
- Test: `worker/test/interaction.test.js`
- Test: `worker/test/commands.test.js`

**Interfaces:**
- Documents: D1 as live source after first confirmation, daily Git mirroring, `/resume` confirmation, `/tailor-jd` intentionally absent, and deploy-before-register ordering.
- Preserves: no automated deployment or command-registration invocation.

- [ ] **Step 1: Add a failing documentation assertion**

  Add a `TestDocumentation` case in `test_interactive.py` that reads `README.md` and requires these exact operator-facing phrases: `"D1 is the live source of truth"`, `"Deploy the Worker before registering commands"`, and `"/tailor-jd is not registered"`.

- [ ] **Step 2: Run the documentation test to verify it fails**

  Run: `python -m unittest test_interactive.TestDocumentation.test_readme_explains_resume_rollout`

  Expected: FAIL because the README still describes the pre-confirmation command state.

- [ ] **Step 3: Update the README with the release contract**

  Document the following, without claiming the feature is live until the operator deploys it:

  - `/resume` downloads a PDF once, shows a private normalized preview, and requires confirmation before D1 changes.
  - D1 is the live source after first confirmation; the next daily digest mirrors it to `resume.md` for Git history.
  - The daily state commit retries a failed push by rebasing; unrecoverable conflicts fail the workflow visibly.
  - `/tailor-jd` is intentionally not registered.
  - Operator order: merge/push, `cd worker && npx wrangler deploy`, then `DISCORD_BOT_TOKEN=... npm run register --prefix worker`.

- [ ] **Step 4: Run release-candidate verification**

  Run: `python -m unittest test_digest.py test_interactive.py`

  Expected: PASS.

  Run: `cd worker && node --test`

  Expected: PASS with zero failures.

  Run: `git diff --check`

  Expected: exit 0.

- [ ] **Step 5: Commit the documentation**

  ```bash
  git add README.md test_interactive.py
  git commit -m "docs: explain resume confirmation rollout"
  ```

## Release handoff

After all six task commits and the verification commands pass, push the branch, deploy the Worker, and only then bulk-register commands. Do not register commands during local development or CI. Perform one private `/resume` upload after deployment, inspect its preview, confirm it, verify a later tailor task reads the D1 record, and verify the next digest mirrors the confirmed Markdown into `resume.md`.
