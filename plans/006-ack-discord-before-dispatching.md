# Plan 006: Acknowledge the Discord interaction before dispatching the workflow

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6c7541e..HEAD -- worker/src/index.js worker/test/interaction.test.js`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Repository state warning**: this plan was written against `origin/main`
> (commit `6c7541e`). Run `git pull --ff-only` first and confirm
> `git rev-parse --short HEAD` prints `6c7541e` or later.

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: MED
- **Depends on**: none (independent of plan 004, though both touch `worker/src/index.js`)
- **Category**: bug
- **Planned at**: commit `6c7541e`, 2026-08-09

## Why this matters

Discord requires a response to an interaction within **3 seconds**. If the
Worker takes longer, Discord shows the user "The application did not respond"
and the interaction token becomes unusable.

Today the Worker does all of this before it replies:

1. `createTask` — one D1 `INSERT`, plus an AES-GCM encryption of the token.
2. `dispatchWorkflow` — an HTTPS round-trip to `api.github.com` to trigger the
   workflow, plus a second D1 `UPDATE` on success.

The GitHub API call is the risk. It is an external network request to a
third-party service from a Cloudflare edge location, and it sits on the
critical path of a 3-second budget.

The failure mode is worse than a slow button. If the budget is exceeded, the
user sees an error — but the task row was already written and the workflow was
already dispatched. `interactive_runner.py` then runs, does the work, and POSTs
its result back; the Worker tries to `PATCH` the original interaction message
with a token Discord has already discarded, gets a non-2xx, marks the task
`delivery_retryable`, and returns 502 so the workflow exits non-zero. The user
gets an error message, a red workflow run, and no resume — for work that
actually completed.

Cloudflare's `ctx.waitUntil` exists for exactly this: return the response now,
finish the background work after. Moving only the GitHub dispatch behind it
removes the external round-trip from the budget while keeping the D1 insert
(fast, local, and the thing whose failure the user genuinely needs to hear
about) in the acknowledged path.

## Current state

Files involved:

- `worker/src/index.js` — the entire Worker.
- `worker/test/interaction.test.js` — `node:test` suite, 16 tests.

`worker/src/index.js:178-186`, the dispatch:

```javascript
async function dispatchWorkflow(env, taskId) {
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/interactive.yml/dispatches`, {
    method: "POST",
    headers: { "authorization": `Bearer ${env.GITHUB_TOKEN}`, "accept": "application/vnd.github+json", "content-type": "application/json", "user-agent": "job-search-digest-worker" },
    body: JSON.stringify({ ref: "main", inputs: { task_id: taskId } }),
  });
  if (!response.ok) throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  await env.DB.prepare("UPDATE tasks SET status = 'dispatched' WHERE id = ? AND status = 'received'").bind(taskId).run();
}
```

`worker/src/index.js:260-289`, the interaction handler:

```javascript
async function handleInteraction(request, env) {
  const raw = await request.text();
  if (!(await discordSignatureIsValid(request, raw, env))) return new Response("invalid signature", { status: 401 });
  const payload = JSON.parse(raw);
  if (payload.type === 1) return json({ type: 1 });
  const allowed = authorizeInteraction(payload, env);
  if (!allowed.ok) return json({ type: 4, data: { content: allowed.message, flags: 64 } });
  try {
    let task;
    if (payload.type === 3) {
      task = buttonTask(payload);
      const snapshot = await env.DB.prepare(
        "SELECT job_key, snapshot_json FROM job_snapshots WHERE message_id = ?"
      ).bind(payload.message?.id || "").first();
      task.input = await tailoringInputFromSnapshotRow(snapshot, snapshot?.job_key);
      if (await buttonReferenceForJobKey(task.jobKey || snapshot.job_key) !== task.jobRef) {
        throw new Error("The tailoring button does not match this job snapshot.");
      }
      task.jobKey = snapshot.job_key;
    }
    else if (payload.data?.name === "resume") task = { kind: "resume", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
    else if (payload.data?.name === "tailor-jd") task = { kind: "tailor-jd", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
    else return json({ type: 4, data: { content: "Unsupported command.", flags: 64 } });
    const taskId = await createTask(env, task, payload.token);
    await dispatchWorkflow(env, taskId);
    return json({ type: 5, data: { content: task.kind === "resume" ? "Reading your CV privately…" : "Preparing your tailored CV privately…", flags: 64 } });
  } catch (error) {
    return json({ type: 4, data: { content: `Could not start that task: ${error.message}`, flags: 64 } });
  }
}
```

`worker/src/index.js:291-294`, the entry point — note it takes only two
parameters today:

```javascript
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/interactions" && request.method === "POST") return handleInteraction(request, env);
```

The delivery helper you will reuse for the failure path,
`worker/src/index.js:245-258`:

```javascript
async function deliverResult(env, task, result) {
  const token = await decrypt(task.interaction_token, env.TASK_ENCRYPTION_KEY);
  const endpoint = `https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${token}/messages/@original`;
  ...
```

**Critical test constraint**: every existing test calls the entry point with
two arguments, e.g. `worker/test/interaction.test.js:135`:

```javascript
  const response = await worker.fetch(request, { DISCORD_PUBLIC_KEY_BASE64: publicKey });
```

There is no third `ctx` argument anywhere in the suite. Your implementation
must therefore work when `ctx` is `undefined`, or 5 existing tests break.

Repo conventions that apply here:

- Errors that the user should see are thrown inside the `try` in
  `handleInteraction` and surface as
  `Could not start that task: ${error.message}`. Messages are written as full
  sentences for a human reader ("The immutable job snapshot is unavailable.").
- Helpers that need testing are `export`ed and take their dependencies as
  arguments. Non-exported helpers (`createTask`, `dispatchWorkflow`) are tested
  only through `worker.fetch`.
- No dependencies, no build step. Plain ES modules, `node --test`.

## Commands you will need

| Purpose      | Command                                                   | Expected on success           |
|--------------|-----------------------------------------------------------|-------------------------------|
| Worker tests | `cd worker && node --test`                                | pass, 0 fail (see below)      |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, exit 0, unchanged       |

**Record the Worker test count before you start.** Plan 004 also adds two tests
to this same suite and may have run first, so do not assume 16. Run
`cd worker && node --test` and note the `pass` figure; call it **M**. This plan
adds **+2**. Every count below is `M` or `M+2`. If the baseline is not green,
STOP.

## Scope

**In scope** (the only files you should modify):

- `worker/src/index.js`
- `worker/test/interaction.test.js`

**Out of scope** (do NOT touch, even though they look related):

- `createTask` and its D1 `INSERT`. It stays on the acknowledged path
  deliberately: it is a local write, and if it fails the user must be told the
  task did not start.
- `.github/workflows/interactive.yml` — the workflow contract (a
  `workflow_dispatch` with a `task_id` input) is unchanged.
- `interactive_runner.py` — the runner is unaffected; it still fetches its
  input by task id.
- The signature verification at the top of `handleInteraction`. It must remain
  the first thing that happens on the route, before any parsing or side effect.

## Git workflow

- Branch: `advisor/006-ack-before-dispatch`
- Commit style: Conventional Commits.
  Suggested message: `fix: acknowledge Discord before dispatching the workflow`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a helper that reports a dispatch failure to the user

Add this function to `worker/src/index.js` immediately after
`dispatchWorkflow`. It reuses the same `@original` webhook endpoint
`deliverResult` uses, so a background failure still reaches the user in the
same place their "Preparing your tailored CV privately…" message appeared:

```javascript
async function reportDispatchFailure(env, interactionToken, message) {
  const endpoint = `https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${interactionToken}/messages/@original`;
  try {
    await fetch(endpoint, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content: message, flags: 64 }),
    });
  } catch {
    // The user's interaction may already have expired. Nothing further to do:
    // the task row still records the failure for inspection.
  }
}
```

Note this takes the **plaintext** interaction token, which
`handleInteraction` already has as `payload.token` — no decryption needed.

**Verify**: `cd worker && node --test` → still M pass, 0 fail.

### Step 2: Record dispatch failures on the task row

Change `dispatchWorkflow` so a failure is persisted rather than only thrown.
Replace the function with:

```javascript
async function dispatchWorkflow(env, taskId) {
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/interactive.yml/dispatches`, {
    method: "POST",
    headers: { "authorization": `Bearer ${env.GITHUB_TOKEN}`, "accept": "application/vnd.github+json", "content-type": "application/json", "user-agent": "job-search-digest-worker" },
    body: JSON.stringify({ ref: "main", inputs: { task_id: taskId } }),
  });
  if (!response.ok) {
    await env.DB.prepare(
      "UPDATE tasks SET status = 'failed', delivery_error = ? WHERE id = ? AND status = 'received'"
    ).bind(`GitHub workflow dispatch failed: ${response.status}`, taskId).run();
    throw new Error(`GitHub workflow dispatch failed: ${response.status}`);
  }
  await env.DB.prepare("UPDATE tasks SET status = 'dispatched' WHERE id = ? AND status = 'received'").bind(taskId).run();
}
```

`'failed'` is already an accepted terminal status — `callbackTaskStatus`
returns it, and `taskAccessStatus` treats anything outside
`received | dispatched | running | delivery_retryable` as terminal (409). There
is no `CHECK` constraint on `tasks.status` in `worker/schema.sql`, so no schema
change is needed.

**Verify**: `cd worker && node --test` → still M pass, 0 fail.

### Step 3: Move the dispatch off the acknowledged path

Change `handleInteraction` to accept a `ctx` parameter and background the
dispatch. Replace only these two lines:

```javascript
    const taskId = await createTask(env, task, payload.token);
    await dispatchWorkflow(env, taskId);
    return json({ type: 5, data: { content: task.kind === "resume" ? "Reading your CV privately…" : "Preparing your tailored CV privately…", flags: 64 } });
