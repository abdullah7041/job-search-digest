# Release preparation: PR53 and PR54

PR54 updates only the pypdf requirement and its hash-locked artifacts. PR53 contains the digest/CV repairs and the tested implementation of JOB_SEARCH_PREFERENCES.md. The owner requested both ready for merge and deployment; this preparation does not merge or deploy them.

## Before integration

- Confirm both PR heads and required CI checks still match the reviewed versions. PR53 must be out of draft, and both must remain conflict-free.
- Read docs/dependency-validation-pr54.md for the isolated dependency audit. The combined Python suite is also run using the candidate pypdf6.21.4 environment; Worker tests and a deployment dry-run cover the Worker bundle.
- Merge PR54 before PR53 so main uses the qualified dependency for the complete application tests. Preserve any newer generated operational state on main; do not overwrite seen.json, boards.json, quota state, or job snapshots with an older branch copy.

## Deployment after merge approval

GitHub Actions uses the merged Python code and locked requirements on its next run. The Discord Worker has a separate deployment. From the merged worker directory use the project's qualified Wrangler version and the existing wrangler.jsonc:

```text
wrangler deploy --keep-vars
```

Keep the existing Worker name, D1 binding/database identifier, configured variables and secrets. The local dry-run compiles the bundle only; it does not prove deployed version or delivery. No database migration is required by these PRs.

After deployment, record the deployed Worker version and run the next authorized digest (or the next scheduled run) within the existing search allowance. Check its accepted card receipts, immutable snapshots and persisted readiness counts, plus source failures and deduplication state. A free-source manual replay can still send Discord messages and requires authorization. Do not call a successful workflow proof of a successful new tailored CV or delivery unless its actual result/receipt was observed.

## Known source limitations

- Laverne and Tesla blocked the read-only audit; other dynamic official search pages may not expose machine-readable vacancy listings. These conditions remain visible as coverage failures, with named-company discovery rotated within the existing paid request allowance.
- Sanabil RSS alone does not establish employment type or actual employing entity; unresolved roles remain Needs checking.
- Worker deployment and real application/tailoring outcomes are release verification steps, not results of local tests or the dry-run.
