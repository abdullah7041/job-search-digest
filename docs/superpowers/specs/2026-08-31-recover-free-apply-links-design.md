# Recover a free apply link before discarding a matching job

**Status:** Designed, not implemented

## Goal

Stop discarding jobs that match every filter purely because the aggregator
handed over a paywalled link. Prefer a free link the API already supplies;
fall back to reading the employer's own posting off the relister page; drop
only what neither can rescue.

## Evidence

Measured at `cbdb034` against the live run of 2026-08-31 (Actions run
`33381847921`) and the 179 snapshots in `jobs/`.

**Seven matching jobs were discarded in one day**, none for quality:

| Employer | Role | Blocked by |
|---|---|---|
| Qualcomm | Software Engineering, Riyadh | `sa.jooble.org` |
| exequt MENA | Data Pipeline Engineer: ETL & Big Data, Riyadh | `jobleads.com` |
| DataPlus | Senior Data Engineer | `sa.jooble.org` |
| Ernst & Young AE | Senior Data Engineering Consultant – AI & Cloud | `jobleads.com` |
| SENSYS | Industrial Big Data Engineer | `sa.jobrapido.com` |
| CreditNinja | Senior Data Platform Engineer | `jobleads.com` |
| Latitude | Senior ML Engineer – Production AI & MLOps | `jobleads.com` |

The published funnel hides this. `stats.json` records `matched: 15` for
2026-08-31, but that count is taken *after* the paid-relister cut. The real
shape is 22 matched → 7 dropped on link quality → 15 remaining, all previously
delivered. The digest reported "no new roles today" on a day the market
produced seven.

**Paid relisters are the dominant link source, not an edge case.** Hosts of the
174 JSearch-sourced archived deliveries, which were captured before
`PAID_RELISTER_HOSTS` began cutting them:

```
 65  sa.linkedin.com        41  www.jobleads.com
 16  sa.jooble.org          15  sa.jobrapido.com
 11  www.naukrigulf.com      5  bebee.com
  5  wuzzuf.net              5  sa.indeed.com
```

Checked with `on_paid_relister` itself rather than by eye: **100 of those 174
(57%) sit on hosts now blacklisted.** The majority of what the aggregator
supplies would be cut under today's rules.

**The JD is never the problem.** All 174 JSearch-sourced snapshots carry
captured JD text — JSearch returns `job_description` inline. These jobs arrive
fully scoreable and fully tailorable. Only the link is missing.

**The existing rescue cannot reach them.** `rescue_apply_link` exists to find an
employer's posting behind a relister, but `main()` drops paid-relister jobs at
step 2 while the rescue runs inside `enrich_descriptions` at step 5. The jobs
that most need it are discarded three steps before it runs.

**`apply_options` is read nowhere.** `grep -rn "apply_options\|is_direct\|job_publisher"`
over tracked Python returns nothing. `fetch_jsearch` takes
`j.get("job_apply_link") or j.get("job_google_link") or ""` — whichever
publisher JSearch ranked first, frequently a paid relister.

## The constraint that shapes the design

`record_apply_link` sets `apply_url` and deliberately never overwrites `url`:

> Setting `apply_url` rather than overwriting `url` is deliberate: `job_identity`
> keys an aggregator posting on its URL, so rewriting it would make every rescued
> job look new on the next run and redeliver it daily for as long as the rescue
> kept working.

`job_identity` keys `VOLATILE_ID_SOURCES` (which includes `jsearch`) on
`canonical_url(job["url"])`. Any design that improves a job's link by writing
`job["url"]` re-keys every affected posting and redelivers the entire back
catalogue. **This design writes only `apply_url`.**

## Options considered

**Deliver paywalled jobs with a warning flag.** Rejected: it moves the cost onto
the reader, who then clicks into a subscription wall. The link is recoverable
often enough to be worth recovering.

**Fetch the relister page for every paid job (page-rescue only).** Viable but
lower ceiling. It costs one GET per job, and `rescue_apply_link`'s own comment
records that Glassdoor, Jooble and learn4good answer 403 to datacenter IPs —
so it can never rescue the 16 jooble-hosted postings. Kept as the fallback
tier, not the primary.

**Read the free link out of the API response (chosen, tier 1).** Costs zero
extra requests, works on hosts that refuse datacenter fetches, and uses data
already paid for.

## Design

Five changes, each fail-soft.

### 1. Extract candidate links, tolerating an unknown response shape

New `jsearch_apply_options(raw_job) -> list[dict]` returning `[{"publisher", "url"}]`.

Resolution order: the `apply_options` key, then `apply_links`, then a generic
scan for any value that is a non-empty list of dicts whose first element has a
key containing `link` or `url`. Anything unrecognised yields `[]`.

