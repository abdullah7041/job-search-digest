# Digest and CV failure repair — 2026-10-31

## Intent and approved approach
Repair the October 30 shortlist and the Gathern CV fallback within existing search allowances. The user selected targeted fixes over expanded sources or spending. Keep the current Python/Discord system and dependencies.

## Evidence and root causes
- Production run 41042340241 fetched 1,673 jobs, matched 121, and ranked five after deduplication, seen, freshness, and link checks. Two were below 40; three failed JD completeness; two pending cards were accepted.
- The two KTek Power BI cards share the same duty list; one adds four metadata lines. The completeness check requires a known heading despite explicit duties, and description deduplication compares raw text, missing this relist.
- HALA's old Greenhouse board returns 404. Its official website links careers.hala.com, whose public frontend reads gateway.hala.com/mawhiba/v1/public/careers/jobs. That endpoint returned current postings successfully.
- Workable v3 reads ten jobs per page. A 429 pauses the host for the run, deferring eight more boards on October 30. The widget endpoint already used for JD recovery returns board jobs and descriptions in one GET (Foodics: 29 jobs verified). Rate limits remain possible.
- CV tailoring generates a headline despite already knowing its exact required value; exact validation rejects the draft after retries. The fallback message still starts by claiming the document is tailored.

## Design
1. Recognize bounded substantive duty lists in the shared Python JD checks and the corresponding Worker check. Keep minimum substance and qualification checks; do not accept marketing prose or declare absent qualifications known. Preserve original JD text and hashes.
2. Deduplicate the observed metadata-prefixed copy using normalized duty content, scoped to employer, title, and location. Reuse current deduplication paths and retain legacy seen protection. Do not collapse distinct substantive vacancies.
3. Replace HALA's retired board with its verified public careers feed and fetch the description only for relevant candidates. Preserve source provenance, real publication dates, Saudi filtering, and explicit failure reporting. Retire the obsolete HALA board queue entry so it cannot run alongside its replacement.
4. Read Workable boards through the widget endpoint already used here, preserving canonical employer posting URLs, publication dates, description content, and existing seen/receipt behavior. Keep bounded responses, a bounded Retry-After retry, and honest host-pause coverage. Do not bypass 429s or count failed/partial responses as successful reads.
5. Construct the exact allowed headline from the master title and captured target title before validation; validate all other model wording normally. Label an untouched fallback as a master resume from its first sentence.

## Alternatives and boundaries
The user selected targeted repairs. Lowering fit thresholds would not fix heading, duplication, stale source, or headline-contract failures; extra paid searches change the allowance. Neither is included. Keep ten as a maximum, not a promised count; do not pad with weak roles or invent fit evidence. Existing archived messages remain immutable. No deployment, merge, metered replay, or outgoing message is implied by local tests.

## Verification
Use frozen October 30 text and a reproducible invalid headline to show failures before fixes. Check substantive duties, metadata deduplication and distinct-location preservation, HALA feed/detail mapping and failures, Workable mapping/rate limits/legacy seen behavior, and truthful fallback copy. Run relevant Python and Worker suites and diff checks. A successful replay establishes repaired behavior, not ten live suitable vacancies or a successful new model-generated CV.
