# Stable job identity for delivery deduplication

**Status:** Implemented on `claude/resume-tailoring-job-fit-7e8k11`, awaiting user review

## Goal

Deliver each posting once. `seen.json` keyed on `job["id"]` never suppressed an
aggregator repeat, so the same role arrived on consecutive days.

## Evidence

Measured over the 11 archived days in `jobs/`, 140 snapshots:

- 57 distinct JSearch postings produced 122 deliveries. **65 were repeats.**
- One JAL "Senior Software Engineer (Frontend/UI-UX)" arrived on 9 separate
  days under **11 different `source_id` values**, all pointing at 1 URL.
- QuantumBlack "Software Engineer II" arrived on 7 days under 9 ids.

Stability by field, across every repeat delivery of the same posting:

| Field | Distinct values for 1 posting |
| --- | --- |
| `source_id` | 11 |
| `url` | 1 |
| `title` | 1 |
| `location` | 1 |

The id is the only field that moves, and it is the one `seen.json` used.

## Options considered

| Option | Verdict |
| --- | --- |
| Key on the JD SHA-256 | Rejected as the primary key. Descriptions are fetched after the dedupe filter, so using them there means enriching every matched posting on every run instead of only the new ones. |
| Key on normalised `(company, title, location)` | Rejected. Aggregator titles drift for the same posting: Pasqal's role appeared as "Client Solutions" on 2026-08-18 and "Client Delivery" on 2026-08-19. Some listings carry the company as the literal string "Company". |
| Key on the canonical URL | **Chosen** for sources with volatile ids. |
| Change `job_key()` in `job_archive.py` as well | Rejected. The Discord button's `custom_id` is `tailor:<sha256(job_key)[:32]>`, so changing it breaks every button already sitting in the channel. Identity for deduplication is a separate concern and gets a separate function. |

### Which sources change

Only `VOLATILE_ID_SOURCES`, currently `{"jsearch"}`. An ATS id is canonical and
stable, so those postings keep the bare id. That is also what every existing
entry in `seen.json` uses, so no previously delivered ATS role reappears as a
side effect of this change.

### URL canonicalisation

Lowercase scheme and host, drop the fragment, strip a trailing slash, sort the
remaining query parameters, and remove `TRACKING_PARAMS`.

Only 3 query parameters appear anywhere in the archive: `refId`, `trackingId`
and `jk`. The first 2 are LinkedIn per-response tracking and are removed;
leaving them in would recreate the churn this change exists to remove. `jk` is
Indeed's job key and is kept. Dropping the whole query string instead would
collapse every Indeed posting onto `https://sa.indeed.com/viewjob`, which is why
the rule is a denylist of tracking parameters and not an allowlist.

### Description backstop

`drop_repeat_descriptions()` runs after `enrich_descriptions()` and before
ranking, so a repeat never costs a model call. It suppresses a posting whose
exact description was already delivered, recorded under a `jd:<sha256>` key.

This exists because an aggregator can relist a job under a new URL of its own.
Jooble did it once in 11 days, minting `/jdp/-514809098470354554` and
`/jdp/6609179533026884564` for one posting.

It is safe as a second pass because none of the 58 distinct archived
descriptions is shared by 2 different companies. The hash matches
`make_snapshot()` exactly, so the state file and the archive agree on what
counts as the same description.

## Consequences

`main()` can now empty `fresh` after enrichment, which the previous structure
could not reach without dividing by zero when computing `avg_score`. The
heartbeat block moved into `post_heartbeat()` and both empty-day paths call it.
The second path records `relisted` in `stats.json`.

## Migration

Changing the key means a currently live posting looks new on the first run
after deploy, which would produce one final day of the repeats this removes.

`seen.json` is therefore backfilled from the committed archives in the same
commit: 57 URL identities and 58 description keys, dated to the day each
posting was captured. Verified afterwards that all 140 archived deliveries
resolve to a key already in the file, against 33 that would not have under the
old id-based key.

The 365 pre-existing `jsearch_*` id keys are left in place. They can no longer
match anything and expire on the normal `STATE_TTL_DAYS` schedule. Deleting
them would gain a smaller file and risk re-delivering a JSearch posting that
happens to arrive without a URL.

## Verification

1. 132 Python tests and 33 Worker tests, plus actionlint. 9 of the Python tests
   are new and cover identity directly: id churn, LinkedIn tracking parameters,
   Indeed's `jk`, ATS keys held constant, host case and trailing slashes, the
   no-URL fallback, the description backstop, and agreement with the archive
   hash.
2. Three end-to-end tests replay a second day against the first day's state:
   the same postings under regenerated ids and fresh tracking parameters
   produce a heartbeat; a relist under a new URL produces a heartbeat; a
   genuinely new posting is still delivered.
3. The migration replayed over all 140 archived deliveries: 0 would be
   re-delivered.

Not run: a live end-to-end digest. No `RAPIDAPI_KEY` or `OPENROUTER_API_KEY` in
the implementing session, so the first real run is the first time the new key
meets live JSearch output.

## Success criteria

The same posting stops appearing on consecutive days. `stats.json` `new` counts
drop toward the number of genuinely new roles, and `relisted` stays near zero
except when an aggregator churns URLs.
