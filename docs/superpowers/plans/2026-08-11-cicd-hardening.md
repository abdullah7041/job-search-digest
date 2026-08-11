# CI/CD hardening implementation plan

## Global constraints

- Preserve the existing digest, Worker, Discord interaction, D1, and state-file contracts.
- Keep `seen.json`, `stats.json`, and `jobs/**` as repository-backed state; never force-push.
- Treat scheduled delivery as a daily window, not an exact Riyadh-time guarantee.
- Do not expose production secrets to pull-request CI or include data/log excerpts in alerts.
- Preserve unrelated user work and use only the `codex/cicd-hardening` branch.

## Task 1: Reproducible runtime inputs

Create a direct Python dependency manifest and a fully pinned hash-verified lock file. Update all workflow installation steps to install the lock with `--require-hashes`. Pin third-party Actions by full reviewed SHA and add weekly Dependabot updates for both pip and GitHub Actions.

## Task 2: Testable workflow guard

Using test-first development, add a standard-library helper and focused tests that:

- package only changed `seen.json`, `stats.json`, and `jobs/YYYY-MM-DD.json` files with hashes;
- reject absolute paths, traversal, symlinks, tampered hashes, and unexpected files;
- build concise Discord alert payloads without supplied sensitive detail;
- evaluate whether a completed successful digest and a current Riyadh-day stats row exist.

## Task 3: Least-privilege GitHub Actions

Add a secret-free advisory CI workflow for pull requests, code pushes to `main`, and manual dispatch. Harden the daily digest, interactive task, and watchdog workflows with explicit job permissions, pinned Actions, locked dependencies, appropriate concurrency, redacted Discord failure alerts, seven-day state artifacts, conflict-safe state persistence, and a 11:00 Riyadh freshness watchdog. Preserve the existing interactive `task_id` input and callback behavior.

## Task 4: Operational documentation and acceptance checks

Update the README with workflow triggers, timing limits, dependency refresh, alert behavior, artifact recovery, and rollback instructions. Run focused helper tests, existing Python and Worker suites, lock installation validation, workflow linting, and a static review of permissions/action pins/state paths.
