# Job list — Saudi Arabia, senior tech

**Verified: 2026-07-20.** Every link below was fetched today, returned HTTP 200, and
resolves to an open posting still accepting applications. Roles pass the same filters
the digest uses: Saudi location, senior+, relevant title (engineer / software / AI-ML /
data / DevOps-SRE / platform / product / architect).

## Summary

- **10 live verified** listings (links confirmed open today).
- **0 replaced** — no dead/expired links found. This is the first materialized list
  (the folder previously kept no listings file; the digest posted to Discord and stored
  only seen-IDs). Nothing to archive.
- **0 could not confirm** — every matched role passed link verification.
- **Sources fetched:** HALA (15), Careem (28), Tamara (39), Lean (9), Foodics (36) —
  127 postings, 10 matched the Saudi/senior/relevant filter.
- **Source skipped:** JSearch aggregator (LinkedIn/Indeed/Glassdoor breadth) — `RAPIDAPI_KEY`
  not set in this environment, so it was not queried. Set the key to widen coverage.

Fit = deterministic keyword score (same `keyword_score()` the digest uses); higher = more
skill-keyword hits in the title.

---

## HALA — Greenhouse (fintech, Riyadh)

- **Actuarial Data Scientist** — Riyadh, Saudi Arabia · fit 55 · published 2026-07-14
  https://job-boards.greenhouse.io/hala/jobs/5299828008
- **Software Engineer** — Riyadh, Saudi Arabia · fit 40 · published 2026-06-24
  https://job-boards.greenhouse.io/hala/jobs/5217243008
- **Software Engineer II** — Riyadh, Saudi Arabia · fit 40 · published 2026-07-14
  https://job-boards.greenhouse.io/hala/jobs/5340971008
- **Site Reliability Engineer** — Riyadh, Saudi Arabia · fit 40 · published 2026-06-15
  https://job-boards.greenhouse.io/hala/jobs/5260477008
- **Engineering Manager** — Riyadh, Saudi Arabia · fit 40 · published 2025-10-13
  https://job-boards.greenhouse.io/hala/jobs/4926149008

## Tamara — Greenhouse (BNPL / fintech, Riyadh)

- **Engineering Manager - I** — Riyadh, Saudi Arabia · fit 40 · published 2026-04-07
  https://job-boards.eu.greenhouse.io/tamara/jobs/4798842101
- **Engineering Manager - II** — Riyadh, Saudi Arabia · fit 40 · published 2026-04-07
  https://job-boards.eu.greenhouse.io/tamara/jobs/4592339101
- **Product Engineer II - Flutter** — Riyadh, Saudi Arabia · fit 40 · published 2025-12-17
  https://job-boards.eu.greenhouse.io/tamara/jobs/4556956101
- **Product Engineer II - QA** — Riyadh · fit 40 · published 2026-01-19
  https://job-boards.eu.greenhouse.io/tamara/jobs/4760886101
- **Application Support Engineer** — Saudi Arabia · fit 40 · published 2025-11-13
  https://job-boards.eu.greenhouse.io/tamara/jobs/4683204101

---

## Notes

- **Careem, Lean, Foodics** were fetched live and returned open roles, but none of their
  current postings passed the Saudi + senior + relevant-title filter today (mostly UAE/Pakistan,
  or Saudi interns/sales). They stay in the pipeline and will surface a matching senior role
  when one lands.
- To widen this list, set `RAPIDAPI_KEY` (JSearch) or add companies to `COMPANIES` in
  [digest.py](digest.py) — see the `CANDIDATES` block at the bottom of that file (Tabby, Salla,
  Mozn, Unifonic, etc., ATS still unconfirmed).
- Regenerate this list by running the digest; link verification now runs automatically before
  delivery (see `verify_link` in [digest.py](digest.py)).
