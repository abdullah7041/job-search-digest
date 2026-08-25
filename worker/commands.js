// The command set this application registers with Discord. This file is the
// source of truth: `register-commands.mjs` PUTs exactly what is here, so a
// command absent from this array is deleted from Discord on the next run.
//
// That cuts both ways, and did. /tailor-jd was registered against an earlier
// build, then removed from this file without the registration being re-run, so
// it stayed in Discord's command list with nothing behind it: every use
// answered "Unsupported command." while an error message elsewhere in the
// Worker still told the user to reach for it. Run `npm run register` whenever
// this array changes.
//
// Types, from Discord's application-command reference:
//   command type 1 = CHAT_INPUT (a slash command)
//   option  type 3  = STRING
//   option  type 11 = ATTACHMENT
// attachmentFrom() in src/index.js finds an upload by option name, so a command
// may declare at most one attachment option and must name it.
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
    description: "Tailor your CV to a job the digest did not deliver, or could not read.",
    options: [
      {
        name: "url",
        type: 3,
        description: "Link to the posting. I read the description from it myself.",
        required: false,
      },
      {
        name: "image",
        type: 11,
        description: "A screenshot of the job description, if the link is behind a login.",
        required: false,
      },
    ],
  },
];

// Every name here must have a matching dispatch branch in handleInteraction.
export const COMMAND_NAMES = COMMANDS.map((command) => command.name);
