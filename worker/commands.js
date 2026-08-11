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
];

// Every name here must have a matching dispatch branch in handleInteraction.
export const COMMAND_NAMES = COMMANDS.map((command) => command.name);
