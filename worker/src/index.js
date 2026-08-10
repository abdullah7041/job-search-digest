const encoder = new TextEncoder();

function interactionUserId(payload) {
  return payload.member?.user?.id || payload.user?.id || "";
}

export function authorizeInteraction(payload, env) {
  if (payload.guild_id !== env.DISCORD_GUILD_ID || payload.channel_id !== env.DISCORD_CHANNEL_ID || interactionUserId(payload) !== env.DISCORD_USER_ID) {
    return { ok: false, message: "This bot only accepts requests from the configured private job-search channel." };
  }
  return { ok: true, userId: interactionUserId(payload) };
}

export function buttonTask(payload) {
  const customId = payload.data?.custom_id || "";
  const match = /^tailor:([0-9a-f]{32})$/i.exec(customId);
  if (!match || !match[1]) throw new Error("Invalid tailoring button.");
  return { kind: "tailor", jobRef: match[1].toLowerCase(), userId: interactionUserId(payload) };
}

export function taskExpiryMilliseconds(kind) {
  return kind === "resume" ? 24 * 60 * 60 * 1000 : 14 * 60 * 1000;
}

export function taskAccessStatus(task, now = new Date()) {
  if (!task) return 404;
  const expiresAt = new Date(task.expires_at);
  if (!Number.isFinite(expiresAt.getTime()) || expiresAt <= now) return 410;
  if (!["received", "dispatched", "running", "delivery_retryable"].includes(task.status)) return 409;
  return 200;
}

async function sha256Hex(value) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function buttonReferenceForJobKey(jobKey) {
  return (await sha256Hex(jobKey)).slice(0, 32);
}

function jdIsComplete(value) {
  const normalized = (value || "").trim().replace(/\s+/g, " ");
  const words = normalized.match(/\b\w+\b/g) || [];
  const lowered = normalized.toLowerCase();
  const hasResponsibilities = ["responsibil", "you will", "duties", "about the role"].some((marker) => lowered.includes(marker));
  const hasRequirements = ["requirement", "qualification", "experience", "skills"].some((marker) => lowered.includes(marker));
  return normalized.length >= 400 && words.length >= 60 && hasResponsibilities && hasRequirements;
}

export async function tailoringInputFromSnapshotRow(row, expectedJobKey) {
  if (!row?.snapshot_json) throw new Error("The immutable job snapshot is unavailable.");
  if (row.job_key !== expectedJobKey) throw new Error("The job snapshot reference does not match this button.");
  const snapshot = JSON.parse(row.snapshot_json);
  if (snapshot.job_key !== expectedJobKey) throw new Error("The stored job snapshot does not match this button.");
  const jd = snapshot.jd || {};
  if (jd.status !== "captured" || !jd.text?.trim()) {
    throw new Error("A full job description is required; use /tailor-jd to provide it.");
  }
  if (!jd.sha256 || await sha256Hex(jd.text) !== jd.sha256) {
    throw new Error("The stored job description no longer matches its digest.");
  }
  if (!jdIsComplete(jd.text)) throw new Error("The snapshot does not contain a complete job description.");
  return { job_snapshot: snapshot };
}

export function callbackTaskStatus(result) {
  if (result?.status !== "completed" && result?.status !== "failed") {
    throw new Error("The task result has an invalid status.");
  }
  return result.status;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function background(ctx, promise) {
  // Tests and any runtime without an execution context fall back to awaiting
  // inline; production Workers always provide ctx.waitUntil.
  if (typeof ctx?.waitUntil === "function") {
    ctx.waitUntil(promise);
    return promise;
  }
  return promise;
}

function base64(bytes) {
  return btoa(String.fromCharCode(...bytes));
}

function fromBase64(value) {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

function fromHex(value) {
  if (!/^[0-9a-f]+$/i.test(value) || value.length % 2 !== 0) throw new Error("invalid hex");
  return Uint8Array.from(value.match(/../g), (pair) => Number.parseInt(pair, 16));
}

function constantTimeEqual(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

async function encryptionKey(secret) {
  const hash = await crypto.subtle.digest("SHA-256", encoder.encode(secret));
  return crypto.subtle.importKey("raw", hash, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function encrypt(value, secret) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await encryptionKey(secret);
  const cipher = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, encoder.encode(value));
  return `${base64(iv)}.${base64(new Uint8Array(cipher))}`;
}

async function decrypt(value, secret) {
  const [iv, cipher] = value.split(".");
  const key = await encryptionKey(secret);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromBase64(iv) }, key, fromBase64(cipher));
  return new TextDecoder().decode(plain);
}

async function discordSignatureIsValid(request, raw, env) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  const key = await crypto.subtle.importKey("raw", fromBase64(env.DISCORD_PUBLIC_KEY_BASE64), { name: "Ed25519" }, false, ["verify"]);
  try {
    return crypto.subtle.verify("Ed25519", key, fromHex(signature), encoder.encode(timestamp + raw));
  } catch {
    return false;
  }
}

