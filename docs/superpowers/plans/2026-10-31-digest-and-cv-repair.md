# Digest and CV Repair Implementation Plan

> **For agentic workers:** Execute with superpowers:executing-plans, test first.

**Goal:** Repair the observed shortlist, source coverage, and CV failures.
**Architecture:** Reuse existing Python validators, source fetchers, deduplication, tailoring and Worker contracts. No dependencies or additional paid searches.
**Tech Stack:** Python unittest, requests, Node native tests, Discord Worker.
**Spec:** ../specs/2026-10-31-digest-and-cv-repair-design.md

## Global Constraints

- Preserve evidence checks, Saudi-only scope, dates, ten-card maximum and immutable archives.
- Keep current search allowance and dependencies; honor rate limits.
- Reuse the current checkout on branch codex/digest-cv-repairs.

## Review Focus

- Marketing and duty-only snippets must not become verified qualifications.
- Duplicate normalization must preserve distinct locations and substantive requirements.
- New source identifiers must not resend previously delivered Workable jobs.
- Partial/malformed source responses must remain failures in coverage.
- Model formatting repair must not bypass summary or bullet evidence validation.

### Task 1: CV headline and fallback

Files: resume_tailor.py, interactive_runner.py, test_interactive.py.
Interface: tailor(document, snapshot, api_key, sources, master_text) retains its return tuple.

- [x] Add a failing test for a valid plan with an invalid headline, and a fallback message test.
- [x] Run the targeted unittest cases; expect headline rejection and false tailored copy.
- [x] Construct the required headline before validation; label master fallback accurately.
- [x] Run test_interactive.py; expect all passing, including unsupported summary/bullet rejection.

### Task 2: Substantive JDs and duplicate listings

Files: job_archive.py, digest.py, worker/src/index.js, test_digest.py, test_strict_alerts.py, worker/test/interaction.test.js, one shared frozen fixture under eval/.
Interfaces: jd_completeness_failures(str), jd_grade(str), drop_repeat_descriptions(jobs, seen), and Worker tailoringInputFromSnapshotRow retain their signatures.

- [x] Freeze the October 30 duty text; write failures for heading-free substantive duties, same-run metadata duplicates, and Worker acceptance with valid hash.
- [x] Run targeted Python and Node cases; expect heading and duplicate failures.
- [x] Add a bounded line-start duty-list recognizer; require genuine qualification evidence for heading-free lists; recognize structured short descriptions without treating marketing as full text.
- [x] Deduplicate normalized duty bodies by employer/title/location, retaining old fingerprints for existing seen entries and preferring the richer copy.
- [x] Run regression cases for different requirements/locations, legacy seen entries, no-qualification snippets, marketing, and verified-review delivery.

### Task 3: Employer source coverage

Files: digest.py, jd_fetch.py if needed, test_digest.py.
Interfaces: fetch_company(c), get_description(job), and board_read_queue(boards) retain their signatures.

- [x] Add failing fixtures for HALA's public jobs/detail payload and Workable's one-response board payload; include source errors and legacy seen URLs.
- [x] Run targeted cases; expect unsupported HALA source and absent widget mapping.
- [x] Replace HALA's curated source, suppress its obsolete discovered board, and recover public descriptions through its public endpoint. Treat missing descriptions as missing.
- [x] Replace paginated Workable reads with the existing bounded widget GET; preserve canonical URLs, dates and rate-limit behavior.
- [x] Run source, Retry-After, malformed response, coverage, and legacy seen regression cases.

### Task 4: Final verification and review

- [x] Run `python -X utf8 -m unittest test_digest.py test_interactive.py test_workflow_guard.py test_fit_check.py test_eval.py test_strict_alerts.py` and `npm test` in worker; expect zero failures.
- [x] Run `git diff --check`; expect no whitespace errors.
- [x] Review the whole diff against the approved spec, address substantive findings, record results here.
- [x] Report local fixes and verification; distinguish them from production deployment and new live tailoring.

## Execution record

- Ruling: use a task branch in the existing checkout, following desktop workspace preference; no new worktree needed.
- Pre-flight: Python/Worker JD validation must share identical frozen examples; source mapping and deduplication must preserve seen behavior.

- Task 1 complete: headline and fallback regressions failed before the repair; all 127 interactive tests then passed.
- Task 2 complete: frozen October 30 replay reproduced heading rejection and duplicate cards before repair. The copy with explicit experience now reaches fit verification; the duty-only copy remains incomplete. Same-run duplicates retain the richer original text. Conflicting requirements and locations remain distinct.
- Task 3 complete: HALA source mapping and Workable single-response tests failed before repair, then passed. Existing Retry-After and source-coverage tests use the new read endpoint without weakening their assertions. Legacy delivered URLs are checked before network/model work.
- Live read-only checks: the repaired fetchers read HALA 19 postings and Foodics 29 postings in two board GETs; all HALA rows supplied publication dates; the blank HALA software-developer description stayed missing. No digest delivery or model tailoring was triggered.
- Workable parity audit compared full paginated v3 shortcode sets with widget shortcode sets for Foodics and Salla; both matched at the audit. This is not a guarantee for every account or future provider change. Discovery retains v3 to distinguish invented account tokens; confirmed-board reads use the widget.
- Final independent review found three important HALA edge cases: malformed details could abort the digest, invalid location types could escape source validation, and a closure flag could be ignored after enrichment. All three were reproduced by failing tests, fixed, and passed in the final suite. No unresolved review findings remain.
- Final verification: `python -X utf8 -m unittest test_digest.py test_interactive.py test_workflow_guard.py test_fit_check.py test_eval.py test_strict_alerts.py` ran 672 tests, OK with one skip. `npm test` in worker passed all 50 tests. `git diff --check` passed.
- Windows test environment: the initial redirected baseline used cp1252 and encountered eight UnicodeEncodeErrors while logging. Running Python with `-X utf8` resolved the environment issue without changing product logging.
- Release boundary: changes are local to codex/digest-cv-repairs. No merge, push, deployment, production delivery, or new model-generated CV outcome is claimed. Existing archived snapshots and operational state files are unchanged.
