import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import test from "node:test";

import worker, {
  authorizeInteraction,
  buttonTask,
  callbackTaskStatus,
  buttonReferenceForJobKey,
  claimInternalNonce,
  claimTaskResult,
  postCard,
  pruneExpiredState,
  releaseTaskResult,
  resumeComponentAction,
  confirmResumePreview,
  createResumePreview,
  currentConfirmedResume,
  tailoringInputFromSnapshotRow,
  taskAccessStatus,
  taskExpiryMilliseconds,
  PASTE_MODAL,
  manualSnapshot,
  manualTaskFromCommand,
  manualTaskFromModal,
} from "../src/index.js";

const env = {
  DISCORD_GUILD_ID: "guild-1",
  DISCORD_CHANNEL_ID: "channel-1",
  DISCORD_USER_ID: "user-1",
};

const FULL_JD = "About the role: Example Labs is hiring a senior engineer to build reliable machine learning products. Responsibilities include designing production Python services, developing LLM retrieval systems, reviewing architecture, monitoring model quality, documenting decisions, and partnering with product and data teams. Requirements include five years of software engineering experience, strong Python and SQL, hands-on API design, cloud deployment, automated testing, observability, and clear written communication. Candidates should have delivered maintainable systems, handled production incidents, and worked effectively with cross-functional teams. Experience with containers, CI/CD, evaluation pipelines, and responsible AI practices is preferred.";

test("rejects an interaction outside the configured private channel", () => {
  const result = authorizeInteraction({ guild_id: "guild-1", channel_id: "elsewhere", member: { user: { id: "user-1" } } }, env);
  assert.equal(result.ok, false);
  assert.match(result.message, /private job-search channel/i);
});

test("creates a tailoring task from an opaque card button", () => {
  const result = buttonTask({ data: { custom_id: "tailor:f765d605515d048a68135fcbf73b66f2" }, member: { user: { id: "user-1" } } });
  assert.deepEqual(result, { kind: "tailor", jobRef: "f765d605515d048a68135fcbf73b66f2", userId: "user-1" });
});

test("derives a Discord-safe opaque button reference from a job key", async () => {
  const reference = await buttonReferenceForJobKey("greenhouse:provider-42");
  assert.equal(reference, "f765d605515d048a68135fcbf73b66f2");
  assert.equal(reference.length, 32);
});

test("rejects malformed card button identifiers", () => {
  assert.throws(() => buttonTask({ data: { custom_id: "tailor:" }, member: { user: { id: "user-1" } } }), /invalid/i);
});

test("parses a digest-bound resume confirmation before tailoring button routing", () => {
  assert.deepEqual(
    resumeComponentAction({
      data: { custom_id: "resume-ok:abcdef12-3456-7890-abcd-ef1234567890:0123456789abcdef0123456789abcdef" },
      member: { user: { id: "user-1" } },
    }),
    {
      action: "confirm",
      previewId: "abcdef12-3456-7890-abcd-ef1234567890",
      digestPrefix: "0123456789abcdef0123456789abcdef",
      userId: "user-1",
    },
  );
  assert.throws(
    () => resumeComponentAction({ data: { custom_id: "resume-ok:abcdef12-3456-7890-abcd-ef1234567890:bad" }, member: { user: { id: "user-1" } } }),
    /invalid resume/i,
  );
});

test("does not delete a preview when its atomic confirmation loses a version race", async () => {
  const statements = [];
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return { sql, args, async run() { return { meta: { changes: 0 } }; } };
        },
      };
    },
    async batch(batch) {
      statements.push(...batch);
      return [{ meta: { changes: 0 } }];
    },
  };

  const result = await confirmResumePreview(db, "user-1", "preview-123", "0123456789abcdef0123456789abcdef");

  assert.deepEqual(result, { ok: false, reason: "stale" });
  assert.equal(statements.length, 1);
  assert.match(statements[0].sql, /INSERT INTO confirmed_resumes/);
});