async function internalRequestIsValid(request, raw, env) {
  const supplied = request.headers.get("X-Job-Digest-Signature") || "";
  const timestamp = request.headers.get("X-Job-Digest-Timestamp") || "";
  const nonce = request.headers.get("X-Job-Digest-Nonce") || "";
  if (!/^\d+$/.test(timestamp) || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  if (!/^[a-zA-Z0-9-]{16,64}$/.test(nonce)) return false;
  const path = new URL(request.url).pathname;
  const canonical = `${timestamp}\n${nonce}\n${request.method}\n${path}\n${raw}`;
  const key = await crypto.subtle.importKey("raw", encoder.encode(env.CALLBACK_HMAC_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const expected = base64(new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(canonical))));
  return constantTimeEqual(supplied, expected);
}

export async function claimInternalNonce(db, nonce) {
  await db.prepare("DELETE FROM internal_nonces WHERE expires_at <= datetime('now')").bind().run();
  const claim = await db.prepare(
    "INSERT OR IGNORE INTO internal_nonces (nonce, expires_at) VALUES (?, datetime('now', '+10 minutes'))"
  ).bind(nonce).run();
  return claim.meta?.changes === 1;
}

export async function claimTaskResult(db, taskId) {
  const leaseUntil = Math.floor(Date.now() / 1000) + 2 * 60;
  const claim = await db.prepare(
    "UPDATE tasks SET status = 'delivering', delivery_lease_until = ?, delivery_error = NULL WHERE id = ? AND (" +
    "status IN ('received', 'dispatched', 'running', 'delivery_retryable') OR " +
    "(status = 'delivering' AND delivery_lease_until <= unixepoch()))"
  ).bind(leaseUntil, taskId).run();
  return claim.meta?.changes === 1;
}

export async function releaseTaskResult(db, taskId, error) {
  const release = await db.prepare(
    "UPDATE tasks SET status = 'delivery_retryable', delivery_lease_until = NULL, delivery_error = ? " +
    "WHERE id = ? AND status = 'delivering'"
  ).bind(String(error).slice(0, 1000), taskId).run();
  return release.meta?.changes === 1;
}

async function createTask(env, task, interactionToken) {
  const id = crypto.randomUUID();
  const expiresAt = new Date(Date.now() + taskExpiryMilliseconds(task.kind)).toISOString();
  const encryptedToken = await encrypt(interactionToken, env.TASK_ENCRYPTION_KEY);
  await env.DB.prepare(
    "INSERT INTO tasks (id, kind, user_id, job_key, input_json, interaction_token, status, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'received', ?)"
  ).bind(id, task.kind, task.userId, task.jobKey || null, JSON.stringify(task.input || {}), encryptedToken, expiresAt).run();
  return id;
}

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

function attachmentFrom(payload) {
  const option = payload.data?.options?.find((item) => item.type === 11);
  const attachment = option && payload.data?.resolved?.attachments?.[option.value];
  if (!attachment?.url) throw new Error("Attach a PDF to this command.");
  return { url: attachment.url, filename: attachment.filename || "resume.pdf", contentType: attachment.content_type || "", size: attachment.size || 0 };
}

export async function postCard(env, card) {
  const leaseUntil = Math.floor(Date.now() / 1000) + 2 * 60;
  const claim = await env.DB.prepare(
    "INSERT INTO delivery_claims (job_key, state, lease_until, claimed_at) VALUES (?, 'posting', ?, datetime('now')) " +
    "ON CONFLICT(job_key) DO UPDATE SET state = 'posting', lease_until = excluded.lease_until, last_error = NULL " +
    "WHERE delivery_claims.state = 'retryable' OR (delivery_claims.state = 'posting' AND delivery_claims.lease_until <= unixepoch())"
  ).bind(card.job_key, leaseUntil).run();
  if (claim.meta?.changes !== 1) {
    const current = await env.DB.prepare("SELECT state FROM delivery_claims WHERE job_key = ?").bind(card.job_key).first();
    return { status: current?.state === "posted" ? "existing" : "in_progress", job_key: card.job_key };
  }

  let response;
  const messageNonce = (await sha256Hex(card.job_key)).slice(0, 24);
  const messagePayload = { ...card.payload, nonce: messageNonce, enforce_nonce: true };
  try {
    response = await fetch(`https://discord.com/api/v10/channels/${env.DISCORD_CHANNEL_ID}/messages`, {
      method: "POST",
      headers: { "authorization": `Bot ${env.DISCORD_BOT_TOKEN}`, "content-type": "application/json" },
      body: JSON.stringify(messagePayload),
    });
  } catch (error) {
    await env.DB.prepare("UPDATE delivery_claims SET state = 'retryable', lease_until = NULL, last_error = ? WHERE job_key = ?")
      .bind(`Discord outcome unknown: ${error.message}`, card.job_key).run();
    return { status: "retryable", job_key: card.job_key, error: `Discord outcome unknown: ${error.message}` };
  }
  if (!response.ok) {
    await env.DB.prepare("UPDATE delivery_claims SET state = 'retryable', lease_until = NULL, last_error = ? WHERE job_key = ?")
      .bind(`Discord HTTP ${response.status}`, card.job_key).run();
    throw new Error(`Discord card post failed: ${response.status}`);
  }
  const message = await response.json();
  try {
    const statements = [
      env.DB.prepare("INSERT OR REPLACE INTO deliveries (job_key, message_id, posted_at) VALUES (?, ?, datetime('now'))").bind(card.job_key, message.id),
      env.DB.prepare("UPDATE delivery_claims SET state = 'posted', message_id = ? WHERE job_key = ?").bind(message.id, card.job_key),
    ];
    if (card.snapshot) {
      statements.push(
        env.DB.prepare("INSERT INTO job_snapshots (message_id, job_key, snapshot_json, captured_at) VALUES (?, ?, ?, ?)")
          .bind(message.id, card.job_key, JSON.stringify(card.snapshot), card.snapshot.captured_at || new Date().toISOString())
      );
    }
    await env.DB.batch(statements);
  } catch (error) {
    return { status: "in_progress", job_key: card.job_key, error: `Card posted but persistence is pending: ${error.message}` };
  }
  return { status: "posted", job_key: card.job_key };
}

async function deliverResult(env, task, result) {
  const token = await decrypt(task.interaction_token, env.TASK_ENCRYPTION_KEY);
  const endpoint = `https://discord.com/api/v10/webhooks/${env.DISCORD_APPLICATION_ID}/${token}/messages/@original`;
  let response;
  if (result.pdf_base64) {
    const form = new FormData();
    form.set("payload_json", JSON.stringify({ content: result.content, flags: 64 }));
    form.set("files[0]", new Blob([fromBase64(result.pdf_base64)], { type: "application/pdf" }), result.filename || "tailored-resume.pdf");
    response = await fetch(endpoint, { method: "PATCH", body: form });
  } else {
    response = await fetch(endpoint, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ content: result.content, flags: 64 }) });
  }
  if (!response.ok) throw new Error(`Discord result delivery failed: ${response.status}`);
}

