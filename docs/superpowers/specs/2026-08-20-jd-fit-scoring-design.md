# Job-fit scoring on real job descriptions

**Status:** Implemented in `e4faf9a` on `claude/resume-tailoring-job-fit-7e8k11`, awaiting user review

## Goal

Make the daily fit score reflect what a posting asks for, so a score of 70 means
the job description supports 70. The trigger was a Pasqal "Senior Quantum ML
Engineer" card scored 70 that did not read like a 70, and the concern that a
wrong score also produces a wrongly aimed tailored CV.

## The problem, from the archive

Four defects, three provable from the 140 snapshots already committed in `jobs/`.

### 1. The scorer read 900 characters of recruiting copy

`JD_RANK_CHARS` was 900 and `gemini_rank()` sent the first 900 characters of
each description. For the QuantumBlack posting those 900 characters are "Who
You'll Work With", "high performance/high reward culture", "resilience",
"mentorship", and a benefits list. The section describing the job, "Your
Impact", starts at character 1931.

The model's recorded reasons say things like "emphasizes mentorship and
leadership growth rather than pure IC contribution". That is an accurate summary
of the text it was given. Of the 34 distinct archived descriptions longer than
1500 characters, 10 put their first requirements heading past character 900 and
8 carry no heading this module recognises.

### 2. Aggregator teasers were graded as complete descriptions

`jd_completeness_failures()` passes any text with 400 characters, 60 words, one
responsibility marker, and one requirement marker. A JSearch marketing paragraph
clears all 4. The Pasqal description is 497 characters, was stamped
`status: captured`, was scored, and had a live Tailor CV button. 19 of the 140
archived snapshots are these, all between 447 and 512 characters.

### 3. The same description scored differently every day

QuantumBlack "Software Engineer II", byte-identical 5417-character description,
scored 40, 40, 40, 45, 50, 50 and 75 across the 7 runs the model scored it, a
spread of 35. Two further runs fell back to keyword scoring and reported 55.
9 postings in the archive have a spread of 15 points or more on an unchanged
description. The worst is an ETL Engineer role scored 30, 80, 85 and 85.

Contributing causes: all 25 jobs went into one call, so a score depended on
which other jobs were new that day; `temperature: 0.2` was sent with no `seed`;
and the excerpt problem above left the model scoring text that says little.

### 4. A failed model call was delivered as a score

When the call failed, `apply_ranking()` fell back to `keyword_score()`, which is
40 plus 15 per keyword in the title. The card then read `fit 55 · matches your
target titles and skills`, formatted identically to a model score. 2 cards on
2026-08-19 were this.

## Options considered

### What the ranker reads

| Option | Verdict |
| --- | --- |
| Raise the character cap | Rejected. A longer prefix is still a prefix, and it spends the token budget on benefits copy. Postings vary in how much they run before the role. |
| Send the whole description | Rejected. 25 jobs at 6000 characters is roughly 42k input tokens per run, and the model weighs perks alongside requirements. |
| Window from the requirements heading | **Chosen.** `jd_signal()` finds the first heading that introduces the work and takes `JD_RANK_CHARS` (now 2000) from there. |

Two corrections during implementation:

- A plain substring search put the QuantumBlack window at "...your location,
  experience, and skills), we provide a comprehensive benefits package", because
  "skills" appears inside that sentence. `SIGNAL_RE` now requires the heading to
  sit at the start of the text, the start of a line, or after sentence-ending
  punctuation or a bullet.
- Windowing a short description from a late heading threw away its opening. The
  Pasqal teaser's only heading is near its end, so the window returned 199 of
  497 characters. A description that fits the window is now returned whole.

### Grading a teaser

| Option | Verdict |
| --- | --- |
| Length threshold alone | Rejected. The archive holds structured postings at 749 and 1300 characters that read as real postings. |
| Fetch the real page when the text looks thin | Rejected by the user, and jobleads-class sites block the runner. |
| Heading structure, with length deciding how much structure is required | **Chosen.** Teaser if under 1200 characters and carrying fewer than 2 signal headings. |

Validated across all 140 archived snapshots: 19 teasers, every one between 447
and 512 characters and every one from JSearch. The shortest description graded
`full` is 1300 characters. No overlap.

Schema handling: `schema_version` stays at 1 and `jd.grade` is additive.
`write_snapshots()` raises on a version mismatch when appending to a same-day
archive, so a version bump would break the first run after deploy.
`snapshot_grade()` derives a grade from the text for the 11 archive files
written before the field existed.

### Making instability visible

| Option | Verdict |
| --- | --- |
| Score in smaller fixed batches | Rejected. It multiplies calls, and the excerpt was the larger cause. |
| Per-job score history in a new state file | Rejected. `workflow_guard.py` allows exactly `seen.json`, `stats.json` and `jobs/<date>.json` through to the state commit. That allowlist is narrow on purpose. |
| Seed, deterministic listing order, and read history from the archives | **Chosen.** |

