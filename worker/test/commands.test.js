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

test("a file-carrying command declares at most one named attachment option", () => {
  // attachmentFrom() in src/index.js locates the upload by option name, so a
  // second attachment option on one command would be unreachable. It returns
  // null rather than throwing when the option is absent, which is what lets
  // /tailor-jd take a screenshot without demanding one.
  for (const command of COMMANDS) {
    const attachments = (command.options || []).filter((option) => option.type === 11);
    assert.ok(attachments.length <= 1, `${command.name} declares more than one attachment option`);
    for (const attachment of attachments) {
      assert.match(attachment.name, /^[-_a-z0-9]{1,32}$/, `${command.name} attachment needs a name`);
      assert.equal(typeof attachment.required, "boolean");
    }
  }
});

test("command names are unique", () => {
  assert.equal(new Set(COMMAND_NAMES).size, COMMAND_NAMES.length);
});

test("registers exactly the commands the worker can serve", () => {
  // /tailor-jd was registered against an earlier build and then dropped from
  // this array without register-commands.mjs being re-run, so it survived in
  // Discord with no branch behind it and answered "Unsupported command." for
  // every use. Pinning the set here makes removing one a deliberate edit.
  assert.deepEqual(COMMAND_NAMES, ["resume", "tailor-jd"]);
  assert.equal(COMMANDS[0].options[0].type, 11);
});

test("/tailor-jd asks for target metadata with an optional link or screenshot", () => {
  // Both absent is the case that opens the paste modal, so neither option can
  // be required or the user could never reach it.
  const tailor = COMMANDS.find((command) => command.name === "tailor-jd");
  const byName = Object.fromEntries((tailor.options || []).map((option) => [option.name, option]));
  assert.equal(byName.title.type, 3);
  assert.equal(byName.title.required, false);
  assert.equal(byName.company.type, 3);
  assert.equal(byName.company.required, false);
  assert.equal(byName.url.type, 3);
  assert.equal(byName.url.required, false);
  assert.equal(byName.image.type, 11);
  assert.equal(byName.image.required, false);
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
