# AI coding agent for the job digest — plan (2026-07-31)

Grounded in the repo as it stands today: `digest.py` (fetch → filter → rank → tailor → deliver), `resume.md` as tailoring source, `test_digest.py` (16 offline tests), `tailored/` committed back by `digest.yml`, state in `seen.json`/`stats.json`.

## What the agent is for

The digest breaks in exactly one way: silently drifting out of sync with the world. ATS APIs change shape, companies move boards, the Gemini model string dies, profile.json goes stale, JSearch quota shifts. Today you notice via a WARN in a log you don't read, or a drift warning in Discord. The agent's job is to notice, diagnose, and propose the fix as a reviewable PR. You stay the merge gate.

Explicitly out of scope: auto-applying to jobs, scraping sources the README already ruled out, touching `resume.md` content (that's your voice), any multi-agent framework, any new runtime dependency.

## Recommendation: Claude Code GitHub Action, issue-driven, PR-only

One workflow file (`.github/workflows/agent.yml`) using `anthropics/claude-code-action`, with `ANTHROPIC_API_KEY` as a repo secret. Three triggers:

1. **Digest failure** — `workflow_run` on `job-digest` concluding `failure`. The agent reads the failed log, reproduces offline via `python test_digest.py`, and opens a PR with the fix. This covers the real failure class: an ATS payload change that a fetcher no longer parses.
2. **Weekly health check** — Monday cron. The agent reads `stats.json` (fetched/matched/new/avg-score trend), the last 7 run logs' WARN lines, and `seen.json` growth; posts one issue titled "Digest health — week of X" with either "all healthy, no action" or specific findings plus a PR when something needs code.
3. **@claude in issues** — you file "add company X" or "scores feel off this week", the agent runs `--discover`, edits `COMPANIES` or the rubric, opens the PR.

Why this and not the alternatives: the repo already lives on Actions, so the agent runs where the evidence (logs, stats, state) already is, with no new infrastructure. A Cowork scheduled task could do the weekly check but can't catch failures at the moment they happen and can't reach the Actions logs as directly. A standing local agent is a laptop dependency this project was built to avoid.

## Guardrails (non-negotiable, encoded in the workflow + CLAUDE.md)

- **PRs only.** The agent never pushes to `main`. Branch protection optional but recommended.
- **Tests are the gate.** A PR must show `python test_digest.py` passing, and any fetcher change must include a live verification note (real API called, real field seen) — the same standard this repo was built with. New failure modes get a new test in the same PR.
- **No new dependencies** without an explicit issue from you approving it. `requirements.txt` is one line today; the agent keeps it that way.
- **Scope fence:** the agent may edit `digest.py`, `test_digest.py`, `COMPANIES`, workflow files, and docs. It may not edit `resume.md`, `profile.json` (yours via PROFILE_REFRESH_PROMPT.md), `seen.json`, or `tailored/`.
- **Budget caps:** `max_turns` limit in the action config; concurrency group so runs never stack; weekly cron + failure trigger means near-zero spend in a healthy week.

## What could break, honestly

- **The agent fixes the wrong thing confidently.** An ATS 403 from a runner-IP block looks identical to a dead token; the "reproduce offline first, verify live before claiming" rule in CLAUDE.md is the mitigation, and your PR review is the backstop.
- **PR noise.** A flaky ATS could trigger failure-PRs repeatedly. Mitigation: the failure trigger only fires on the *workflow* failing (digest.py exits non-zero on Discord failure only — fetchers are fail-soft), so most flakiness never wakes the agent. Weekly check is capped at one issue.
- **seen.json race:** the agent's PR merge plus the daily state commit can conflict. Mitigation: agent never touches state files; the existing concurrency group already serializes digest runs.
- **Model/API churn on the agent itself:** the action version and model string need occasional bumping — same class of problem it exists to fix, one level up. Pin the action to a major version.

## Cost to maintain

- **Money:** Anthropic API for agent runs — a healthy week is one health-check run; a bad week maybe 3–4 runs. At typical Claude Action usage this is single-digit dollars/month, capped by `max_turns`. GitHub Actions minutes stay free-tier (digest is ~2 min/day; agent runs a few min/week). Watch the first month's bill before trusting the estimate.
- **Time:** reviewing the weekly issue (~2 min) and the occasional PR (~10 min, same bar as reviewing any AI code: read it, run tests, check the live-verification note).

## What you'd babysit

1. Every PR before merge — the agent proposes, never disposes.
2. `resume.md` sync with the Cowork master after each resume update (agent can *remind* in the weekly issue if the file is stale vs. a marker date, but the edit is yours).
3. `profile.json` refresh monthly (existing nudge) — the agent flags drift, you decide the profile.
4. API keys: RapidAPI quota (200/month free tier — 3 queries/day is ~93), Gemini key validity, ANTHROPIC_API_KEY billing.
5. The repo's visibility decision (public today; tailored resumes carry your contact info).

## Build order (when you say go)

1. `agent.yml` with the failure trigger only + repo `CLAUDE.md` carrying the guardrails above. Smallest useful agent; run it a week or two.
2. Add the weekly health-check cron once you trust the failure path.
3. Add `@claude` issue handling last — it's the widest door, so it goes in when the narrow ones have earned trust.

Each phase is one workflow file edit; nothing in `digest.py` changes for any of this.
