import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import test from "node:test";

import worker, {
  authorizeInteraction,
  buttonTask,
  callbackTaskStatus,
  claimInternalNonce,
  claimTaskResult,
  postCard,
  releaseTaskResult,
  tailoringInputFromSnapshotRow,
  taskAccessStatus,
  taskExpiryMilliseconds,
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
  const result = buttonTask({ data: { custom_id: "tailor:greenhouse:provider-42" }, member: { user: { id: "user-1" } } });
  assert.deepEqual(result, { kind: "tailor", jobKey: "greenhouse:provider-42", userId: "user-1" });
});

test("rejects malformed card button identifiers", () => {
  assert.throws(() => buttonTask({ data: { custom_id: "tailor:" }, member: { user: { id: "user-1" } } }), /invalid/i);
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
