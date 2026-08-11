# Plan 004: Add retention to the Worker's D1 tables

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- worker/src/index.js worker/test/interaction.test.js worker/schema.sql`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first and confirm
> `git rev-parse --short HEAD` prints `6c7541e` or later.

## Status

- **Priority**: P1
- **Effort**: S–M
- **Risk**: MED
- **Depends on**: none (coordinate windows with plans/003 — see maintenance notes)
- **Category**: security
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

The Cloudflare Worker keeps five tables in D1. Exactly one of them is ever
cleaned up:

```javascript
await db.prepare("DELETE FROM internal_nonces WHERE expires_at <= datetime('now')").bind().run();
```

That is the only `DELETE FROM` statement in `worker/src/index.js` (line 143).
The other four tables — `tasks`, `deliveries`, `delivery_claims`,
`job_snapshots` — grow without bound for the lifetime of the deployment.

The `tasks` table is the one that matters most. Every Discord button click
inserts a row containing `interaction_token`: the user's Discord interaction
token, AES-GCM encrypted with `TASK_ENCRYPTION_KEY`. That token is useful for
15 minutes (`taskExpiryMilliseconds` returns 14 minutes for tailoring tasks,
24 hours for resume tasks) and then is dead weight. It is retained forever.
Encrypted-but-retained credentials are still a larger blast radius than
necessary: any future compromise of `TASK_ENCRYPTION_KEY` exposes the full
history rather than the last few minutes of it.

`job_snapshots` holds a full copy of every delivered job description keyed by
Discord message id — the same evidence the repository archives in `jobs/*.json`
— and also grows forever.

After this plan, the Worker prunes its own state once per day on the same code
path the daily digest already calls, with no new infrastructure and no cron
trigger.

## Current state

Files involved:

- `worker/src/index.js` — the entire Worker. Routes, D1 access, Discord calls.
- `worker/schema.sql` — the D1 schema, applied by hand at deployment.
- `worker/test/interaction.test.js` — `node:test` suite, 16 tests, hand-rolled
  fake D1 objects (no real database).

`worker/schema.sql` in full, as it exists today:

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  user_id TEXT NOT NULL,
  job_key TEXT,
  input_json TEXT NOT NULL,
  interaction_token TEXT NOT NULL,
  status TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  delivery_lease_until INTEGER,
  delivery_error TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
  job_key TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_claims (
  job_key TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('posting', 'retryable', 'posted')),
  message_id TEXT,
  claimed_at TEXT NOT NULL,
  lease_until INTEGER,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS internal_nonces (
  nonce TEXT PRIMARY KEY,
  expires_at TEXT NOT NULL
);

-- Snapshot rows are append-only and keyed by the exact Discord message whose
-- button resolves them. This prevents a later digest from changing an older
-- card's job description.
CREATE TABLE IF NOT EXISTS job_snapshots (
  message_id TEXT PRIMARY KEY,
  job_key TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  captured_at TEXT NOT NULL
);
```

Every table carries a timestamp column suitable for a TTL: `tasks.expires_at`,
`deliveries.posted_at`, `delivery_claims.claimed_at`,
`job_snapshots.captured_at`. All are ISO-8601 `TEXT`, written either by
`new Date().toISOString()` or by SQLite's `datetime('now')`, so
`datetime(column) < datetime('now', '-N days')` compares them correctly.

The existing prune function to mirror, `worker/src/index.js:142-148`:

```javascript
export async function claimInternalNonce(db, nonce) {
  await db.prepare("DELETE FROM internal_nonces WHERE expires_at <= datetime('now')").bind().run();
  const claim = await db.prepare(
    "INSERT OR IGNORE INTO internal_nonces (nonce, expires_at) VALUES (?, datetime('now', '+10 minutes'))"
  ).bind(nonce).run();
  return claim.meta?.changes === 1;
}
```

The route where the daily prune will run, `worker/src/index.js:299-318`:

```javascript
    if (url.pathname === "/internal/digest" && request.method === "POST") {
      const digest = JSON.parse(raw);
      const cards = digest.cards || [];
      if (digest.heartbeat) {
        cards.push({
          job_key: digest.heartbeat.key,
          payload: { content: digest.heartbeat.content, allowed_mentions: { parse: [] } },
        });
      }
      const summary = { posted: [], existing: [], in_progress: [], retryable: [], failed: [] };
      for (const card of cards) {
        try {
          const outcome = await postCard(env, card);
          summary[outcome.status].push(card.job_key);
        } catch (error) {
          summary.failed.push({ job_key: card.job_key, error: error.message });
        }
      }
      return json(summary);
    }
```

This route is hit exactly once per day, by `discord_delivery.post_interactive_cards`
from the scheduled digest run — including on quiet days, when it carries only a
heartbeat. That makes it a reliable daily tick with no cron trigger needed.

Repo conventions that apply here:

- Exported helpers are plain `export async function name(db, ...)` taking the
  database as the first argument, so tests can pass a fake. See
  `claimInternalNonce`, `claimTaskResult`, `releaseTaskResult`. Match this —
  do **not** reach for `env.DB` inside the helper.
- SQL is written inline as a template-free string, parameters bound with `?`.
  Never interpolate values into SQL.
- Tests build a fake `db` with `prepare(sql)` → `bind(...args)` → `run()`.
  There is no real D1 in the test suite and none should be added.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Worker tests | `cd worker && node --test`                                | pass, 0 fail (see below)      |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0, unchanged       |

**Record the Worker test count before you start.** Plan 006 also adds two tests
to this same suite and may have run first, so do not assume 16. Run
`cd worker && node --test` and note the `pass` figure; call it **M**. This plan
adds **+2**. Every count below is `M` or `M+2`.

There is no build, bundler, or typecheck step for the Worker. `worker/package.json`
is `{"private": true, "type": "module", "scripts": {"test": "node --test"}}`.

## Scope

**In scope** (the only files you should modify):

- `worker/src/index.js`
- `worker/test/interaction.test.js`
- `worker/schema.sql` — comments only, documenting the retention windows

**Out of scope** (do NOT touch, even though they look related):

- `worker/wrangler.jsonc` — do **not** add a `[triggers]` cron section. The
  daily digest POST is the tick; a cron trigger is new infrastructure for no
  gain and the project's stated principle is "what's deliberately simple".
- Any change to the table schemas themselves (columns, indexes, constraints).
  The deployed D1 database was created from this file by hand; a schema change
  would need a migration story that is out of scope here.
- `job_archive.py` / `jobs/*.json` — the repository-side archive is plan 003.
- Deleting rows from the live D1 database yourself. This plan changes code
  only. The first prune happens on the next scheduled digest run.

## Git workflow

- Branch: `advisor/004-d1-retention`
- Commit style: Conventional Commits.
  Suggested message: `feat: prune expired Worker task and delivery state`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the retention constants and the prune function

In `worker/src/index.js`, immediately **after** the existing
`claimInternalNonce` function (line 148), add:

```javascript
// Retention windows. Tasks hold an encrypted Discord interaction token that is
// useful for at most 24 hours, so they are kept only briefly past expiry for
// debugging. Snapshot and delivery rows are kept as long as seen.json keeps a
// job id (ARCHIVE_TTL_DAYS / STATE_TTL_DAYS = 120 days), because a button on a
// card older than that resolves to a job the pipeline has already forgotten.
const TASK_RETENTION_DAYS = 7;
const DELIVERY_RETENTION_DAYS = 120;

export async function pruneExpiredState(db) {
  const removed = {};
  const sweeps = [
    ["tasks", `DELETE FROM tasks WHERE datetime(expires_at) < datetime('now', '-${TASK_RETENTION_DAYS} days')`],
    ["job_snapshots", `DELETE FROM job_snapshots WHERE datetime(captured_at) < datetime('now', '-${DELIVERY_RETENTION_DAYS} days')`],
    ["deliveries", `DELETE FROM deliveries WHERE datetime(posted_at) < datetime('now', '-${DELIVERY_RETENTION_DAYS} days')`],
    ["delivery_claims", `DELETE FROM delivery_claims WHERE state = 'posted' AND datetime(claimed_at) < datetime('now', '-${DELIVERY_RETENTION_DAYS} days')`],
  ];
  for (const [table, sql] of sweeps) {
    try {
      const result = await db.prepare(sql).bind().run();
      removed[table] = result.meta?.changes ?? 0;
    } catch (error) {
      // A sweep failure must never cost the caller its digest delivery.
      removed[table] = `error: ${error.message}`;
    }
  }
  return removed;
}
```

The day counts are interpolated from module constants, never from request data
— that is why the template literal is acceptable here. Do not extend this
pattern to any value that could come from a request.

`delivery_claims` is swept only in the `posted` state on purpose: a row still in
`posting` or `retryable` is live coordination state, and deleting it would let
the same card be posted twice.

**Verify**: `cd worker && node --test` → still M pass, 0 fail (nothing calls
the new function yet).

### Step 2: Call the prune once per digest

In the `/internal/digest` handler, add one line as the first statement inside
the `if` block, before `const digest = JSON.parse(raw);`:

```javascript
    if (url.pathname === "/internal/digest" && request.method === "POST") {
      const pruned = await pruneExpiredState(env.DB);
      const digest = JSON.parse(raw);
```

Then include the result in the response so the digest log records it. Change
the summary construction line from:

```javascript
      const summary = { posted: [], existing: [], in_progress: [], retryable: [], failed: [] };
```

to:

```javascript
      const summary = { posted: [], existing: [], in_progress: [], retryable: [], failed: [], pruned };
```

The Python caller reads only the five list keys
(`digest.py:1113-1115` reads `posted`, `existing`, `in_progress`, `retryable`,
`failed`), so an extra key is safe. Do not rename or remove any existing key.

**Verify**: `cd worker && node --test` → M pass, 0 fail. Then
`python -m unittest test_digest.py test_interactive.py` → `OK`, unchanged
(the Python side must be unaffected by the extra response key).

### Step 3: Add tests

Add these two tests to `worker/test/interaction.test.js`. Import
`pruneExpiredState` by adding it to the existing import block at the top of the
file (keep the list alphabetical: it goes after `postCard`).

Model the fake database on the existing
`"prunes expired HMAC nonces while atomically claiming a new nonce"` test —
same `prepare(sql).bind(...).run()` shape.

```javascript
test("prunes expired tasks and aged delivery state on the daily digest tick", async () => {
  const executed = [];
  const db = {
    prepare(sql) {
      return {
        bind() {
          return {
            async run() {
              executed.push(sql);
              return { meta: { changes: 2 } };
            },
          };
        },
      };
    },
  };

  const removed = await pruneExpiredState(db);

  assert.equal(executed.length, 4);
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM tasks")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM job_snapshots")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM deliveries")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM delivery_claims")));
  // Live coordination state is never swept.
  const claims = executed.find((sql) => sql.startsWith("DELETE FROM delivery_claims"));
  assert.match(claims, /state = 'posted'/);
  assert.deepEqual(removed, { tasks: 2, job_snapshots: 2, deliveries: 2, delivery_claims: 2 });
});

test("a failing retention sweep never breaks the digest delivery path", async () => {
  const db = {
    prepare(sql) {
      return {
        bind() {
          return {
            async run() {
              if (sql.startsWith("DELETE FROM tasks")) throw new Error("d1 unavailable");
              return { meta: { changes: 0 } };
            },
          };
        },
      };
    },
  };

  const removed = await pruneExpiredState(db);

  assert.match(String(removed.tasks), /^error: /);
  assert.equal(removed.deliveries, 0);
});
```

**Verify**: `cd worker && node --test` → M+2 pass, 0 fail.

### Step 4: Document the windows in the schema

Add comments to `worker/schema.sql` above the `tasks` and `job_snapshots`
tables so the retention policy is discoverable from the schema. Change nothing
executable — comments only:

```sql
-- Rows are pruned by pruneExpiredState() in src/index.js, 7 days past
-- expires_at. interaction_token is an encrypted, short-lived Discord token;
-- it must not be retained beyond debugging usefulness.
CREATE TABLE IF NOT EXISTS tasks (
```

and

```sql
-- Snapshot rows are append-only and keyed by the exact Discord message whose
-- button resolves them. This prevents a later digest from changing an older
-- card's job description. Pruned by pruneExpiredState() after 120 days, which
-- matches STATE_TTL_DAYS in digest.py and ARCHIVE_TTL_DAYS in job_archive.py.
CREATE TABLE IF NOT EXISTS job_snapshots (
```

**Verify**: `grep -c "pruneExpiredState" worker/schema.sql` → `2`.

## Test plan

- New tests in `worker/test/interaction.test.js`:
  - `prunes expired tasks and aged delivery state on the daily digest tick` —
    all four sweeps run, and `delivery_claims` is scoped to `state = 'posted'`.
  - `a failing retention sweep never breaks the digest delivery path` — a D1
    error is captured, not thrown.
- Structural pattern: `prunes expired HMAC nonces while atomically claiming a
  new nonce`, the existing test at the bottom of the same file.
- Verification: `cd worker && node --test` → M+2 pass, 0 fail; and
  `python -m unittest test_digest.py test_interactive.py` → 45 pass.

## Done criteria

ALL must hold:

- [ ] `cd worker && node --test` exits 0 with M+2 pass, 0 fail
- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with its count unchanged
- [ ] `grep -c "DELETE FROM" worker/src/index.js` returns `5` (the original
      nonce sweep plus four new ones)
- [ ] Every value interpolated into SQL is a module constant, never anything
      request-derived. Check by eye:
      `grep -n "datetime('now'" worker/src/index.js` — every match must
      interpolate only `TASK_RETENTION_DAYS` or `DELIVERY_RETENTION_DAYS`, or
      no variable at all. Values that come from a request are bound with `?`.
- [ ] `grep -n "triggers" worker/wrangler.jsonc` returns no matches — no cron
      trigger was added
- [ ] `git status --porcelain` lists only `worker/src/index.js`,
      `worker/test/interaction.test.js`, `worker/schema.sql`
- [ ] No credential value appears anywhere in the diff
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `worker/src/index.js:142-148` or the `/internal/digest` handler does not match
  the excerpts in "Current state".
- `cd worker && node --test` fails before you make any change.
- You conclude a Cloudflare cron trigger is needed. It is not — report the
  reasoning instead of adding one.
- You find yourself wanting to change the `summary` object's existing five keys.
  `digest.py` reads them by name; changing them breaks delivery.
- A sweep would need to delete rows in a state other than the ones listed
  (`delivery_claims` outside `posted`, or `tasks` that are still `delivering`).
  That is a correctness question, not a cleanup one — report it.

## Maintenance notes

- **Retention alignment**: `DELIVERY_RETENTION_DAYS` here, `ARCHIVE_TTL_DAYS`
  in `job_archive.py` (plan 003), and `STATE_TTL_DAYS` in `digest.py` are all
  120 and must move together. If they diverge, a "Tailor CV" button can resolve
  a snapshot from one store while the other has forgotten it, producing a
  confusing partial failure.
- Deleting a `job_snapshots` row makes any button on that Discord card fail
  cleanly: `tailoringInputFromSnapshotRow` throws "The immutable job snapshot is
  unavailable." and the user sees "Could not start that task: …". That is the
  intended degradation for a 120-day-old card; if it ever needs to be friendlier,
  change the message in `tailoringInputFromSnapshotRow`, not the retention.
- The prune runs inside the request that delivers the digest, so it adds four
  D1 statements to that request's latency. If the digest POST ever approaches a
  timeout, move the sweep behind `ctx.waitUntil` — the same mechanism plan 006
  introduces for the interaction route.
- Reviewer should scrutinize: that `delivery_claims` is scoped to `posted`,
  that no sweep can throw, and that no request-derived value reaches SQL.