async function handleInteraction(request, env, ctx) {
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
    // Discord discards the interaction if it is not acknowledged within 3
    // seconds. The GitHub dispatch is an external round-trip, so it runs after
    // the acknowledgement; a failure is reported back through the same message.
    const dispatch = dispatchWorkflow(env, taskId).catch((error) =>
      reportDispatchFailure(env, payload.token, `Could not start that task: ${error.message}`)
    );
    background(ctx, dispatch);
    return json({ type: 5, data: { content: task.kind === "resume" ? "Reading your CV privately…" : "Preparing your tailored CV privately…", flags: 64 } });
  } catch (error) {
    return json({ type: 4, data: { content: `Could not start that task: ${error.message}`, flags: 64 } });
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/interactions" && request.method === "POST") return handleInteraction(request, env, ctx);
    if (!url.pathname.startsWith("/internal/")) return new Response("not found", { status: 404 });
    const raw = await request.text();
    if (!(await internalRequestIsValid(request, raw, env))) return new Response("unauthorized", { status: 401 });
    if (!(await claimInternalNonce(env.DB, request.headers.get("X-Job-Digest-Nonce")))) return new Response("replayed request", { status: 409 });
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
    const input = /^\/internal\/tasks\/([^/]+)\/input$/.exec(url.pathname);
    if (input && request.method === "GET") {
      const task = await env.DB.prepare("SELECT input_json, status, expires_at FROM tasks WHERE id = ?").bind(input[1]).first();
      if (!task) return new Response("not found", { status: 404 });
      const accessStatus = taskAccessStatus(task);
      if (accessStatus !== 200) return new Response(accessStatus === 410 ? "expired" : "task is already terminal", { status: accessStatus });
      await env.DB.prepare("UPDATE tasks SET status = 'running' WHERE id = ? AND status IN ('received', 'dispatched')").bind(input[1]).run();
      return json({ input: JSON.parse(task.input_json), status: task.status, expires_at: task.expires_at });
    }
    const result = /^\/internal\/tasks\/([^/]+)\/result$/.exec(url.pathname);
    if (result && request.method === "POST") {
      const task = await env.DB.prepare("SELECT * FROM tasks WHERE id = ?").bind(result[1]).first();
      if (!task) return new Response("not found", { status: 404 });
      if (new Date(task.expires_at) <= new Date()) return new Response("expired", { status: 410 });
      const taskResult = JSON.parse(raw);
      const status = callbackTaskStatus(taskResult);
      if (!(await claimTaskResult(env.DB, result[1]))) return new Response("result already claimed", { status: 409 });
      try {
        await deliverResult(env, task, taskResult);
      } catch (error) {
        await releaseTaskResult(env.DB, result[1], error.message);
        return new Response("result delivery temporarily failed", { status: 502 });
      }
      await env.DB.prepare(
        "UPDATE tasks SET status = ?, delivery_lease_until = NULL, delivery_error = NULL WHERE id = ? AND status = 'delivering'"
      ).bind(status, result[1]).run();
      return json({ ok: true });
    }
    return new Response("not found", { status: 404 });
  },
};