test("stores a pending preview against the current confirmed-resume version", async () => {
  const writes = [];
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async first() {
              return sql.startsWith("SELECT markdown")
                ? { markdown: "# Existing", source_sha256: "old", version: 4 }
                : null;
            },
            async run() {
              writes.push({ sql, args });
              return { meta: { changes: 1 } };
            },
          };
        },
      };
    },
  };

  const current = await currentConfirmedResume(db, "user-1");
  const preview = await createResumePreview(db, "user-1", "# Updated", "f".repeat(64));

  assert.deepEqual(current, { markdown: "# Existing", source_sha256: "old", version: 4 });
  assert.equal(preview.baseVersion, 4);
  assert.equal(preview.digestPrefix, "f".repeat(32));
  assert.match(preview.id, /^[0-9a-f-]{36}$/i);
  assert.equal(writes.length, 1);
  assert.match(writes[0].sql, /INSERT INTO resume_previews/);
  assert.equal(writes[0].args[4], 4);
});

test("expires a tailoring interaction before Discord's token window", () => {
  assert.equal(taskExpiryMilliseconds("tailor"), 14 * 60 * 1000);
  assert.equal(taskExpiryMilliseconds("resume"), 24 * 60 * 60 * 1000);
});

test("copies the exact full-JD snapshot into a tailoring task", async () => {
  const snapshot = {
    job_key: "greenhouse:provider-42",
    title: "Senior Machine Learning Engineer",
    company: "Example Labs",
    jd: {
      status: "captured",
      text: FULL_JD,
      sha256: createHash("sha256").update(FULL_JD).digest("hex"),
    },
  };

  const input = await tailoringInputFromSnapshotRow(
    { job_key: snapshot.job_key, snapshot_json: JSON.stringify(snapshot) },
    snapshot.job_key,
  );

  assert.deepEqual(input, { job_snapshot: snapshot });
});

test("rejects a stored snapshot whose JD no longer matches its digest", async () => {
  const snapshot = {
    job_key: "greenhouse:provider-42",
    jd: { status: "captured", text: "Changed JD", sha256: "not-the-real-hash" },
  };

  await assert.rejects(
    tailoringInputFromSnapshotRow(
      { job_key: snapshot.job_key, snapshot_json: JSON.stringify(snapshot) },
      snapshot.job_key,
    ),
    /digest/i,
  );
});

test("rejects a hash-valid JD fragment even if it is labeled captured", async () => {
  const text = "Python and SQL required.";
  const snapshot = {
    job_key: "greenhouse:provider-42",
    jd: {
      status: "captured",
      text,
      sha256: "c71fad90f4bf927822b6ef96a0232e2a40b2e38c453629a571e69d8cd9f9537b",
      completeness_failures: [],
    },
  };

  await assert.rejects(
    tailoringInputFromSnapshotRow(
      { job_key: snapshot.job_key, snapshot_json: JSON.stringify(snapshot) },
      snapshot.job_key,
    ),
    /complete job description/i,
  );
});

test("persists the runner's explicit completed or failed status", () => {
  assert.equal(callbackTaskStatus({ status: "completed" }), "completed");
  assert.equal(callbackTaskStatus({ status: "failed" }), "failed");
  assert.throws(() => callbackTaskStatus({ status: "running" }), /status/i);
});

function hex(bytes) {
  return Buffer.from(bytes).toString("hex");
}

test("accepts Discord's hex-encoded Ed25519 signature at the interaction route", async () => {
  const keys = await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"]);
  const publicKey = Buffer.from(await crypto.subtle.exportKey("raw", keys.publicKey)).toString("base64");
  const timestamp = String(Math.floor(Date.now() / 1000));
  const raw = JSON.stringify({ type: 1 });
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

  const response = await worker.fetch(request, { DISCORD_PUBLIC_KEY_BASE64: publicKey });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { type: 1 });
});

