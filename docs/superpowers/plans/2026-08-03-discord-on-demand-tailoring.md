# Discord On-Demand CV Tailoring Implementation Plan

> **For agentic workers:** Execute inline in an isolated worktree; keep every behavior test-first.

**Goal:** Make Discord job cards trigger private, fact-checked, job-specific PDF tailoring from one confirmed master CV.

**Architecture:** A Cloudflare Worker receives signed Discord interactions and coordinates short-lived task state in D1. GitHub Actions keeps job discovery and executes PDF extraction, Gemini tailoring, verification, and rendering. The private repository stores the normalized CV and immutable job snapshots only.

**Tech stack:** Python 3.12, Gemini API, Cloudflare Worker/D1, Discord Interactions, GitHub Actions, Playwright Chromium.

## Global Constraints

- `/resume` is the sole master-CV upload trigger; plain Discord attachments remain inert.
- Only the configured Discord guild, channel, and user may invoke actions.
- Original uploaded PDFs are discarded after extraction; `resume.md` is updated only after confirmation.
- No PDF is generated without a full JD. Unsupported requirements stay in the report as gaps.
- Generated CVs must be English, ATS-first, A4, and at most two pages.
- The daily digest posts one application-owned card per archived job and never auto-tailors.
- Tailored outputs are ephemeral Discord responses and short-retention workflow artifacts, not committed resume files.

## Tasks

1. Add schema-validated job snapshots and application-owned Discord card payloads to the daily pipeline; retain the plain webhook fallback.
2. Add confirmed resume ingestion, fact extraction, and preview/confirmation artifacts.
3. Add structured JD-to-CV tailoring, source-reference validation, ATS HTML rendering, PDF verification, and concise reports.
4. Add the Worker/D1 interaction coordinator, command registration, signed callbacks, task expiry, and idempotency.
5. Add dedicated interactive workflows, fixture integration tests, documentation, deployment configuration, and a live Discord smoke test.
