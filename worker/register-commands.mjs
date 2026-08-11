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
