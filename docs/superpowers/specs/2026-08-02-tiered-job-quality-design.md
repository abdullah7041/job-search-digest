# Tiered job-quality workflow — design

**Date:** 2026-08-02  
**Approved direction:** maximize recall without presenting every candidate as equally actionable. Deliver two fit tiers, tailor only the strongest tier, and calibrate through a short weekly review.

## Problem

The daily job-digest workflow is reliable at the job level, but it can lose viable roles before ranking. In the 2026-08-02 scheduled run, link verification dropped six of thirteen fresh roles after third-party boards returned HTTP 403, 429, or an inaccessible page. Those responses establish that GitHub's runner cannot fetch the page; they do not establish that the role is closed.

The existing one-list delivery also hides useful uncertainty: a role's fit, availability evidence, and ability to tailor from a real JD are collapsed into a single score and URL.

## Goals

1. Prefer recall over false negatives while retaining hard relevance filters.
2. Make fit confidence legible as **Strong matches** and **Worth reviewing**.
3. Never discard a role merely because a third-party host blocks or rate-limits GitHub Actions.
4. Produce tailored resumes only for strong matches, preserving the current bounded model cost.
5. Improve decisions over time with a two-minute weekly review of delivered roles.
6. Make the scheduled workflow prove its offline regression suite passed before delivery.

## Non-goals

- Adding a database, scraper, browser automation, or a new runtime dependency.
- Automatically applying to jobs or rewriting the master `resume.md`.
- Replacing the existing ATS readers or expanding sources as part of this change.
- Sending weak roles that fail the existing Saudi, seniority, or target-title hard filters.

## Architecture

The pipeline becomes:

```
fetch → normalize/dedupe → hard eligibility → link evidence → JD enrichment
     → fit classification → tiered delivery → strong-tier tailoring → state and metrics
```

### Hard eligibility

Keep `matches_filters` as the recall floor. It rejects only definite mismatches: non-Saudi location, explicit title/location conflict, excluded seniority, and unrelated titles. No model result or link-verification result may bypass these filters.

### Link evidence

Replace the boolean link result with an explicit status:

| Status | Meaning | Delivery behavior |
|---|---|---|
| `open` | The page resolves and shows no closed-posting evidence. | Deliver normally. |
| `closed` | A definite negative exists: no URL, non-recoverable not-found response, closed-page marker, or known ATS closed-page proof. | Drop. |
| `unknown` | The runner cannot determine status: 403, 429, timeout, connection error, or other ambiguous host response. | Keep and label the uncertainty. |

An `unknown` link does not affect the fit tier. The recipient can open a role from their own network, while the digest clearly says that the hosted check was inconclusive.

### JD enrichment and classification

Fetch a bounded JD excerpt for every surviving fresh role before ranking where the source permits it. Keep enrichment fail-soft: missing JD text is a visible confidence constraint, never a delivery failure.

The ranking output is normalized and validated before use. A strong match must satisfy all of:

- a score at or above the selected strong threshold;
- a score/reason returned for that exact job id;
- a non-empty JD excerpt, unless the source cannot expose one and the result is explicitly marked title-only.

All other hard-filtered, non-closed roles are **Worth reviewing**. A malformed or unavailable Gemini response falls back to deterministic scoring and places candidates conservatively in the review tier unless they meet the same strong-tier evidence rules.

The implementation plan will set the initial threshold to 80, make it a named configuration constant, and cover its boundary in tests. It is deliberately a calibration point, not a claim that 80 is permanently correct.

### Delivery

Discord has two sections, ordered by fit score within each:

1. **Strong matches** — score, concise reason, role URL, and a tailored-resume link when generated.
2. **Worth reviewing** — score, concise reason, role URL, plus compact flags such as `link check inconclusive` or `title-only assessment`.

Tailoring remains capped by `TAILOR_MAX` and runs only on strong matches. A tailoring failure keeps the role in the strong section without a resume link; it never prevents delivery or state persistence.

On days with no eligible roles, keep the current heartbeat. On days with review-tier roles but no strong matches, send the review section rather than claiming there were no new roles.

## State, metrics, and feedback

Continue using repository JSON state and the existing GitHub Actions commit-back pattern. Persist enough per-run counts in `stats.json` to distinguish:

- fresh candidates before link verification;
- definite link closures dropped;
- inconclusive links kept;
- strong and review-tier deliveries;
- tailored-resume successes and failures.

Do not make a feedback service part of the runtime. Add a versioned, human-editable weekly review file or template in the repository. Once a week, the user records accepted and rejected role URLs or IDs plus an optional reason. The next iteration can use that evidence to tune thresholds and title/profile rules; it will not silently train or auto-change selection rules.

## Error handling

- ATS and JD requests remain fail-soft per source/job.
- Discord delivery remains fail-loud; no roles are marked seen until all Discord chunks post successfully.
- Link verification distinguishes known closure from inaccessible evidence and logs aggregate status counts without exposing secrets.
- Invalid model JSON, missing records, non-numeric scores, and out-of-range scores are treated as unavailable ranking rather than trusted output.
- State files remain backward-compatible: absent new metrics default safely and corrupt state produces the existing fresh-start warning.

## Verification

Add or update offline tests for:

1. 403, 429, request error, and timeout link checks becoming `unknown` and remaining deliverable.
2. Known 404/closed-page/closed-ATS proofs becoming `closed` and being excluded.
3. Boundary classification at the strong threshold, missing JD behavior, and model-result validation.
4. Two-section Discord rendering, including review-tier uncertainty flags and a review-only day.
5. Strong-only tailoring and non-blocking tailoring failures.
6. New metrics and compatibility with existing state files.
7. A workflow step that runs `python test_digest.py` before `python digest.py`.

The regression gate is `python test_digest.py`; the existing tests must continue to pass. The workflow YAML should also be syntax-checked locally before it is published.

## Scope

In scope: `digest.py`, `test_digest.py`, `.github/workflows/digest.yml`, `README.md`, `stats.json` behavior, and a weekly-review template/documentation.

Out of scope: `resume.md`, `profile.json`, existing ATS source expansion, secret values, live Discord posting during local tests, and automated job applications.

## Rollout and success criteria

Ship behind the existing daily workflow with no new secret or dependency. For the first two weeks, review the new per-run counts and complete one weekly feedback note.

The change is successful when:

- 403/429/timeout verification outcomes no longer cause silent candidate loss;
- every delivered role has a visible tier and any evidence limitation;
- all strong-tier roles with successful tailoring receive a resume link, and review-tier roles do not consume tailoring calls;
- the scheduled workflow runs the offline suite before delivery;
- the weekly review produces concrete threshold/filter adjustments or confirms the initial calibration is sound.
