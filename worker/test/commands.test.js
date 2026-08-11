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