The generic scan exists because the digest calls `/search-v2`, whose field names
are not fully documented in this repository. `fetch_jsearch`'s docstring lists
the fields "verified live" and `apply_options` is not among them — it is a
strong expectation from the JSearch API, **not a verified fact about this
endpoint**. The scan makes the code correct either way.

### 2. Choose a free link

`best_free_link(candidates) -> str` returns the first candidate whose URL is
`http`/`https` and whose host is not in `PAID_RELISTER_HOSTS`, else `""`.
Reuses `link_host` and `on_paid_relister` rather than re-implementing host
matching.

### 3. Populate `apply_url`, never `url`

In `fetch_jsearch`, set `job["apply_url"]` from `best_free_link`. `job["url"]`
is untouched, so `job_identity`, `canonical_url` and every existing `seen.json`
key keep their current values. No redelivery of the back catalogue.

### 4. Filter on the link actually handed over

In `main()`, change the paid-relister cut from `on_paid_relister(j.get("url"))`
to `on_paid_relister(apply_link(j))`. `apply_link` already returns
`job.get("apply_url") or job.get("url", "")`. Without this line, step 3 has no
effect — this is the change that makes the rescue count.

### 5. Give the page-rescue its chance before the drop

For jobs still on a paid relister after step 4, call the existing
`rescue_apply_link` and re-test, then drop what still fails. This moves an
existing call earlier for one subset of jobs; it does not change what
`rescue_apply_link` does.

Cost: one GET per still-paid job, bounded by the paid-relister count (7 on the
measured day). `record_apply_link`'s employer-token check still governs whether
a discovered link is believed, so a relister's "people also viewed" rail cannot
substitute another company's posting.

## Ordering in `main()`

Current, with the defect marked:

```
1. prefer_employer_postings(matched)
2. paid_only dropped              <- discards before any rescue
3. fresh = not in seen
4. verify_link
5. enrich_descriptions            <- rescue_apply_link lives here
```

Designed:

```
1. prefer_employer_postings(matched)
2. rescue_apply_link for jobs whose apply_link is on a paid relister
3. paid_only dropped, testing apply_link(job)
4. fresh = not in seen
5. verify_link
6. enrich_descriptions
```

Step 2 runs on `matched`, not on `fresh`, because the paid-relister drop
precedes the freshness check and a job dropped at step 3 never reaches step 4.
This is the one place the design spends requests on jobs that may already be
seen; the bound is the paid-relister count per run.

## Failure behavior

Every tier degrades to today's behavior:

- unrecognised response shape → `[]` → no `apply_url` → unchanged
- every candidate paywalled → `""` → no `apply_url` → unchanged
- page fetch 403/404/timeout → `rescue_apply_link` already logs and returns
- employer token disagrees → `record_apply_link` refuses the link and logs

Nothing here can raise into `main()`. A failure costs the user one job's link,
never the run.

## Observability

One summary line per run, alongside the existing counts:

```
apply links: N from the API, M rescued from the page, K still paywalled and dropped
```

Plus a single line naming the field the extractor found, the first time it finds
one:

```
JSearch apply options read from 'apply_options' (publishers: LinkedIn, Jobleads, ...)
```

That line is how the `apply_options` expectation gets confirmed or refuted from
a real run, without a separate probe workflow. If it never appears, tier 1 is
inert and the page-rescue is carrying the feature.

## Verification

Offline, no network, matching the existing `unittest` style:

- an `apply_options` array containing one free option → job survives, and
  `apply_link(job)` returns the free URL
- every option paywalled and no page rescue → job is still dropped
- the field absent entirely → extractor returns `[]`, page-rescue path runs
- a generic array under an unexpected key → extractor still finds it
- **`job_identity(job)` is byte-identical before and after `apply_url` is set** —
  the redelivery trap this design exists to avoid
- an end-to-end `main()` test where a paid-relister job with a free alternative
  reaches Discord, asserting `seen.json` gains its key

## Success criteria

- On the first run after deploy, the summary line reports a non-zero count in at
  least one of the first two categories.
- A job whose only JSearch link is `jobleads.com` but which offers a free
  alternative is delivered, with the free URL on the card.
- `seen.json` does not gain a burst of keys for postings delivered before this
  change — evidence that identity did not move.

## Out of scope

- **The `stats.json` rebase conflict** that failed run `33381847921`. One row per
  day means two writers on one day rewrite the same line and conflict
  irreconcilably on rebase; the workflow's retry loop has no conflict handling.
  Real, unrelated, and its own fix.
- **Cloudflare Workers Logs are disabled.** A dashboard toggle, not code, but it
  is why a Worker failure would currently be invisible.
- Widening `COMPANIES` or `AGGREGATOR_QUERIES`. Worth doing; independent of this.
- Replacing or removing either paid API. JSearch supplied 174 of 179 archived
  jobs; the 14 free ATS companies supplied 5.
