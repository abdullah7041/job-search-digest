# Plan 017: Match a skill keyword at a word start, not anywhere in the title

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat ab0eb45..HEAD -- digest.py test_digest.py profile.json`
> If any changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: correctness (scoring fallback)
- **Planned at**: commit `ab0eb45`, 2026-09-06

## Why this matters

`keyword_score` tests each profile keyword with a plain substring `in`, so a
keyword fires on any title that merely contains its letters. `"api"` fires on
*Web Scr**api**ng* and *C**api**tal*; `"ml"` fires on *A**ML** Officer*; `"ai"`
fires a second time on *Gen**AI*** where `"genai"` has already scored it. Each
false hit is worth +15 on a 40-base score.

**This is a real defect, and it is small.** Measured against the 91 distinct
titles in `jobs/*.json` at `ab0eb45` (script below, reproducible): 6 titles
carry a spurious hit, out of 129 hits total. The blast radius is bounded twice
over — `keyword_score` runs only when `apply_ranking` (`digest.py:1401-1417`)
cannot get a valid model score, and `drop_below_floor` (`digest.py:1454-1470`)
never drops on an unverified score. So a spurious hit changes **ordering inside
a fallback digest**, never inclusion. That is why this is P3 and not P1.

It is worth fixing anyway because the fallback exists precisely for the days the
ranker is down, which are the days the ordering is all the reader has.

## Current state

`digest.py:1132-1135`:

```python
def keyword_score(job):
    title = job["title"].lower()
    hits = sum(1 for k in PROFILE["skill_keywords"] if k in title)
    return min(100, 40 + hits * 15)  # 40 base for passing filters, +15 per skill hit
```

Two keyword lists feed it and **both** are affected: `profile.json`'s
`skill_keywords`, and `DEFAULT_PROFILE["skill_keywords"]`
(`digest.py:139-143`), which `load_profile` falls back to when `profile.json`
is missing, corrupt, or incomplete (`digest.py:360-374`). The fix belongs in
the matcher, not in either list — editing a list only hides the class of bug
until the next keyword is added.

## The evidence, and why the obvious fix is wrong

The obvious fix — require a word boundary on both sides — **causes
regressions**. Measured on the same 91 titles:

| Matching rule | Spurious hits removed | Legitimate hits destroyed |
|---|---|---|
| substring (today) | — | — |
| **word start only** (`(?<![a-z0-9])kw`) | **6** | **0** |
| both boundaries (`(?<![a-z0-9])kw(?![a-z0-9])`) | 8 | **2** |

The two hits that only a trailing boundary destroys are both correct matches:

- `AI Engineer - Agentic` — `"agent"` is a true hit; **Agentic** is the job.
- `Full Stack Machine Learning Engineer (Datacentre AI Engineering)` —
  `"data"` in **Datacentre**.

English builds meaning by suffix (`agent`→`agentic`, `analyt`→`analytics`) and
noise by prefix (`scr`+`api`+`ng`, `A`+`ML`). **Anchor the start, leave the end
free.** That is the whole change.

The six hits the word-start rule removes, with the score before → after:

| Title | Keyword | Score |
|---|---|---|
| `Senior ML Scientist: GenAI, MLOps & Production` | `ai` | 85 → 70 |
| `GenAI/LLM Data Scientist & Engineer — Consultant` | `ai` | 100 → 85 |
| `AML Officer` | `ml` | 55 → 40 |
| `Senior Data Engineer: Web Scraping & Automation (Remote)` | `api` | 85 → 70 |
| `Senior Data Engineer - Web Scraping` | `api` | 70 → 55 |
| `Senior Product Manager (Capital)` | `api` | 55 → 40 |

Every one is a title the current rule over-scores. `AML Officer` — an
anti-money-laundering compliance role — is the clearest: it has nothing to do
with ML.

Reproduce before changing anything (this script is the evidence, keep it out of
the commit):

```python
import json, glob, re
kws = json.load(open("profile.json", encoding="utf-8"))["skill_keywords"]

def day(f):                      # older snapshots are a bare list, newer ones {"jobs": [...]}
    d = json.load(open(f, encoding="utf-8"))
    return d["jobs"] if isinstance(d, dict) else d

titles = {j["title"] for f in glob.glob("jobs/*.json") for j in day(f) if j.get("title")}
for t in sorted(titles):
    sub = {k for k in kws if k in t.lower()}
    left = {k for k in kws if re.search(r"(?<![a-z0-9])" + re.escape(k), t.lower())}
    if sub - left:
        print(f"{min(100, 40+15*len(sub))} -> {min(100, 40+15*len(left))}  {sorted(sub-left)}  {t}")
```

Expected: exactly the six rows in the table above.

## Tasks (TDD — write the failing test first, every time)

### Task 1 — pin today's behaviour so the change is visible

In `test_digest.py`, next to the existing `keyword_score` coverage
(`test_digest.py:640`), add a test asserting the **desired** rule on the three
real titles that expose it, using an explicit keyword list rather than the live
`PROFILE` (the profile is data and will keep changing):

- `"AML Officer"` scores the base 40 — `ml` must not fire.
- `"Senior Data Engineer - Web Scraping"` does not count `api`.
- `"AI Engineer - Agentic"` **does** count `agent` — this is the regression
  guard for the both-boundaries mistake, and it must fail if someone later
  adds a trailing `\b`.

Patch `PROFILE` (or parameterise the helper — see Task 2) rather than mutating
the module global permanently; the suite must stay order-independent.

**Verify**: `python -m unittest test_digest.py` — the new test fails on the
`AML`/`Scraping` assertions and passes on the `Agentic` one.

### Task 2 — make the matcher anchor at a word start

Replace the substring test with one compiled word-start pattern per keyword.
Compile once at import, not per job — `keyword_score` runs on every job of
every run:

```python
def _keyword_pattern(keyword):
    """A keyword matches at a word start, with the suffix left free.

    'agent' must still match 'Agentic' and 'data' must still match
    'Datacentre', so there is no trailing boundary. The leading one is what
    stops 'api' matching 'Scraping' and 'ml' matching 'AML'. Measured on the
    91 archived titles at ab0eb45: 6 spurious hits removed, 0 real hits lost.
    See plans/017.
    """
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword))
```

`re.escape` is required, not optional: `full-stack`, `ai/ml` and `full stack`
are already in the lists and contain regex metacharacters.

Keep `keyword_score`'s signature and its return contract
(`min(100, 40 + hits * 15)`) exactly as they are. Nothing else in the pipeline
changes.

**Verify**: `python -m unittest test_digest.py` — Task 1's test passes; every
pre-existing test still passes.

### Task 3 — prove the two keyword sources both benefit

Add one test that runs the matcher against `DEFAULT_PROFILE["skill_keywords"]`
(`digest.py:139-143`) and asserts `"AML Officer"` scores 40 there too. The
default list contains `ai`, `ml` and `api` as well; a fix that only helps
`profile.json` is half a fix.

**Verify**: `python -m unittest test_digest.py test_interactive.py
test_workflow_guard.py` — `OK`, and the Python count is the baseline + the
tests this plan adds (do not hardcode the baseline; read it from the run).

### Task 4 — record the rule where the next person will look

Add two lines to `PROFILE_REFRESH_PROMPT.md`'s `skill_keywords` bullet: a
keyword matches at a **word start** and the suffix is free, so `agent` covers
`agentic`, and a keyword that is a common letter-run inside other words
(`ai`, `ml`, `api`) is now safe to keep in the list.

**Verify**: `git diff PROFILE_REFRESH_PROMPT.md` shows only that bullet.

## Done criteria

- [ ] `keyword_score` matches each keyword at a word start; no trailing boundary.
- [ ] Patterns are compiled once, not per call.
- [ ] `AI Engineer - Agentic` still scores `agent` (explicit test).
- [ ] `AML Officer`, `Web Scraping`, `Capital` no longer take a false hit.
- [ ] Both `profile.json` and `DEFAULT_PROFILE` keyword lists are covered by tests.
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` → `OK`.
- [ ] `cd worker && npm test` → unchanged (this plan touches no Worker code).
- [ ] `PROFILE_REFRESH_PROMPT.md` documents the matching rule.

## STOP conditions

- The drift check shows `keyword_score` already changed → stop, report.
- Any pre-existing test fails after Task 2 → stop, report the test and the
  title it scored; do **not** adjust the test to fit the new rule.
- The reproduction script prints anything other than the six expected rows →
  the archive or the profile moved; stop and re-measure before coding.
- You find yourself adding a trailing `\b` or `(?![a-z0-9])` to make something
  pass → stop. That is the regression this plan exists to prevent.

## Explicitly out of scope

- Re-tuning `40 + hits * 15`, `MIN_DELIVER_SCORE`, or `TEASER_SCORE_CAP`.
- Editing the keyword lists themselves. The refresh routine owns those.
- Matching against anything but the title. `keyword_score` reads the title on
  purpose — the description path belongs to the model ranker.
- Backfilling scores in `jobs/*.json` or `stats.json`. History stays as it ran.
