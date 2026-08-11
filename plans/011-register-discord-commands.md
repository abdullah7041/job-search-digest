# Plan 011: Make the repository the source of truth for Discord command registration

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 85ccf38..HEAD -- worker/src/index.js worker/package.json worker/wrangler.jsonc`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.
>
> **Base warning**: this plan is written against local `main` at commit
> `85ccf38`. Agent worktrees in this repository have been observed branching
> from `origin/main`, which may be behind. Before doing anything, run
> `git reset --hard main` in your worktree and confirm
> `cd worker && node --test` reports **20 passing tests**. If it reports 16,
> you are on the wrong base — STOP and report.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (adds a script and tests; changes no runtime code path)
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `85ccf38`, 2026-08-11

## Why this matters

`worker/src/index.js` dispatches on two slash-command names:

```javascript
    else if (payload.data?.name === "resume") task = { kind: "resume", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
    else if (payload.data?.name === "tailor-jd") task = { kind: "tailor-jd", userId: allowed.userId, input: { attachment: attachmentFrom(payload) } };
    else return json({ type: 4, data: { content: "Unsupported command.", flags: 64 } });
```

Nothing in this repository ever registers those commands with Discord. There
is no call to `applications/{id}/commands` anywhere, and no registration
script. Whatever is registered today was done by hand, is recorded nowhere, and
drifts silently the moment anyone edits it in the Discord UI.

That produces a question nobody in the repository can answer: *do `/resume` and
`/tailor-jd` exist?* A design spike (`docs/2026-08-10-resume-command-design.md`)
hit exactly this wall and had to record it as an open question requiring either
a live API call or the operator's memory.

This plan does not answer that question. It makes it permanently unaskable:
after this, the registered command set is whatever `worker/commands.js` says,
applied by running one script. The repository becomes the source of truth, and
"what's registered?" is answered by reading a file.

A second, quieter benefit: the command definitions and the handler that
dispatches on them currently have no mechanical link. A test that asserts every
defined command name has a matching branch in `handleInteraction` — and vice
versa — catches the class of bug where someone renames one and not the other,
which would present to the user as "Unsupported command."

**Registration itself is deliberately NOT automated.** No workflow runs this.
It is a one-line manual command the operator runs when the command set changes,
because it needs `DISCORD_BOT_TOKEN` and because pushing command definitions to
a live Discord application is an outward-facing action that should stay
deliberate.

## Current state

Files involved:

- `worker/src/index.js` — the Worker. Dispatches on command names at lines
  337–339; extracts the uploaded file in `attachmentFrom` at line 245.
- `worker/package.json` — no dependencies, ESM, one script.
- `worker/wrangler.jsonc` — holds the application and guild ids as non-secret
  `vars`.
- `worker/test/interaction.test.js` — `node:test` suite, currently 20 tests.

`worker/src/index.js:245-250`, which defines what shape the options must have:

```javascript
function attachmentFrom(payload) {
  const option = payload.data?.options?.find((item) => item.type === 11);
  const attachment = option && payload.data?.resolved?.attachments?.[option.value];
  if (!attachment?.url) throw new Error("Attach a PDF to this command.");
  return { url: attachment.url, filename: attachment.filename || "resume.pdf", contentType: attachment.content_type || "", size: attachment.size || 0 };
}
```

**Option type 11 is `ATTACHMENT`.** Both commands must declare exactly one
option of that type, or `attachmentFrom` throws and the user sees "Attach a PDF
to this command." Type 1 (`CHAT_INPUT`) is the command type.

`worker/package.json` in full:

```json
{
  "private": true,
  "type": "module",
  "scripts": { "test": "node --test" }
}
```

`worker/wrangler.jsonc` — the two ids the script needs are already committed
here as non-secret configuration (they are snowflake identifiers, not
credentials):

```jsonc
  "vars": {
    "DISCORD_APPLICATION_ID": "1535041396904558592",
    "DISCORD_GUILD_ID": "1502683160931205273",
    ...
  }
```

Repo conventions that apply here:

- No dependencies anywhere in `worker/`. Node's built-in `fetch` and
  `node:test` only. **Do not add a package manager step, a `package-lock.json`,
  or any dependency.**
- Exported helpers take their inputs as arguments so tests can drive them
  without network. See `claimInternalNonce(db, nonce)` and
  `pruneExpiredState(db)` in `worker/src/index.js`.
- Tests use a hand-rolled fake rather than a mocking library. See the
  `prunes expired tasks and aged delivery state on the daily digest tick` test.
- User-facing strings are full sentences ("Attach a PDF to this command.").

## Commands you will need

| Purpose      | Command                                                   | Expected on success        |
|--------------|-----------------------------------------------------------|----------------------------|
| Worker tests | `cd worker && node --test`                                | 20 pass before, 24 after   |
| Python tests | `python -m unittest test_digest.py test_interactive.py`   | `OK`, 70 tests, unchanged  |
| Syntax check | `node --check worker/register-commands.mjs`               | exit 0, no output          |

Record the Worker test count you actually observe before starting; this plan
adds **+4**.

## Scope

**In scope** (the only files you should create or modify):

- `worker/commands.js` (create) — the command definitions, exported.
- `worker/register-commands.mjs` (create) — the script that applies them.
- `worker/test/commands.test.js` (create) — tests for the definitions.
- `worker/package.json` — add one npm script entry.
- `README.md` — one short subsection under Setup documenting the command.

**Out of scope** (do NOT touch, even though they look related):

- `worker/src/index.js`. This plan adds registration; it does not change how
  interactions are handled. If you find yourself editing the dispatch branches,
  stop.
- `.github/workflows/*.yml`. Registration is deliberately manual — do not add a
  workflow that runs it. An automated registration step would need
  `DISCORD_BOT_TOKEN` in CI and would re-apply command definitions on every
  push, which is exactly the silent outward-facing change this plan exists to
  prevent.
- `interactive_runner.py` and everything downstream. Registering a command does
  not make it functional — `run_task` still handles only `job_snapshot` tasks.
  That gap is documented in `docs/2026-08-10-resume-command-design.md` and is
  someone else's plan.
- **Do not run the registration script against the live Discord application.**
  Writing it is in scope; executing it is the operator's action, because it
  requires a credential you must not have and mutates a live third-party
  application.

## Git workflow

- Branch: `advisor/011-register-commands`
- Commit style: Conventional Commits
  (example from history: `feat: prune expired Worker task and delivery state`).
  Suggested message: `feat: declare Discord command definitions in the repo`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Define the commands

Create `worker/commands.js`:

```javascript
// The command set this application registers with Discord. This file is the
// source of truth: `register-commands.mjs` PUTs exactly what is here, so a
// command absent from this array is deleted from Discord on the next run.
//
// Types, from Discord's application-command reference:
//   command type 1 = CHAT_INPUT (a slash command)
//   option  type 11 = ATTACHMENT
// attachmentFrom() in src/index.js finds the upload by option type 11, so any
// command that carries a file MUST declare exactly one option of that type.
export const COMMANDS = [
  {
    name: "resume",
    type: 1,
    description: "Upload a new master CV as a PDF. You confirm before it replaces the current one.",
    options: [
      {
        name: "file",
        type: 11,
        description: "Your CV as a PDF.",
        required: true,
      },
    ],
  },
  {
    name: "tailor-jd",
    type: 1,
    description: "Supply a full job description as a PDF for a role whose posting could not be captured.",
    options: [
      {
        name: "file",
        type: 11,
        description: "The job description as a PDF.",
        required: true,
      },
    ],
  },
];

// Every name here must have a matching dispatch branch in handleInteraction.
export const COMMAND_NAMES = COMMANDS.map((command) => command.name);
```

Descriptions must be 1–100 characters (Discord's limit); names must be
lowercase, 1–32 characters, matching `^[-_a-z0-9]{1,32}$`. The tests in step 3
assert both.

**Verify**: `node --check worker/commands.js` → exit 0.

### Step 2: Write the registration script

Create `worker/register-commands.mjs`:

```javascript
#!/usr/bin/env node
// Applies worker/commands.js to the Discord application, guild-scoped.
//
// Guild commands, not global, for two reasons: they apply instantly (global
// commands can take up to an hour to propagate), and this bot is scoped to one
// guild anyway — authorizeInteraction() in src/index.js rejects every
// interaction from anywhere else.
//
// PUT is deliberate: it replaces the whole command set with what is in
// commands.js, so removing a command from that file removes it from Discord.
// POST would only ever add, letting stale commands accumulate invisibly.
//
// Run manually when the command set changes:
//   DISCORD_BOT_TOKEN=... npm run register --prefix worker
//
// The token is never read from a file and never logged.
import { COMMANDS } from "./commands.js";

const token = process.env.DISCORD_BOT_TOKEN;
const applicationId = process.env.DISCORD_APPLICATION_ID;
const guildId = process.env.DISCORD_GUILD_ID;

const missing = Object.entries({ DISCORD_BOT_TOKEN: token, DISCORD_APPLICATION_ID: applicationId, DISCORD_GUILD_ID: guildId })
  .filter(([, value]) => !value)
  .map(([name]) => name);

if (missing.length > 0) {
  console.error(`Missing required environment variables: ${missing.join(", ")}`);
  console.error("Application and guild ids are in worker/wrangler.jsonc under vars.");
  process.exit(2);
}

const endpoint = `https://discord.com/api/v10/applications/${applicationId}/guilds/${guildId}/commands`;
const response = await fetch(endpoint, {
  method: "PUT",
  headers: {
    authorization: `Bot ${token}`,
    "content-type": "application/json",
    "user-agent": "job-search-digest-register",
  },
  body: JSON.stringify(COMMANDS),
});

if (!response.ok) {
  // Discord's error body names the offending field; it contains no credential.
  console.error(`Discord rejected the command registration: HTTP ${response.status}`);
  console.error((await response.text()).slice(0, 2000));
  process.exit(1);
}

const registered = await response.json();
console.log(`Registered ${registered.length} command(s) on guild ${guildId}:`);
for (const command of registered) {
  console.log(`  /${command.name} — ${command.description}`);
}
```

Note it reads the ids from the environment rather than parsing
`wrangler.jsonc` — that file is JSONC (comments allowed), and adding a JSONC
parser would mean adding a dependency. The values are printed in the error
message's pointer instead.

**Verify**: `node --check worker/register-commands.mjs` → exit 0.

Then confirm it refuses to run without a token, which must exit 2 and print the
missing names — run it with the variables explicitly cleared:

```bash
cd worker && env -u DISCORD_BOT_TOKEN -u DISCORD_APPLICATION_ID -u DISCORD_GUILD_ID node register-commands.mjs; echo "exit=$?"
```

→ prints the three missing variable names and `exit=2`. **It must not make a
network call.** If it exits 0 or attempts a request, the guard is wrong.

### Step 3: Add tests

Create `worker/test/commands.test.js`. The fourth test is the important one —
it is the mechanical link between the definitions and the handler:

```javascript
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { COMMANDS, COMMAND_NAMES } from "../commands.js";

test("every command satisfies Discord's name and description limits", () => {
  for (const command of COMMANDS) {
    assert.match(command.name, /^[-_a-z0-9]{1,32}$/, `bad name: ${command.name}`);
    assert.equal(command.type, 1);
    assert.ok(command.description.length >= 1 && command.description.length <= 100,
      `description out of range for ${command.name}`);
  }
});

test("every file-carrying command declares exactly one attachment option", () => {
  // attachmentFrom() in src/index.js locates the upload by option type 11 and
  // throws if it is absent, so this is what makes the command usable at all.
  for (const command of COMMANDS) {
    const attachments = (command.options || []).filter((option) => option.type === 11);
    assert.equal(attachments.length, 1, `${command.name} must declare one attachment option`);
    assert.equal(attachments[0].required, true);
  }
});

test("command names are unique", () => {
  assert.equal(new Set(COMMAND_NAMES).size, COMMAND_NAMES.length);
});

test("every registered command name has a dispatch branch in the worker", () => {
  // Guards the drift that would present to the user as "Unsupported command."
  const source = readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
  for (const name of COMMAND_NAMES) {
    assert.ok(
      source.includes(`payload.data?.name === "${name}"`),
      `worker/src/index.js has no dispatch branch for /${name}`,
    );
  }
  const dispatched = [...source.matchAll(/payload\.data\?\.name === "([^"]+)"/g)].map((m) => m[1]);
  for (const name of dispatched) {
    assert.ok(COMMAND_NAMES.includes(name),
      `worker/src/index.js dispatches on /${name}, which commands.js does not register`);
  }
});
```

**Verify**: `cd worker && node --test` → 4 more tests than your recorded
baseline, 0 fail.

### Step 4: Add the npm script

In `worker/package.json`, add one entry to `scripts`. The file becomes:

```json
{
  "private": true,
  "type": "module",
  "scripts": {
    "test": "node --test",
    "register": "node register-commands.mjs"
  }
}
```

Do not add dependencies, a `devDependencies` block, or an `engines` field.

**Verify**: `cd worker && npm run register 2>&1 | head -3` → prints the missing
environment variable message (because no token is set), not a module error.

### Step 5: Document it

Add a short subsection to `README.md` under the existing Setup section, after
the Worker configuration steps. It must state: what the file is, that it is the
source of truth, that `PUT` deletes anything not listed, that it is run
manually, and the exact command. Name `DISCORD_BOT_TOKEN` — never a value.

Suggested wording, adapt to the surrounding voice:

> **Registering the slash commands.** `worker/commands.js` is the source of
> truth for which slash commands exist. `npm run register --prefix worker`
> applies it to the Discord application, guild-scoped so it takes effect
> immediately. It uses `PUT`, so a command removed from that file is removed
> from Discord on the next run. It needs `DISCORD_BOT_TOKEN`,
> `DISCORD_APPLICATION_ID` and `DISCORD_GUILD_ID` in the environment — the
> latter two are in `worker/wrangler.jsonc`. Run it by hand when the command
> set changes; no workflow runs it.

**Verify**: `grep -c "register-commands\|npm run register" README.md` → at
least 1.

### Step 6: Full verification

**Verify**:
- `cd worker && node --test` → baseline+4 pass, 0 fail
- `python -m unittest test_digest.py test_interactive.py` → `OK`, 70 tests
- `git status --porcelain` → only the five in-scope files
- `git diff | grep -iE "bot [A-Za-z0-9._-]{20,}"` → no matches (no token in the diff)

## Test plan

- New tests in `worker/test/commands.test.js`:
  - name/type/description conform to Discord's limits
  - each command declares exactly one required type-11 attachment option — the
    thing `attachmentFrom` depends on
  - names are unique
  - **definitions and dispatch branches agree in both directions** — the
    regression guard that matters
- Structural pattern: `worker/test/interaction.test.js` — same `node:test`
  imports, same plain `assert.strict` style.
- Verification: `cd worker && node --test` → baseline+4, 0 fail.

## Done criteria

ALL must hold:

- [ ] `cd worker && node --test` exits 0 with 4 more tests than the baseline you recorded
- [ ] `python -m unittest test_digest.py test_interactive.py` exits 0 with 70 tests
- [ ] `node --check worker/register-commands.mjs` and `node --check worker/commands.js` both exit 0
- [ ] Running the script with no `DISCORD_BOT_TOKEN` exits 2, names the missing
      variables, and makes no network request
- [ ] `grep -n 'method:' worker/register-commands.mjs` shows `method: "PUT"` and
      nothing else — the request must be `PUT`, so the file is authoritative.
      (Corrected during review: this criterion originally read
      `grep -c "POST" ... returns 0`, which the plan's own prescribed script
      could never satisfy — its comment block explains *why not* POST and so
      contains the word. Check the method, not the string.)
- [ ] `git grep -n "DISCORD_BOT_TOKEN" worker/` shows it read from
      `process.env` only, never assigned a literal
- [ ] No credential value anywhere in the diff
- [ ] `git status --porcelain` lists only `worker/commands.js`,
      `worker/register-commands.mjs`, `worker/test/commands.test.js`,
      `worker/package.json`, `README.md`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `cd worker && node --test` reports 16 rather than 20 before you start — you
  are on a stale base (see the base warning at the top).
- `worker/src/index.js:245` or `:337-339` do not match the excerpts in
  "Current state".
- You are about to run the registration script against the live Discord
  application. Writing it is your job; running it is the operator's.
- You are about to add a dependency, a lockfile, or a CI step that registers
  commands. All three are out of scope and the last one is a deliberate
  design decision, not an oversight.
- The step-3 cross-check test fails on first run. That would mean the names in
  `commands.js` do not match the handler's branches — report which, rather than
  changing the handler to fit (the handler is out of scope).
- You conclude the commands should be global rather than guild-scoped. Report
  the reasoning; do not switch, because global commands propagate slowly and
  this application rejects every interaction outside its one configured guild.

## Maintenance notes

- **This plan makes the commands *registerable*, not *functional*.** Registering
  `/resume` and `/tailor-jd` means Discord will show them and the Worker will
  accept them — but `interactive_runner.run_task` still handles only
  `job_snapshot` tasks, so both still fail with "The task has no immutable job
  snapshot." That gap is the subject of
  `docs/2026-08-10-resume-command-design.md`. Consider registering only
  `/resume` (comment `tailor-jd` out of the array) until its handler exists, so
  the visible command set matches what actually works.
- The cross-check test in step 3 reads `src/index.js` as text. If the dispatch
  ever moves to a lookup table or a switch, that regex breaks and the test will
  fail loudly rather than silently pass — which is the right failure, but
  update the test alongside the refactor.
- `PUT` is destructive by design. If someone adds a command through the Discord
  UI and does not add it to `commands.js`, the next registration run deletes it.
  That is the point, and it is why the README wording says so explicitly.
- Reviewer should scrutinize: that the request is `PUT` not `POST`, that the
  token is only ever read from `process.env`, that no workflow runs the script,
  and that the bidirectional name check actually fails when a name is changed
  on one side (try it locally before approving).
