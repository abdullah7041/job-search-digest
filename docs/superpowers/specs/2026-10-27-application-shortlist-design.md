# An application shortlist that keeps promising leads visible

Status: proposed design; awaiting approval before implementation.

## Intent and agreed direction

Help the candidate choose up to ten Saudi jobs to act on each day across the
twelve previously supplied role families. The user approved auditing real
failures, deliberately searching the strongest commercial/analytics tracks,
separating discovery from application readiness, and checking actual Discord
delivery. They also approved showing unresolved promising jobs as **Needs
checking**, with the missing details stated explicitly.

Ten remains the maximum daily review volume. Do not invent fit, raise scores,
repeat delivered jobs to fill space, or promise ten new suitable vacancies daily.

## Evidence behind this change

The October 27 production run, `40481072924`, fetched 1,646 postings, matched
117 local filters, and evaluated 18 candidates. It delivered zero cards:

- Four candidates failed the description-completeness gate.
- Seven had complete descriptions but zero positive resume strengths survived
  verification. The reason each strength disappeared is not recorded.
- Seven were rejected below the review floor.
- All 142 employer boards were attempted, but several Workable boards returned
  HTTP 429. No feeds were quota-paused. Attempted does not mean read successfully.

Echelon's Forward Deployed AI Engineer received a preliminary rank of 85 but
failed `responsibilities_not_evidenced`. The employer's public Ashby description
contains duties under **THE ROLE: DEPLOYING IMPACT** and qualifications under
**AI AND TECHNOLOGY FIRST**. This proves a heading-related false rejection; it
does not establish a final fit score or erase gaps such as unproven RAG work.

Sources inspected:

- https://github.com/abdullah7041/job-search-digest/actions/runs/40481072924
- https://jobs.ashbyhq.com/echeloncareers/a853776f-f277-4aff-a68d-da8190dd4463
- `digest.py`, `fit_check.py`, `job_archive.py`, `jd_fetch.py`, and the Discord Worker.

## Approaches

1. **Repair the existing selective alerts only.** Fix false rejections while
   continuing to hide every uncertain lead. This changes less, but leaves the
   user unable to review promising unresolved jobs.
2. **Extend the existing digest with explicit readiness states — chosen.** Keep
   verified recommendations, add a clearly separated Needs checking state, and
   fix the observed validation/search problems. This matches the user's accepted
   direction and reuses the current delivery and tailoring system.

No separate application dashboard, database service, paid provider, or framework
is proposed.

## Daily selection and visible states

Use one shortlist, up to ten cards total, in this order:

| Label | Admission | What the card claims |
| --- | --- | --- |
| Apply first | Existing verified strong-match rules, including final score >=80, complete JD, fresh posting and clean fit check | A verified strong match, with real evidence |
| Worth reviewing | Existing viable-review rules, including final score >=40, complete JD and verified positive resume evidence | A relevant application option with its stated gaps/questions |
| Needs checking | Preliminary ranking >=40, recognized target track and an unresolved description or fit-verification issue | A potentially relevant lead; application fit remains unverified |

All three states retain the Saudi-location, known-closed-link, paywall,
deduplication and employer-cap rules. Existing verified posting-date windows
remain: up to seven days for strong matches and thirty days for review/leads.
Unknown dates, explicit non-Saudi locations and confirmed role-scope or work-
authorization mismatches remain excluded. Scores below 40 are not padding.

Needs checking requires a successful preliminary model ranking against the
actual resume, an identified target track, a real posting URL and a specific
pending reason. Missing ranking, an unavailable resume or an arbitrary title
match alone is insufficient. A complete JD whose positive evidence fails
verification can enter this state without manufacturing replacement strengths.

Within each state, retain the existing user title priorities and ranking order.
Ready options consume slots before unresolved leads; no slots are reserved for
uncertain leads. Retain the maximum of three cards per employer across all states.

Needs checking cards show the employer, role, Saudi location, source link,
posting age, and concrete pending reasons. They say **Fit pending**, rather
than presenting a preliminary score as a verified recommendation. Unsupported
strengths are omitted. Show only verified gaps as facts; uncertain gaps remain
questions. Readers must be able to distinguish a missing description, missing
positive evidence, an unavailable check and an exhausted check budget.

## Description recovery and fit evidence

First audit the actual held candidates. Add bounded reason codes/counts for
strengths removed during verification: invalid source quote, quote outside
demonstrated experience/projects, unsupported factual claim, or unsupported
named term in the strength title. Keep the current resume evidence boundary.
Do not log the complete resume, credentials, secrets, or unbounded model responses.

Correct only reproduced false rejections. In particular, title capitalization
or an employer's alternative task heading must not erase otherwise supported
evidence. Fabricated tools, skills, metrics, snippets and requirements must
continue to fail. Do not substitute keyword overlap for verified strengths.

Use the exact employer posting for JD checks. Support the observed substantive
role headings and preserve the distinction between duties/qualifications and
marketing copy, missing text or a truncated teaser. A URL or provider's claim
that text is complete is not proof. Python snapshot/extractor checks, the
interactive runner and Worker must agree on the same regression cases.

The existing full-posting recovery path and employer-provenance checks remain.
If explicit posting-location evidence contradicts provider metadata, exclude
the job; do not infer a job's country from a localized LinkedIn subdomain.

## Search coverage within the existing allowance

