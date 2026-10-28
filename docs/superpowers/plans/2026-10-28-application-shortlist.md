# Application Shortlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver useful verified jobs and explicitly unresolved leads, up to ten total, without weakening evidence or tailoring safeguards.

**Architecture:** Extend the existing Python pipeline and additive snapshot fields. Pending leads use a separate delivery/seen namespace, while ready jobs retain their legacy identity. Keep the current Discord Worker, immutable snapshots, quota planner and dependencies.

**Tech Stack:** Python unittest/stdlib, existing requests, JavaScript node:test, Cloudflare Worker/D1, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-27-application-shortlist-design.md` (approved by user).

## Global Constraints

- Maximum ten cards, maximum three per employer, ready states before Needs checking.
- Existing Saudi, closure, paywall, verified-date, evidence and score rules stay; preliminary scores >=40 and recognized track are required for pending leads.
- Existing two-request default/six-request maximum, page sizes, reserves and source cadence stay.
- Partial-JD new cards cannot tailor; legacy recovery remains; valid full-JD pending cards may tailor.
- Unchanged pending leads do not repeat; one ready upgrade is allowed; only accepted deliveries update seen state.
- No new dependencies, paid providers, automatic applications or invented evidence.

## Review Focus

- A pending receipt must not suppress a ready upgrade or corrupt an immutable old snapshot (Task 3).
- A forged button must not tailor a new partial JD, while legacy buttons still recover (Task 4).
- A verified skill quote must survive ordinary title wording without granting an absent named tool; invalid snippets remain rejected (Task 1).
- An HTTP 429 must not turn skipped boards into successful coverage or consume a long retry outside budget (Task 2).
- Model/check failures, unknown date/location, rank <40 and confirmed exclusions must never become padding (Tasks 3–4).

### Task 1: Reproduce description failures and expose evidence decisions

**Files:** `job_archive.py`, `fit_check.py`, `jd_fetch.py`, `digest.py`; tests in `test_digest.py`, `test_fit_check.py`.
**Interfaces:** Preserve `jd_grade(text) -> str` and `verify(raw, job, resume) -> dict`. Add bounded `strength_audit` counts/reason codes to fit results. Extract posting location as additive `posting_location` metadata for explicit conflict checks.

- [ ] Add failing cases for Echelon-shaped role headings, rejected-strength diagnostics, legitimate demonstrated quotes and foreign structured posting location.
- [ ] Run focused unittest classes; observe intended failures.
- [ ] Recognize substantive role headings, preserve teaser/marketing rejection; audit received/kept/discarded strengths, and explicitly request source-quoted positive strengths in the existing prompt. Correct only reproduced false rejections. Reject explicit foreign posting metadata rather than localized URL hosts.
- [ ] Run focused tests plus existing evidence/provenance tests; expect pass and absent tools/skills-list claims still rejected.
- [ ] Commit validation changes.

### Task 2: Focus existing searches and handle source throttling

**Files:** `digest.py`; tests in `test_digest.py`.
**Interfaces:** `focused_query_plan(queries, budget, day) -> list[str]` provides primary families 1–4 then rotating 5–12. `primary_title_filter(day=None) -> str` derives the existing API Boolean title filter. Free-source throttle state is per host, reset once in main, with board attempt/success/throttle counts.

- [ ] Add failing rotation/budget assertions and a 429 source replay proving later same-host requests are skipped and boards are not marked read.
- [ ] Run focused unittest cases; observe fail.
- [ ] Reuse existing query/allowance helpers; restrict scheduled fresh-feed page to primary family, complement Indeed, preserve request limits. Honor bounded Retry-After or pause the host; stamp only successful reads.
- [ ] Run query/quota/budget/board tests; expect pass with no extra metered calls.
- [ ] Commit coverage changes.

### Task 3: Select pending leads with upgrade-safe persistence

**Files:** `digest.py`, `job_archive.py`; tests in `test_strict_alerts.py`, `test_digest.py`.
**Interfaces:** `pending_reasons(job) -> list[str]`, `is_pending(job) -> bool`. Add `application_state` values `apply_first`, `worth_reviewing`, `needs_checking`; `pending_reasons` to new snapshots. Existing `job_key(job)` produces a stable `pending:`-prefixed key only for pending delivery. Existing `remember` writes `pending:`-prefixed source/description keys for pending receipts; ready keys are unchanged.

- [ ] Add failing end-to-end cases for a complete-JD missing-strength lead, partial JD, mixed-state caps/order, unknown rank/date, rank <40 and known exclusions; assert no fabricated strengths/final score on pending snapshots.
- [ ] Add a two-run regression: accepted pending lead is suppressed unchanged but can produce one ready upgrade; failed pending delivery remains retryable. Preserve legacy ready dedupe.
- [ ] Run cases; observe fail.
- [ ] Select pending only from unresolved candidates with verified preliminary ranking >=40, target track, real URL, confirmed date and Saudi location. Apply shared caps/order. Suppress repeat pending at selection (not discovery), preserving reassessment. Use pending receipt namespace so delivery and snapshot keys can upgrade independently.
- [ ] Run full strict-alert and delivery/state tests; expect pass, with ready-only metrics excluding pending.
- [ ] Commit selection/persistence changes.

### Task 4: Render truthful cards and enforce tailoring readiness

**Files:** `job_archive.py`, `digest.py`, `worker/src/index.js`; tests in `test_digest.py`, `test_strict_alerts.py`, `worker/test/interaction.test.js`.
**Interfaces:** `job_card_payload(snapshot)` displays Fit pending and concrete reasons for pending; no preliminary recommendation text. Worker snapshot gate reads additive application_state and JD grade, verifies integrity, and rejects new incomplete/teaser tailoring while preserving legacy recovery.

- [ ] Add failing payload and Worker tests for full/partial pending JDs, forged clicks, altered hashes and legacy snapshots.
- [ ] Run focused Python and `node --test`; observe intended failures.
- [ ] Mirror description checks in Worker. Add actual clickable posting links, disable partial-JD buttons, and gate server-side. Mirror labels in webhook fallback and summary. Record accepted per-state counts separately from attempted/failed and coverage metrics.
- [ ] Run Python card/strict-alert tests and Worker tests; expect pass.
- [ ] Commit card changes.

### Task 5: Verify and release

**Files:** approved design and plan, relevant README behavior notes only; no provider/state files edited manually.
**Interfaces:** Existing CI, Wrangler deploy --keep-vars, normal PR merge, manual/scheduled digest workflow and D1 read-only receipts.

- [ ] Run full Python suite, Worker suite and git diff --check; inspect output.
- [ ] Obtain one fresh whole-branch review against the approved design; fix important reproduced issues with failing-then-passing tests.
- [ ] Push branch, create/attach PR, await passing CI; deploy Worker preserving vars/secrets and merge under existing user authorization.
- [ ] Run one free-source live verification if today's metered requests were already spent; await state persistence. Compare accepted Discord card IDs/snapshots, readiness counts and seen/upgrade namespaces.
- [ ] Report actual yield, remaining gaps and exact tests; do not claim ten cards or human button validation without evidence.

## Execution decisions

Native execution follows the user's approval to implement; no additional approach/approval menu is needed. The managed worktree protects the primary checkout. Any justified deviation is recorded in this plan's ignored progress ledger before proceeding.