function signedInternalRequest(method, path, body, secret, timestamp = String(Math.floor(Date.now() / 1000)), nonce = crypto.randomUUID()) {
  const canonical = `${timestamp}\n${nonce}\n${method}\n${path}\n${body}`;
  const signature = createHmac("sha256", secret).update(canonical).digest("base64");
  return new Request(`https://worker.test${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Job-Digest-Signature": signature,
      "X-Job-Digest-Timestamp": timestamp,
      "X-Job-Digest-Nonce": nonce,
    },
    body: method === "GET" ? undefined : body,
  });
}

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

test("acknowledges a button interaction without waiting for the GitHub dispatch", async () => {
  const { request, env, ctx, backgrounded } = await buttonInteraction();
  const originalFetch = globalThis.fetch;
  let dispatched = 0;
  globalThis.fetch = async () => {
    dispatched += 1;
    return new Response(null, { status: 204 });
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

// A signed /resume slash-command interaction, plus the env, execution
// context, and a fake D1 that records every SQL string it is given -- so a
// refusal can be proven to have written nothing, the same way buttonInteraction
// above proves a real dispatch. The attachment is well-formed even though the
// refused branch never reads it, so this models a genuine upload attempt.
async function resumeCommandInteraction() {
  const keys = await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"]);
  const publicKey = Buffer.from(await crypto.subtle.exportKey("raw", keys.publicKey)).toString("base64");
  const attachmentId = "attachment-1";
  const payload = {
    type: 2,
    guild_id: "guild-1",
    channel_id: "channel-1",
    member: { user: { id: "user-1" } },
    token: "interaction-token",
    data: {
      name: "resume",
      options: [{ name: "file", type: 11, value: attachmentId }],
      resolved: {
        attachments: {
          [attachmentId]: { url: "https://cdn.discordapp.test/cv.pdf", filename: "cv.pdf" },
        },
      },
    },
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

  const executed = [];
  const db = {
    prepare(sql) {
      executed.push(sql);
      return {
        bind() {
          return {
            async first() { return null; },
            async run() { return { meta: { changes: 1 } }; },
          };
        },
      };
    },
  };

  const backgrounded = [];
  return {
    request,
    executed,
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

test("uploading a CV is refused before a task row or a workflow is spent", async () => {
  const { request, env, ctx, executed, backgrounded } = await resumeCommandInteraction();
  const originalFetch = globalThis.fetch;
  const calledUrls = [];
  globalThis.fetch = async (url) => {
    calledUrls.push(String(url));
    return new Response("{}", { status: 200 });
  };

  try {
    const response = await worker.fetch(request, env, ctx);
    const body = await response.json();

    assert.equal(response.status, 200);
    assert.equal(body.type, 4);
    assert.equal(body.data.flags, 64);
    assert.match(body.data.content, /not wired up yet/);
    assert.ok(!executed.some((sql) => /INSERT INTO tasks/.test(sql)), "no task row should be written");
    assert.ok(!calledUrls.some((url) => url.includes("api.github.com")), "no workflow should be dispatched");
    assert.equal(backgrounded.length, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("binds internal HMAC authentication to the method, path, and fresh timestamp", async () => {
  const secret = "callback-secret";
  const nonces = new Set();
  const db = {
    prepare() {
      return { bind(nonce) { return { async run() { if (nonces.has(nonce)) return { meta: { changes: 0 } }; nonces.add(nonce); return { meta: { changes: 1 } }; } }; } };
    },
  };
  const request = signedInternalRequest("POST", "/internal/digest", '{"cards":[]}', secret);
  const replay = request.clone();
  const response = await worker.fetch(request, { CALLBACK_HMAC_SECRET: secret, DB: db });
  assert.equal(response.status, 200);

  assert.equal((await worker.fetch(replay, { CALLBACK_HMAC_SECRET: secret, DB: db })).status, 409);

  const wrongPath = signedInternalRequest("POST", "/internal/digest", '{"cards":[]}', secret);
  const replayed = new Request("https://worker.test/internal/other", wrongPath);
  assert.equal((await worker.fetch(replayed, { CALLBACK_HMAC_SECRET: secret, DB: db })).status, 401);

  const stale = signedInternalRequest("POST", "/internal/digest", '{"cards":[]}', secret, "1");
  assert.equal((await worker.fetch(stale, { CALLBACK_HMAC_SECRET: secret, DB: db })).status, 401);
});

test("returns only the configured user's confirmed resume to a signed daily sync", async () => {
  const secret = "callback-secret";
  const db = {
    prepare(sql) {
      return {
        bind() {
          return {
            async run() { return { meta: { changes: 1 } }; },
            async first() {
              if (sql.startsWith("SELECT markdown")) {
                return { markdown: "# Confirmed", source_sha256: "a".repeat(64), version: 2 };
              }
              return null;
            },
          };
        },
      };
    },
  };
  const request = signedInternalRequest("GET", "/internal/confirmed-resume", "", secret);

  const response = await worker.fetch(request, {
    CALLBACK_HMAC_SECRET: secret,
    DISCORD_USER_ID: "user-1",
    DB: db,
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { markdown: "# Confirmed", source_sha256: "a".repeat(64), version: 2 });
});

test("rejects expired and terminal tasks at internal task routes", () => {
  const now = new Date("2026-08-07T12:00:00Z");
  assert.equal(taskAccessStatus({ status: "dispatched", expires_at: "2026-08-07T11:59:59Z" }, now), 410);
  assert.equal(taskAccessStatus({ status: "completed", expires_at: "2026-08-07T12:01:00Z" }, now), 409);
  assert.equal(taskAccessStatus({ status: "running", expires_at: "2026-08-07T12:01:00Z" }, now), 200);
  assert.equal(taskAccessStatus({ status: "delivery_retryable", expires_at: "2026-08-07T12:01:00Z" }, now), 200);
});

test("retries an uncertain Discord post with the same enforced nonce and marks only confirmed delivery", async () => {
  const claims = new Map();
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              if (sql.startsWith("INSERT INTO delivery_claims")) {
                const current = claims.get(args[0]);
                if (current && current.state !== "retryable") return { meta: { changes: 0 } };
                claims.set(args[0], { state: "posting" });
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE delivery_claims SET state = 'retryable'")) claims.set(args[1], { state: "retryable" });
              if (sql.startsWith("UPDATE delivery_claims SET state = 'posted'")) claims.set(args[1], { state: "posted" });
              return { meta: { changes: 1 } };
            },
            async first() { return claims.get(args[0]) || null; },
          };
        },
      };
    },
    async batch(statements) {
      return Promise.all(statements.map((statement) => statement.run()));
    },
  };
  const originalFetch = globalThis.fetch;
  let discordPosts = 0;
  const bodies = [];
  const urls = [];
  globalThis.fetch = async (url, options) => {
    discordPosts += 1;
    urls.push(url);
    bodies.push(JSON.parse(options.body));
    if (discordPosts === 1) throw new Error("connection reset after upload");
    return new Response(JSON.stringify({ id: "message-1" }), { status: 200 });
  };
  const card = {
    job_key: "greenhouse:provider-42",
    snapshot: { job_key: "greenhouse:provider-42", captured_at: "2026-08-03T12:00:00Z" },
    payload: { content: "job" },
  };
  try {
    assert.equal((await postCard({ DB: db, DISCORD_CHANNEL_ID: "channel", DISCORD_BOT_TOKEN: "token" }, card)).status, "retryable");
    assert.equal((await postCard({ DB: db, DISCORD_CHANNEL_ID: "channel", DISCORD_BOT_TOKEN: "token" }, card)).status, "posted");
    assert.equal((await postCard({ DB: db, DISCORD_CHANNEL_ID: "channel", DISCORD_BOT_TOKEN: "token" }, card)).status, "existing");
    assert.equal((await postCard(
      { DB: db, DISCORD_CHANNEL_ID: "channel", DISCORD_BOT_TOKEN: "token" },
      { job_key: "heartbeat:2026-08-07", payload: { content: "No new roles today." } },
    )).status, "posted");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(discordPosts, 3);
  assert.equal(bodies[0].nonce, bodies[1].nonce);
  assert.equal(bodies[1].enforce_nonce, true);
  assert.equal(bodies[2].content, "No new roles today.");
  assert.match(urls[2], /channels\/channel\/messages$/);
});

test("atomically allows only one concurrent result delivery claim", async () => {
  let status = "running";
  const db = {
    prepare() {
      return { bind() { return { async run() { if (!["received", "dispatched", "running"].includes(status)) return { meta: { changes: 0 } }; status = "delivering"; return { meta: { changes: 1 } }; } }; } };
    },
  };

  const claims = await Promise.all([claimTaskResult(db, "task-1"), claimTaskResult(db, "task-1")]);

  assert.deepEqual(claims.sort(), [false, true]);
});

test("rolls a failed result delivery back to retryable without reopening a completed task", async () => {
  let status = "running";
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              if (sql.startsWith("UPDATE tasks SET status = 'delivering'")) {
                if (!["received", "dispatched", "running", "delivery_retryable"].includes(status)) return { meta: { changes: 0 } };
                status = "delivering";
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE tasks SET status = 'delivery_retryable'")) {
                if (status !== "delivering") return { meta: { changes: 0 } };
                status = "delivery_retryable";
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE tasks SET status = ?")) {
                status = args[0];
                return { meta: { changes: 1 } };
              }
              return { meta: { changes: 0 } };
            },
          };
        },
      };
    },
  };

  assert.equal(await claimTaskResult(db, "task-1"), true);
  assert.equal(await releaseTaskResult(db, "task-1", "Discord 503"), true);
  assert.equal(status, "delivery_retryable");
  assert.equal(await claimTaskResult(db, "task-1"), true);
  status = "completed";
  assert.equal(await claimTaskResult(db, "task-1"), false);
});

test("prunes expired HMAC nonces while atomically claiming a new nonce", async () => {
  const nonces = new Map([["expired", "expired"], ["active", "active"]]);
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              if (sql.startsWith("DELETE FROM internal_nonces")) {
                nonces.delete("expired");
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("INSERT OR IGNORE INTO internal_nonces")) {
                if (nonces.has(args[0])) return { meta: { changes: 0 } };
                nonces.set(args[0], "active");
                return { meta: { changes: 1 } };
              }
              return { meta: { changes: 0 } };
            },
          };
        },
      };
    },
  };

  assert.equal(await claimInternalNonce(db, "new-nonce-123456"), true);
  assert.equal(nonces.has("expired"), false);
  assert.equal(nonces.has("active"), true);
  assert.equal(nonces.has("new-nonce-123456"), true);
  assert.equal(await claimInternalNonce(db, "active"), false);
});

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

  assert.equal(executed.length, 5);
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM tasks")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM job_snapshots")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM deliveries")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM delivery_claims")));
  assert.ok(executed.some((sql) => sql.startsWith("DELETE FROM resume_previews")));
  // Live coordination state is never swept.
  const claims = executed.find((sql) => sql.startsWith("DELETE FROM delivery_claims"));
  assert.match(claims, /state = 'posted'/);
  assert.deepEqual(removed, { tasks: 2, job_snapshots: 2, deliveries: 2, delivery_claims: 2, resume_previews: 2 });
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

// A tailored CV used to be PATCHed onto the interaction message with flags: 64.
// Ephemeral messages are absent from channel history and vanish on client
// reload, so the document the user asked for disappeared and no copy survived
// anywhere. These tests pin the durable channel post that replaced it.
function resultDelivery(taskResult, { channelStatus = 200, ackStatus = 200 } = {}) {
  let status = "running";
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async first() {
              if (sql.startsWith("SELECT * FROM tasks")) {
                return {
                  id: "task-1",
                  status,
                  expires_at: new Date(Date.now() + 600000).toISOString(),
                  interaction_token: "not-a-decryptable-token",
                };
              }
              return null;
            },
            async run() {
              if (sql.startsWith("INSERT OR IGNORE INTO internal_nonces")) return { meta: { changes: 1 } };
              if (sql.startsWith("UPDATE tasks SET status = 'delivering'")) {
                if (status !== "running") return { meta: { changes: 0 } };
                status = "delivering";
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE tasks SET status = 'delivery_retryable'")) {
                status = "delivery_retryable";
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE tasks SET status = ?")) {
                status = args[0];
                return { meta: { changes: 1 } };
              }
              return { meta: { changes: 0 } };
            },
          };
        },
      };
    },
  };

  const calls = [];
  const secret = "callback-secret";
  const body = JSON.stringify(taskResult);
  const request = signedInternalRequest("POST", "/internal/tasks/task-1/result", body, secret);
  const env = {
    CALLBACK_HMAC_SECRET: secret,
    DISCORD_CHANNEL_ID: "channel-1",
    DISCORD_APPLICATION_ID: "app-1",
    DISCORD_BOT_TOKEN: "bot-token",
    TASK_ENCRYPTION_KEY: "test-encryption-key",
    DB: db,
  };

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const entry = { url: String(url), method: init?.method, headers: init?.headers || {}, body: init?.body };
    calls.push(entry);
    const isChannelPost = entry.url.includes("/channels/");
    return new Response("{}", { status: isChannelPost ? channelStatus : ackStatus });
  };

  return {
    calls,
    taskStatus: () => status,
    async run() {
      try {
        return await worker.fetch(request, env, {});
      } finally {
        globalThis.fetch = originalFetch;
      }
    },
  };
}

test("posts a tailored CV to the channel so it survives a Discord reload", async () => {
  const delivery = resultDelivery({
    status: "completed",
    content: "Tailored one-page CV for the role.",
    pdf_base64: Buffer.from("%PDF-1.4 fake").toString("base64"),
    filename: "tailored-example-labs-ml-engineer.pdf",
  });

  const response = await delivery.run();

  assert.equal(response.status, 200);
  const posted = delivery.calls[0];
  assert.match(posted.url, /channels\/channel-1\/messages$/);
  assert.equal(posted.method, "POST");
  assert.equal(posted.headers.authorization, "Bot bot-token");
  assert.ok(posted.body instanceof FormData);
  // Ephemeral is what made the document disappear, so nothing may set it.
  assert.equal(JSON.parse(posted.body.get("payload_json")).flags, undefined);
  assert.equal(posted.body.get("files[0]").name, "tailored-example-labs-ml-engineer.pdf");
  assert.equal(delivery.taskStatus(), "completed");
});

test("posts a failure to the channel too, rather than into a message that vanishes", async () => {
  // The rarer branch: _tailored_pdf returns a PDF even when the model fails, so
  // this fires only when the runner itself rejects the task. It is also the
  // message most worth keeping, because it names what went wrong.
  const delivery = resultDelivery({
    status: "failed",
    content: "Interactive task failed safely: the job snapshot has no stable key.",
  });

  const response = await delivery.run();

  assert.equal(response.status, 200);
  const posted = delivery.calls[0];
  assert.match(posted.url, /channels\/channel-1\/messages$/);
  assert.equal(posted.method, "POST");
  assert.equal(JSON.parse(posted.body).flags, undefined);
  assert.match(JSON.parse(posted.body).content, /failed safely/);
  assert.equal(delivery.taskStatus(), "failed");
});

test("a dead interaction token cannot cost the user an already-posted CV", async () => {
  // The workflow can outlive Discord's interaction token. The ephemeral ack is
  // a convenience; the channel message is the deliverable.
  const delivery = resultDelivery({
    status: "completed",
    content: "Tailored one-page CV for the role.",
    pdf_base64: Buffer.from("%PDF-1.4 fake").toString("base64"),
  }, { ackStatus: 401 });

  const response = await delivery.run();

  assert.equal(response.status, 200);
  assert.equal(delivery.taskStatus(), "completed");
});

test("a failed channel post leaves the task retryable instead of silently completed", async () => {
  const delivery = resultDelivery({
    status: "completed",
    content: "Tailored one-page CV for the role.",
    pdf_base64: Buffer.from("%PDF-1.4 fake").toString("base64"),
  }, { channelStatus: 500 });

  const response = await delivery.run();

  assert.equal(response.status, 502);
  assert.equal(delivery.taskStatus(), "delivery_retryable");
});

test("a snapshot with no captured JD reaches the runner instead of ending the click", async () => {
  // This used to throw "A full job description is required; use /tailor-jd to
  // provide it." -- pointing at a command that was not registered, for the one
  // case that is actually recoverable. The runner reads the posting again
  // before it gives up, so the snapshot has to get that far.
  const snapshot = {
    job_key: "jsearch:https://sa.jooble.org/jdp/-19135",
    url: "https://sa.jooble.org/jdp/-19135",
    jd: { status: "missing", text: "", sha256: "", completeness_failures: ["no text"] },
  };

  const input = await tailoringInputFromSnapshotRow(
    { job_key: snapshot.job_key, snapshot_json: JSON.stringify(snapshot) },
    snapshot.job_key,
  );

  assert.deepEqual(input, { job_snapshot: snapshot });
});

test("the paste modal fits Discord's limits and takes a description in two parts", () => {
  // One text input caps at 4000 characters and a real posting runs past that,
  // so the description arrives in two boxes. Five rows is the modal maximum.
  assert.ok(PASTE_MODAL.components.length <= 5);
  const fields = PASTE_MODAL.components.map((row) => row.components[0]);
  const byId = Object.fromEntries(fields.map((field) => [field.custom_id, field]));
  assert.equal(byId.jd_1.style, 2);
  assert.equal(byId.jd_1.required, true);
  assert.equal(byId.jd_2.required, false);
  assert.equal(byId.jd_1.max_length + byId.jd_2.max_length, 8000);
  assert.equal(byId.url.required, false);
  for (const field of fields) assert.equal(field.type, 4);
});

test("a pasted description becomes a tailoring task with both boxes joined", async () => {
  // Split where a person would: at a sentence boundary, because the boxes are
  // joined with a newline and gluing two halves of a word back together is not
  // something a paste-in-two-parts flow can be asked to do.
  const half = FULL_JD.indexOf(" ", Math.ceil(FULL_JD.length / 2)) + 1;
  const payload = {
    type: 5,
    data: {
      custom_id: "tailor-jd-paste",
      components: [
        { type: 1, components: [{ custom_id: "title", value: "AI/ML Support Analyst" }] },
        { type: 1, components: [{ custom_id: "company", value: "KAUST" }] },
        { type: 1, components: [{ custom_id: "url", value: "" }] },
        { type: 1, components: [{ custom_id: "jd_1", value: FULL_JD.slice(0, half) }] },
        { type: 1, components: [{ custom_id: "jd_2", value: FULL_JD.slice(half) }] },
      ],
    },
  };

  const task = await manualTaskFromModal(payload, "user-1");

  assert.equal(task.kind, "tailor");
  assert.equal(task.input.job_snapshot.title, "AI/ML Support Analyst");
  assert.equal(task.input.job_snapshot.company, "KAUST");
  assert.equal(task.input.pasted_jd.replace(/\s+/g, " ").trim(), FULL_JD.replace(/\s+/g, " ").trim());
  assert.match(task.jobKey, /^manual:[0-9a-f]{32}$/);
});

test("a paste that is not a full posting is refused before a workflow is spent", async () => {
  const payload = {
    type: 5,
    data: {
      custom_id: "tailor-jd-paste",
      components: [
        { type: 1, components: [{ custom_id: "title", value: "Data Engineer" }] },
        { type: 1, components: [{ custom_id: "company", value: "Acme" }] },
        { type: 1, components: [{ custom_id: "jd_1", value: "Python role in Riyadh." }] },
      ],
    },
  };

  await assert.rejects(manualTaskFromModal(payload, "user-1"), /full job description/i);
});

test("a link requires target metadata before a workflow is spent", async () => {
  const payload = { data: { name: "tailor-jd", options: [{ name: "url", type: 3, value: "https://jobs.example.test/42" }] } };

  await assert.rejects(manualTaskFromCommand(payload, "user-1"), /title and company/i);
});

test("a link task keeps the supplied target metadata", async () => {
  const payload = { data: { name: "tailor-jd", options: [
    { name: "title", type: 3, value: "AI/ML Support Analyst" },
    { name: "company", type: 3, value: "KAUST" },
    { name: "url", type: 3, value: "https://jobs.example.test/42" },
  ] } };

  const task = await manualTaskFromCommand(payload, "user-1");

  assert.equal(task.input.url, "https://jobs.example.test/42");
  assert.equal(task.input.job_snapshot.title, "AI/ML Support Analyst");
  assert.equal(task.input.job_snapshot.company, "KAUST");
  assert.deepEqual(task.input.images, []);
  assert.equal(task.input.job_snapshot.jd.status, "missing");
});

test("a screenshot alone is enough, and its URL is what the runner reads", async () => {
  const payload = {
    data: {
      name: "tailor-jd",
      options: [
        { name: "title", type: 3, value: "ML Engineer" },
        { name: "company", type: 3, value: "Example Labs" },
        { name: "image", type: 11, value: "att-1" },
      ],
      resolved: { attachments: { "att-1": { url: "https://cdn.discordapp.test/a.png", filename: "a.png", content_type: "image/png", size: 900 } } },
    },
  };

  const task = await manualTaskFromCommand(payload, "user-1");

  assert.equal(task.input.job_snapshot.title, "ML Engineer");
  assert.equal(task.input.job_snapshot.company, "Example Labs");
  assert.deepEqual(task.input.images, ["https://cdn.discordapp.test/a.png"]);
});

test("neither a link nor a screenshot asks for the text instead", async () => {
  const payload = { data: { name: "tailor-jd", options: [] } };

  assert.equal(await manualTaskFromCommand(payload, "user-1"), null);
});

test("a manual snapshot claims no score, tier or link evidence it does not have", async () => {
  const snapshot = await manualSnapshot({ title: "AI/ML Support Analyst", company: "KAUST", url: "" });

  assert.equal(snapshot.score, 0);
  assert.equal(snapshot.ranking_verified, false);
  assert.equal(snapshot.link_status, "unknown");
  assert.equal(snapshot.source, "manual");
  assert.equal(snapshot.jd.status, "missing");
});
