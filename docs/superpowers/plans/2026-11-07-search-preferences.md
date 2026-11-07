# Search preferences and PR readiness

**Goal:** Complete PR53 preferences and qualify PR54's pypdf update together, ready for merge/deployment.
**Spec:** JOB_SEARCH_PREFERENCES.md, plus the user's decision to retain uncertain employment/live-status postings as separate Needs checking leads.
**Approach:** Extend existing source, rank, verification and card paths; no paid-budget increase or new dependency. Preserve immutable archives and candidate facts. PR54 stays dependency-only. Do not merge or deploy during preparation.

- [x] Read canonical preferences on discovery/ranking runs and include them as preferences, never candidate evidence. Update profile priorities and profile-refresh instructions.
- [x] Reproduce and fix full-time filtering, unknown-employment/live-status readiness, responsibilities-based discovery, source priority, requisition dedupe, verification timestamps, grouped digest and complete card metadata.
- [x] Add verified named-employer registry and bounded public feed checks. Reuse existing ATS sources and rotate unsupported named targets within current search request slots; explicitly report blocked/unavailable official listings.
- [x] Qualify PR54's exact hash-locked version in an isolated environment; run both PRs together without changing the system environment.
- [ ] Run full Python/Worker suites, Worker deployment dry-run, independent review and CI on both PR heads. Resolve important findings before marking PR53 ready.

**Review focus:** missing data stays unknown; contract full-time is excluded; blocked pages cannot establish active vacancy status; candidate facts stay resume-backed; title hints cannot hide a supported adjacent role; distinct locations/requisitions are not merged; all named-company coverage limitations remain visible.

**Execution:** native implementation with independent target-source and dependency qualification tasks as required by the dispatching-parallel-agents skill. Source task owns only new target_sources.py/test_target_sources.py/target_companies.json. Dependency qualification uses only temporary environments and its evidence report. Main checkout remains on codex/digest-cv-repairs.


## Verification and review record

- PR54 exact c70b1bf: PyPI artifacts/hashes verified, all eight locked packages installed and checked in an external temporary environment, full original suite655tests passed with one existing Windows symlink skip. No dependency defect was reproduced; PR54 remains dependency-only. Fresh GitHub CI rerun passed on November7.
- Combined PR53+PR54 dependencies: 706Python tests passed, one existing Windows symlink skip; all50Worker tests passed. Regression cases were observed failing before the relevant fixes. The Worker deployment dry-run passed using cached Wrangler4.140.0; no production deployment occurred.
- Independent review reported nine concrete issues: Saudi location substring collisions; unrelated/redirected structured vacancy records overwriting identity/employment; negated or malformed employment data; generic HTTP200pages misclassified as active; deduplication without comparing descriptions; preferred/negated qualifications labeled mandatory; native aggregator links outranking known official sites; partial Amazon responses counted as full coverage; numeric IDs shared across target portals.
- All nine findings were reproduced and fixed. The review's final turn ended at a service usage limit, so the author completed the remaining diff/spec inspection and checked the fixes with regression tests; there was no second independent approval.
- Existing fixtures now include explicit synthetic employment/identity evidence where their scenario promises a verified match. Unknown or unbound evidence is no longer promoted to verified status merely to preserve an old test expectation. Distinct requisitions, locations and substantive descriptions stay separate.
- Live read-only audits: Amazon74Saudi records; Sanabil12explicitSaudi records from15RSSitems. A Sanabil Senior Product Manager posting was independently retrieved at its canonical page and supplied a bound employer identity, FULL_TIME metadata, complete JD and open live status. This establishes that source/metadata path, not candidate fit or application success. No ranking model call, digest delivery, CV generation or application submission was triggered.
- Ruling: keep all existing Saudi company/ATS sources and provider budgets. Unsupported official sources remain visible coverage failures; existing-request fallback searches are bounded. Do not add guessed APIs or bypass blocked portals.
- Release instructions and limits are in docs/release-pr53-pr54.md. PR53's latest GitHub CI must pass before marking it ready; both PRs remain unmerged and the Worker remains undeployed during this preparation.