`score_instability()` groups on the JD SHA-256 rather than `job_key`, because
JSearch mints a fresh opaque id for the same posting on every request, so
`job_key` never repeats across days. Keyword-fallback scores are excluded: they
are constant for a given title and would read as perfect stability on exactly
the days the ranker was down.

The result is reported, not corrected. The fix for an unstable ranker is a
better prompt or a better model, and this is how the next change to either gets
measured.

### Model selection

Ranking moves from `gemini-3.1-flash-lite` on the Gemini API to
`google/gemini-3.7-flash` on OpenRouter. Read from the OpenRouter catalogue on
2026-08-20: $0.375/M input, $1.875/M output, with `discount: 0.75` on the Vertex
endpoints. That is half the price of `gemini-3.6-flash` at a higher Artificial
Analysis intelligence index (56 against 51.6). At 25 jobs a day the run costs
under $0.25 a month, and the second API key retires.

Tailoring stays on `openai/gpt-5.6-luna` at $0.20/M input. It is cheaper than
3.7 Flash and the evidence check is passing. `google/gemini-3.7-flash` replaces
`google/gemini-3.6-flash` as first fallback, since it is cheaper and stronger.

One routing correction applies to both paths. `require_parameters: true` drops
any provider that does not support every parameter the request names. The Vertex
endpoints for 3.7 Flash list `seed`, `response_format` and `reasoning_effort`,
and do not list `temperature`. Sending `temperature: 0.25` therefore routed to
the Google AI Studio endpoints at `discount: 0.5` instead of the Vertex ones at
`0.75`. No request sends `temperature` now; `seed: 7` carries the determinism.

### Tailored CV filename

`tailored-quantumblack-ai-by-mckinsey-software-engineer-ii-.pdf` becomes
`Abdullah_Bin_Ahmed_Software_Engineer_II_QuantumBlack_AI_By.pdf`. The candidate
name is read from `document["name"]` so the filename follows the master resume.

Title-casing every word turned `ML` into `Ml` and `II` into `Ii`, so a job title
keeps the case the employer wrote and only an all-lowercase word is capitalised.
The name is title-cased, because a resume heading is usually shouted.
Truncation is by whole words, which also removes the trailing separator that
`_slug()` produced by cutting at 40 characters before stripping.

## Scope

Included: `digest.py`, `job_archive.py`, `interactive_runner.py`,
`resume_tailor.py`, a new `openrouter.py` holding the transport both callers
share, `profile.json`, `.github/workflows/digest.yml`, `README.md`,
`PROFILE_REFRESH_PROMPT.md`, and the 2 Python test files.

Excluded:

- `resume.md`. The attached master `.docx` is line-for-line identical to the
  tracked file; only layout differs.
- Closing resume content gaps against target roles. The user scoped this change
  to pipeline defects.
- Changing the tailoring model.
- The JSearch id problem below.

## Invariants

- A teaser stays tailorable. It is capped at 69, labelled on the card, and
  carries a warning in the Discord message. `tailored` stays true, because the
  tailoring itself did not degrade.
- `run_task()` blocks exactly what it blocked before. Teasers already passed the
  completeness gate, so nothing that worked stops working.
- Tailoring keeps `data_collection: deny`. That prompt carries the master
  resume including contact details.
- Ranking failure still falls back to `keyword_score()` and never fails the run.
- Archives written before `jd.grade` still load, still merge on a same-day
  append, and are graded from their own text.

## Verification

Ran and passing:

1. 120 Python tests and 33 Worker tests, plus pinned actionlint on the
   workflows. Baseline before the change was 95 Python tests.
2. Both flagged postings replayed through the new ranker offline. The
   QuantumBlack window now opens at "Your Impact" and reaches the stack list
   ("JavaScript, TypeScript, React.js, Python, SQL, AWS, Azure, GCP"), which the
   900-character cut never included. Pasqal is graded `teaser` and its full 497
   characters are sent.
3. Both rendered end to end through `run_task()` against the real archived
   snapshots with `jd.grade` stripped, confirming filenames, 1 page each, and
   the teaser warning on Pasqal only.
4. Same-day append against the existing `jobs/2026-08-20.json`: 13 jobs become
   14, pre-existing snapshots byte-identical, `schema_version` still 1.
5. Grading swept over all 140 archived snapshots: 85 full, 36 incomplete, 19
   teaser, with no overlap in length between the teaser and full sets.

Not run: the live model call. No `OPENROUTER_API_KEY` was available in the
implementing session, so the before-and-after *scores* for the 2 flagged
postings are unverified. Everything deterministic about the call was checked.
Run `OPENROUTER_API_KEY=... python digest.py` to close this.

## Success criteria

- A "why it fits" line cites something from the role rather than the culture
  section.
- Teaser-backed postings never appear under "Strong matches".
- `score_instability()` trends toward an empty list over the next 2 weeks.
- No card shows a bare `fit N` when the ranker was unavailable.

## Known open item, out of scope

JSearch mints a fresh opaque job id for the same posting on every request, and
`seen.json` deduplicates on that id. The QuantumBlack posting was therefore
delivered on 7 separate days, and a JAL frontend role on 9.

Addressed separately in `2026-08-21-stable-job-identity-design.md`.