```

with:

```javascript
    const taskId = await createTask(env, task, payload.token);
    // Discord discards the interaction if it is not acknowledged within 3
    // seconds. The GitHub dispatch is an external round-trip, so it runs after
    // the acknowledgement; a failure is reported back through the same message.
    const dispatch = dispatchWorkflow(env, taskId).catch((error) =>
      reportDispatchFailure(env, payload.token, `Could not start that task: ${error.message}`)
    );
    background(ctx, dispatch);
    return json({ type: 5, data: { content: task.kind === "resume" ? "Reading your CV privately…" : "Preparing your tailored CV privately…", flags: 64 } });
```

and change the function signature on line 260 to:

```javascript
async function handleInteraction(request, env, ctx) {
```

Add this small helper near the other utility functions (next to `json` at line
74 is a good home). It is what makes the existing two-argument tests keep
working:

```javascript
function background(ctx, promise) {
  // Tests and any runtime without an execution context fall back to awaiting
  // inline; production Workers always provide ctx.waitUntil.
  if (typeof ctx?.waitUntil === "function") {
    ctx.waitUntil(promise);
    return promise;
  }
  return promise;
}
```

Finally, thread `ctx` through the entry point. Change:

```javascript
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/interactions" && request.method === "POST") return handleInteraction(request, env);
```

to:

```javascript
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/interactions" && request.method === "POST") return handleInteraction(request, env, ctx);
```

Change nothing else in the entry point — the `/internal/` routes keep their
current behavior.

**Verify**: `cd worker && node --test` → M pass, 0 fail. All five existing
`worker.fetch(request, env)` call sites must still pass; if any fail with
"Cannot read properties of undefined", the `background` helper is wrong.

### Step 4: Add tests

Add all three of the following to `worker/test/interaction.test.js`: one shared
helper at module scope, then two tests that call it.

The helper goes next to the existing module-scope helpers `hex()` and
`signedInternalRequest()` — that is the file's established style. It reuses
`hex()` and the `FULL_JD` constant, both already defined in the file, and
`buttonReferenceForJobKey`, which is already imported.

```javascript
// One signed button interaction, plus the env, fake D1, and execution context
// the /interactions route needs. Returns everything the caller asserts on.
async function buttonInteraction() {
  const keys = await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"]);
  const publicKey = Buffer.from(await crypto.subtle.exportKey("raw", keys.publicKey)).toString("base64");
  const jobKey = "greenhouse:provider-42";
  const snapshot = {
    job_key: jobKey,
    jd: { status: "captured", text: FULL_JD, sha256: createHash("sha256").update(FULL_JD).digest("hex") },
  };
  const payload = {
    type: 3,
    guild_id: "guild-1",
    channel_id: "channel-1",
    member: { user: { id: "user-1" } },
    message: { id: "message-1" },
    token: "interaction-token",
    data: { custom_id: `tailor:${await buttonReferenceForJobKey(jobKey)}` },
  };
  const raw = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = await crypto.subtle.sign("Ed25519", keys.privateKey, new TextEncoder().encode(timestamp + raw));
  const request = new Request("https://worker.test/interactions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Signature-Ed25519": hex(signature),
      "X-Signature-Timestamp": timestamp,
    },
    body: raw,
  });

  const db = {
    prepare() {
      return {
        bind() {
          return {
            async first() {
              return { job_key: jobKey, snapshot_json: JSON.stringify(snapshot) };
            },
            async run() {
              return { meta: { changes: 1 } };
            },
          };
        },
      };
    },
  };

  const backgrounded = [];
  return {
    request,
    backgrounded,
    ctx: { waitUntil: (promise) => backgrounded.push(promise) },
    env: {
      DISCORD_PUBLIC_KEY_BASE64: publicKey,
      DISCORD_GUILD_ID: "guild-1",
      DISCORD_CHANNEL_ID: "channel-1",
      DISCORD_USER_ID: "user-1",
      DISCORD_APPLICATION_ID: "app-1",
      GITHUB_REPOSITORY: "owner/repo",
      GITHUB_TOKEN: "placeholder-not-a-real-token",
      TASK_ENCRYPTION_KEY: "test-encryption-key",
      DB: db,
    },
  };
}
```

First test — the acknowledgement does not wait for the dispatch:

```javascript
test("acknowledges a button interaction without waiting for the GitHub dispatch", async () => {
  const { request, env, ctx, backgrounded } = await buttonInteraction();
  const originalFetch = globalThis.fetch;
  let dispatched = 0;
  globalThis.fetch = async () => {
    dispatched += 1;
    return new Response("{}", { status: 204 });
  };

  try {
    const response = await worker.fetch(request, env, ctx);

    assert.equal(response.status, 200);
    assert.equal((await response.json()).type, 5);
    // The acknowledgement was produced with the dispatch still pending.
    assert.equal(backgrounded.length, 1);
    await Promise.all(backgrounded);
    assert.equal(dispatched, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
```

Second test — a dispatch failure reaches the user instead of vanishing:

```javascript
test("a failed GitHub dispatch reports back to the user's interaction", async () => {
  const { request, env, ctx, backgrounded } = await buttonInteraction();
  const originalFetch = globalThis.fetch;
  const patched = [];
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("api.github.com")) return new Response("{}", { status: 500 });
    patched.push({ url: String(url), body: init?.body });
    return new Response("{}", { status: 200 });
  };

  try {
    const response = await worker.fetch(request, env, ctx);

    assert.equal((await response.json()).type, 5);
    await Promise.all(backgrounded);
    assert.equal(patched.length, 1);
    assert.match(patched[0].url, /webhooks\/app-1\/interaction-token\/messages\/@original/);
    assert.match(patched[0].body, /Could not start that task/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
```

Note `GITHUB_TOKEN` in the helper is a placeholder string, not a real
credential. Never put a real token in a test file.

**Verify**: `cd worker && node --test` → M+2 pass, 0 fail.

## Test plan

- New tests in `worker/test/interaction.test.js`:
  - `acknowledges a button interaction without waiting for the GitHub dispatch`
    — the type-5 response is produced with the dispatch still pending in
    `ctx.waitUntil`.
  - `a failed GitHub dispatch reports back to the user's interaction` — the
    background failure reaches the user rather than disappearing.
- Existing tests that must keep passing unchanged: all five `worker.fetch(...)`
  call sites that pass only two arguments.
- Structural pattern: `accepts Discord's hex-encoded Ed25519 signature at the
  interaction route` (line 119) for request signing, and `prunes expired HMAC
  nonces…` (line 299) for the fake D1 shape.
- Verification: `cd worker && node --test` → M+2 pass, 0 fail.

## Done criteria

ALL must hold:

- [ ] `cd worker && node --test` exits 0 with M+2 pass, 0 fail
- [ ] `python -m unittest test_digest.py test_interactive.py` still exits 0 with its count unchanged
- [ ] `grep -n "await dispatchWorkflow" worker/src/index.js` returns no matches
- [ ] `grep -n "async fetch(request, env, ctx)" worker/src/index.js` returns 1 match
- [ ] The signature check is still the first statement after `const raw = await request.text();`
      in `handleInteraction`
- [ ] `git status --porcelain` lists only `worker/src/index.js` and
      `worker/test/interaction.test.js`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `worker/src/index.js:178-186`, `:260-289`, or `:291-294` do not match the
  excerpts in "Current state".
- `cd worker && node --test` fails before you make any change.
- Any existing test breaks because `ctx` is undefined. That means the
  `background` helper is not guarding correctly — fix the helper, do **not**
  edit the existing tests to pass a `ctx`.
- You conclude `createTask` should also move behind `ctx.waitUntil`. It should
  not: the user must learn immediately if the task could not be created.
- Stubbing `globalThis.fetch` turns out to interfere with other tests in the
  file (they run in the same process). If so, report it — the fix is test
  isolation, not abandoning the assertion.

## Maintenance notes

- `ctx.waitUntil` extends the Worker's lifetime but does not make it unbounded.
  If the dispatch path ever grows beyond a single API call, re-evaluate whether
  it belongs in a queue instead.
- The `'failed'` status written on dispatch failure means
  `/internal/tasks/<id>/input` will answer 409 ("task is already terminal") if a
  runner somehow starts anyway. That is the correct outcome; it is also why the
  status is written before the throw.
- If plan 004 lands as well, both plans touch `worker/src/index.js` — 004 adds
  a function after `claimInternalNonce` and one line in `/internal/digest`,
  this one changes `handleInteraction` and the entry point. They should merge
  cleanly, but land one at a time and re-run `node --test` after each.
- Reviewer should scrutinize: that signature verification still precedes every
  side effect, that no `await` was reintroduced before the type-5 return, and
  that the failure report uses the plaintext `payload.token` rather than
  decrypting the stored one.