Keep the two-request default and six-request absolute maximum for JSearch,
the current free-plan reserves, and existing source cadence/page sizes. No
additional metered calls are authorized by this design.

Use the existing profile's priorities to assign one daily JSearch slot to
families 1–4 (sales operations, commercial/retail analytics, data and BI), rotating
within that group. Rotate the second slot across families 5–12. Keep all supplied
aliases eligible. A lower configured request budget reduces work rather than
silently increasing it. The chosen day-to-family mapping is deterministic and
testable.

Focus the already scheduled ATS/LinkedIn page on the day's primary family using
the API's existing supported title filter. This changes which rows occupy the
same page, not how many pages are purchased. Indeed keeps its existing request
count and rotates the complementary families. Direct boards continue to supply
all target tracks daily. Do not infer improved yield until measured.

For free boards, honor HTTP 429/Retry-After within the existing network deadline.
If a useful wait will not fit, pause that host for the run and rotate its unread
boards to the next opportunity. Do not mark throttled boards successfully read.
Report attempts, successful reads and throttled/failed reads separately, with a
specific coverage message. Do not bypass access controls or remove warnings to
make coverage appear complete.

## Tailoring, delivery and persistence

Every card has a source/application link. Tailor CV is enabled only with a
validated full-description snapshot; a Needs checking card may have a complete
JD and unresolved fit, in which case tailoring remains available under the
existing evidence restrictions. With an incomplete JD, the card explains what
is missing and has no active Tailor CV action. Server-side validation must
enforce this for new readiness-aware cards even if a client fabricates a click.
Legacy cards without readiness metadata keep their existing link-recovery
behavior; recovery must still produce a validated full JD before tailoring.

Existing messages and immutable snapshot/hash checks remain valid. Add readiness
and pending reasons as backward-compatible snapshot fields; legacy cards retain
their existing meaning. Never turn a previously captured partial description
into full text merely by changing its label.

A Needs checking receipt must not create the existing 120-day ready-job seen
entry. Suppress unchanged repeat lead cards, but retry unresolved verification
on subsequent runs within current budgets. When a lead becomes ready, send one
new ready card with the updated evidence. Keep the earlier message's snapshot
immutable. Delivery idempotency must distinguish the unresolved lead from its
single ready upgrade, while preserving stable source identity and legacy keys.
An upgrade replaces a slot in that day's selection; it cannot duplicate the
same job twice in one shortlist.

Assistant's proposed call after the builder review: no intermediate repeat card
when a partial JD becomes complete but fit remains unresolved. The old card
continues to describe its archived snapshot and link to the source. Its disabled
button stays disabled; the single ready upgrade supplies the current snapshot
and tailoring action once the job qualifies. This avoids adding another stream
of pending-status notifications. A lead first published with a full JD can have
Tailor CV enabled as described above.

Record a delivery only after Discord accepts it. Preserve retryability after
failed or uncertain delivery. Archive displayed unresolved leads separately in
readiness metadata, so they do not count as verified review matches or strong
matches. Summary/stats counts distinguish Apply first, Worth reviewing and Needs
checking, plus attempted/accepted/failed deliveries. The same counts appear in
Discord and persisted state.

## Validation and release

Use a bounded replay set from the observed run: Echelon's task-heading case,
representative complete-JD/zero-strength cases, a real ordinary qualification
gap, a below-floor role, and a confirmed location/scope mismatch. Preserve source
provenance and distinguish the frozen replay from a live search.

Runnable checks must demonstrate:

- Reproduced complete-JD and valid-evidence false rejections are corrected;
  genuine fragments, invented experience and altered hashes still fail.
- Unresolved eligible leads produce Needs checking cards with explicit reasons;
  they do not inherit strong/verified-review claims or enable partial-JD tailoring.
- All three states share the ten-card and employer caps; existing ready cards
  are preferred; incompatible/closed/old/undated/below-floor jobs stay excluded.
- Unchanged leads do not spam; a lead can upgrade exactly once; failed deliveries
  remain retryable; legacy snapshots and ready-job deduplication still work.
- Focused queries spend no more than the existing request/page budgets, and
  throttling produces bounded work and accurate coverage counts.

After appropriate local and CI checks, review the change, deploy the Worker if
changed, merge the approved implementation, then verify a bounded live digest.
Use a free-sources-only manual run or the next scheduled run when the day's
metered allowance has already been spent; do not repeatedly purchase the same
search just to produce a positive result.

Release evidence must include the accepted Discord card IDs, their immutable
snapshots, matching per-state statistics and saved deduplication/upgrade state.
Verify button behavior with tests and delivery/snapshot contracts; do not claim
a human Tailor CV click was tested without observing one. A green workflow or
merged PR alone is insufficient. If no admissible leads exist, report the exact
reasons rather than claiming the ten-job outcome is solved.

## Scope and builder decisions

This is a focused extension of the existing Python digest and Discord Worker.
Keep the current platforms and dependencies. Do not add an application tracker,
outreach, automatic job applications, a new paid search provider, invented resume
experience, or a guarantee of ten suitable new jobs every day.

The three-state behavior and overall direction were approved by the user.
The exact daily search allocation, continued date limits, pending-score wording,
upgrade-once behavior, no intermediate repeats, legacy button recovery and
backward-compatible metadata strategy above are the
assistant's proposed calls for this design approval. The builder may choose
small helper names, reason-code spellings, test organization and storage details
that preserve these contracts; no speculative abstraction is required.
