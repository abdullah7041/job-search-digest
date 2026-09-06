# Plan 014: Hold "Why you fit" notes to the same evidence check the gaps already pass

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 23de025..HEAD -- resume_tailor.py test_interactive.py`
> If either changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as
> a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `23de025`, 2026-08-30

## Why this matters

This project's central promise is that a tailored CV never claims anything the
master resume does not evidence. `resume_source.validate_tailored_resume`
enforces it on every bullet, on the headline and on the summary. `clean_gaps`
enforces the mirror rule on the gaps list. **`fit_notes` is the one output that
passes through completely unchecked.**

`fit_notes` is not cosmetic. It is rendered into the Discord message under
**"Why you fit"** and is the text the candidate reads just before deciding
whether to send the CV — and, in an interview, the framing they are most likely
to repeat out loud. A model that writes *"Direct production experience with
Kubernetes and Terraform at scale"* against a resume that mentions neither
produces a claim the candidate may carry into a room and be unable to support.

There is a second reason this asymmetry matters. `fit_notes` is generated from
a prompt that contains up to 6000 characters of **job-description text fetched
from a third-party page** (`resume_tailor.py:145`). Every other channel out of
that prompt is constrained back to the resume's own vocabulary; `fit_notes` is
the only one that is not. Whatever the reason a note ends up unsupported —
model drift, an odd JD, or JD text engineered to steer the model — the same
check that already protects the gaps closes it.

The prompt already states the rule (rule 7: *"each naming concrete evidence"*).
Nothing enforces it. This plan makes the code agree with the prompt, exactly as
the summary-length and headline checks did before it.

## Current state

### Files

- `resume_tailor.py` — builds the prompt, calls the model, validates and
  applies the plan. `clean_gaps` is at line 256; `tailor` returns at line
  312–313.
- `resume_source.py` — the evidence helpers. `unsupported_terms(text, allowed)`
  and `unsupported_facts(text, evidence)` are the two this plan uses. Do not
  modify this file.
- `test_interactive.py` — offline tests. `clean_gaps` is already tested at
  line 549; put the new tests beside it.

### The unchecked return (`resume_tailor.py:311-313`)

```python
        if not errors:
            return apply_plan(document, plan), clean_gaps(plan, document, snapshot), \
                [str(note) for note in plan.get("fit_notes", [])][:3]
```

`clean_gaps` runs on the gaps. The fit notes are stringified, truncated to
three, and returned. That is the entire treatment they get.

### Where they surface (`interactive_runner.py:146-162`)

```python
def _message(title: str, company: str, gaps: list[str], fit_notes: list[str],
             note: str, warnings: list[str] | None = None) -> str:
    """Everything the candidate needs to read, and nothing an employer sees."""
    lines = [f"Tailored one-page CV for **{title}** at **{company}**, built from your confirmed master resume."]
    ...
    if fit_notes:
        lines.append("\n**Why you fit**")
        lines.extend(f"- {note}" for note in fit_notes)
```

### The vocabulary a fit note must be checked against — and why it is NOT `clean_gaps`'

`tailor` already computes the right set, at `resume_tailor.py:281`:

```python
    allowed = vocabulary(master_text, snapshot.get("title", ""), snapshot.get("company", ""))
```

That is the set every other output is checked against (it is passed to
`validate_tailored_resume` on line 304). **`clean_gaps` deliberately uses a
narrower, resume-only set**, because a gap must name something the resume lacks
and the target title must not count as evidence.

A fit note is the opposite kind of sentence: it naturally names the role and the
employer. Checked against the resume-only set, a perfectly good note is silently
dropped. Measured at `23de025` against the real `resume.md`:

```
note  = "Your production LLM evaluation work maps directly to this Senior AI Engineer role at Qiddiya."
unsupported_terms(note, vocabulary(master))                            -> ['Qiddiya.']     # dropped
unsupported_terms(note, vocabulary(master, "Senior AI Engineer", "Qiddiya")) -> []          # kept
```

So `clean_fit_notes` **takes the allowed set as an argument** and `tailor`
passes its existing `allowed`. Do not recompute a narrower one inside the
function, and do not copy `clean_gaps`' `known = vocabulary(...)` block.

### The function to model on (`resume_tailor.py:256-273`)

```python
def clean_gaps(plan: dict, document: dict, snapshot: dict) -> list[str]:
    """Keep only gaps that name something concrete the resume lacks."""
    known = vocabulary(document["summary"], document["headline"],
                       *[bullet["source"] for section in document["sections"]
                         for entry in section["entries"] for bullet in entry["bullets"]],
                       *[line["source"] for section in document["sections"]
                         for line in section["lines"]])
    gaps = []
    for gap in plan.get("gaps", []):
        gap = re.sub(r"\s+", " ", str(gap)).strip()
        if len(gap) < 12:
            continue
        if not unsupported_terms(gap, known):
            # Nothing in it is actually absent from the resume, so it is filler.
            continue
        gaps.append(gap)
    return gaps[:5]
```

**Note the direction.** A gap is *kept* when `unsupported_terms` finds
something — a gap must name a thing the resume lacks. A fit note is the
inverse: it must name nothing the resume lacks, so it is *dropped* when
`unsupported_terms` finds something. Do not copy the condition; invert it.

### What the helpers do (`resume_source.py`)

- `vocabulary(*texts) -> set[str]` — every lowercased word the model may treat
  as established fact.
- `unsupported_terms(text, allowed) -> list[str]` — proper nouns and acronyms
  in `text` with no basis in `allowed`. It deliberately skips a sentence's
  opening word, so ordinary capitalisation is not flagged.
- `unsupported_facts(text, evidence) -> list[str]` — numbers and percentages in
  `text` that do not appear in `evidence`. A fit note claiming *"reduced
  latency by 90%"* against a resume that says 40% is exactly what this catches.

### The prompt rule this enforces (`resume_tailor.py:131-132`)

```
        "7. fit_notes: the two or three strongest reasons this candidate matches, each "
        "naming concrete evidence.\n"
```

### Repo conventions

- Comments explain *why* and name the concrete failure they prevent. The
  `clean_gaps` comment (`# Nothing in it is actually absent from the resume, so
  it is filler.`) is the house length and voice.
- Docstrings are one line where one line suffices, and explain the *rule*, not
  the mechanics.
- **Fail-soft**: a failure costs the user the feature, never the run. Dropping
  an unsupported note is exactly that — the candidate loses one line of
  encouragement, never the CV. Do **not** make an unsupported fit note raise or
  reject the draft; that would cost the user a document over a cosmetic field.
- Tests are `unittest`, offline. See `test_interactive.py:543-556` for how
  `clean_gaps` is tested against a parsed `MASTER` document.

## Commands you will need

| Purpose      | Command                                                                       | Expected on success |
|--------------|-------------------------------------------------------------------------------|---------------------|
| Install      | `python -m pip install --require-hashes -r requirements.txt`                   | exit 0              |
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` | `OK`                |
| Worker tests | `cd worker && npm test`                                                       | `fail 0`            |

**Record your observed baseline in step 1.** At `23de025` the Python suite
reported `Ran 199 tests`. Other plans move that number — express your done
criteria as a delta from what you observe.

## Scope

**In scope** (the only files you may modify):
- `resume_tailor.py` — one new function plus the return statement in `tailor`
- `test_interactive.py` — new tests

**Out of scope** (do NOT touch):
- `resume_source.py`. `unsupported_terms` and `unsupported_facts` are used by
  the bullet, headline and summary checks; changing either changes what a
  *rejected draft* means, which is a far larger blast radius than this plan.
- `clean_gaps`. It is correct as written and its direction is deliberately the
  opposite of the new function's.
- `interactive_runner.py`. The message rendering does not change; it simply
  receives a cleaner list.
- The prompt text in `build_prompt`. Rule 7 already states the requirement.
- `resume.md`, `profile.json` — user content.

## Git workflow

- Branch: `advisor/014-validate-fit-notes`
- Conventional Commits. Recent examples from `git log`:
  `fix: enforce tailored resume contracts`,
  `fix: score jobs on the part of the JD that states requirements`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Record the baseline

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`. Record `Ran N tests` as **N_before**.

### Step 2: Add `clean_fit_notes`

In `resume_tailor.py`, add a new function immediately **after** `clean_gaps`
(so the two mirror-image rules sit together). Unlike `clean_gaps` it takes the
already-computed vocabulary rather than a document — see the section above for
why — plus the master text, because a fit note's numbers are checked against the
whole resume rather than one bullet, the same reasoning
`validate_tailored_resume` uses for the summary (`resume_source.py:214-216`).

Produce this shape:

```python
def clean_fit_notes(plan: dict, allowed: set[str], master_text: str) -> list[str]:
    """Keep only fit notes whose claims the resume actually supports.

    The mirror of clean_gaps: a gap must name something the resume lacks, and a
    fit note must name nothing it lacks. This was the one model output nothing
    checked, and it is the text the candidate reads under "Why you fit" and is
    most likely to repeat in an interview -- so an invented framework or an
    inflated percentage here costs more than one in the PDF would.

    ``allowed`` is tailor's own vocabulary -- the resume plus the target title
    and employer -- not clean_gaps' narrower resume-only set. A fit note names
    the role it is arguing for; checked against the resume alone, every note
    that says the employer's name would be thrown away.

    A number the resume does not contain is dropped even when the job
    description supplied it, so a note quoting a stated requirement ("meets the
    5+ years asked for") does not survive unless the resume states that figure.
    That is the deliberate side of the trade: the case worth catching is a note
    inflating one of the candidate's own metrics, and there is no way to tell
    the two apart from the number alone.
    """
    notes = []
    for note in plan.get("fit_notes", []):
        note = re.sub(r"\s+", " ", str(note)).strip()
        if not note:
            continue
        if unsupported_terms(note, allowed) or unsupported_facts(note, master_text):
            continue
        notes.append(note)
    return notes[:3]
```

You must also add `unsupported_facts` to the import at `resume_tailor.py:17`,
which currently reads:

```python
from resume_source import unsupported_terms, validate_tailored_resume, vocabulary
```

Keep the import alphabetised: `unsupported_facts, unsupported_terms,
validate_tailored_resume, vocabulary`.

**Verify**: `python -c "import resume_tailor; print(callable(resume_tailor.clean_fit_notes))"`
→ prints `True`.

### Step 3: Use it in `tailor`

Replace the return at `resume_tailor.py:311-313`:

```python
        if not errors:
            return apply_plan(document, plan), clean_gaps(plan, document, snapshot), \
                [str(note) for note in plan.get("fit_notes", [])][:3]
```

with:

```python
        if not errors:
            return (apply_plan(document, plan),
                    clean_gaps(plan, document, snapshot),
                    clean_fit_notes(plan, allowed, master_text))
```

Both `allowed` (a local, computed at `resume_tailor.py:281`) and `master_text`
(a parameter, `resume_tailor.py:273-274`) are already in scope at that line —
`allowed` and `master_text` are both passed to `validate_tailored_resume` on
line 304 — so nothing new has to be threaded through.

**Verify**: `grep -n "plan.get(\"fit_notes\", \[\])" resume_tailor.py` → exactly
one match, inside `clean_fit_notes`. Then
`python -m unittest test_interactive.py` → `OK`, same count as before your test
additions.

### Step 4: Add tests

Add these to `test_interactive.py`, beside the existing `clean_gaps` test
(around line 543–556). Use that test's structure, but note the new function
takes a **vocabulary set**, not a document: build it the way `tailor` does,
`allowed = vocabulary(MASTER, "<target title>", "<employer>")`, then build a
`plan` dict by hand, call `clean_fit_notes(plan, allowed, MASTER)`, and assert
on the returned list. `vocabulary` is already imported in `test_interactive.py`
(line 25).

Write five tests:

1. `test_a_fit_note_the_resume_supports_survives` — a note built only from
   words present in `MASTER` is returned unchanged.
2. `test_a_fit_note_may_name_the_target_role_and_employer` — the note
   `"Your production LLM evaluation work maps directly to this <title> role at
   <employer>."` (with the same title/employer used to build `allowed`)
   survives. **This is the test that catches the most likely implementation
   mistake**: recomputing a resume-only vocabulary inside the function, which
   would silently empty the "Why you fit" section on every real run.
3. `test_a_fit_note_naming_a_tool_the_resume_never_mentions_is_dropped` — a
   note naming a proper noun absent from `MASTER` *and* from the title and
   employer you passed (pick one that genuinely does not appear — verify with
   `grep -i` against `resume.md` first) is dropped.
4. `test_a_fit_note_inflating_a_number_is_dropped` — a note containing a
   percentage or figure that does not appear in `MASTER` is dropped. Give this
   one a docstring recording the accepted cost: a note quoting a requirement
   figure from the job description is dropped too, because nothing can
   distinguish it from an inflated one.
5. `test_fit_notes_are_capped_at_three` — six supported notes return exactly
   three.

**Verify**: `python -m unittest test_interactive.py` → `OK`, five more tests
than step 1's `test_interactive` contribution.

### Step 5: Prove the tests catch the regression

Temporarily change the `tailor` return back to the unchecked list
comprehension, run tests 2 and 3, and confirm they still pass (they test
`clean_fit_notes` directly, so they will). Then add one integration-level
assertion if the file already has a `tailor`-level test with a mocked
`call_model`: assert that an unsupported note in the model's answer does not
reach the returned tuple. If no such mocked-`tailor` test exists in
`test_interactive.py`, skip this addition rather than building a new harness —
note the omission in your report.

**Verify**: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
→ `OK`, `Ran (N_before + 5) tests` (or +6 if you added the integration
assertion as its own test).

## Test plan

- **New tests**: the five named in step 4, in `test_interactive.py`.
- **Structural pattern**: the existing `clean_gaps` test at
  `test_interactive.py:543-556`.
- **Choosing the unsupported term**: run
  `grep -ic "<candidate term>" resume.md` and confirm it returns `0` before
  using that term in a test. Do not assume — the resume is user content and
  changes.
- **Existing tests that must keep passing**: every test in `test_interactive.py`
  touching `resume_tailor`, in particular anything asserting on the third
  element of `tailor`'s return tuple. If one breaks because it used an
  unsupported fit note as a fixture, that is the plan working — update the
  fixture to a supported note rather than weakening the check, and say so in
  your report.

## Done criteria

ALL must hold:

- [ ] `grep -c "def clean_fit_notes" resume_tailor.py` returns `1`
- [ ] `grep -c "unsupported_facts" resume_tailor.py` returns `2` (the import and the call)
- [ ] `grep -c "def clean_fit_notes(plan: dict, allowed: set\[str\], master_text: str)" resume_tailor.py` returns `1` — it takes the vocabulary, it does not rebuild one
- [ ] `grep -c "vocabulary(" resume_tailor.py` returns `3` (the import, `clean_gaps`, and `tailor`) — a `4` means `clean_fit_notes` recomputed its own set
- [ ] `grep -n 'for note in plan.get("fit_notes"' resume_tailor.py` returns exactly one line, inside `clean_fit_notes`
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
      → `OK`, `Ran (N_before + 5) tests` or more
- [ ] `cd worker && npm test` → `fail 0` (unchanged)
- [ ] `git status --porcelain` lists only `resume_tailor.py` and `test_interactive.py`
- [ ] `plans/README.md` status row for 014 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The excerpts in "Current state" do not match the live code.
- You find that `clean_fit_notes` drops **every** note in an existing test
  fixture. That would suggest `unsupported_terms` is too strict for
  sentence-shaped prose in this context — report the specific notes and the
  terms flagged rather than loosening the check.
- You are tempted to make an unsupported fit note *reject the draft* (add to
  `errors` and trigger a retry) instead of dropping the note. Do not. That
  turns a cosmetic problem into a lost document, against the repo's fail-soft
  rule.
- You cannot find a proper noun genuinely absent from `resume.md` to use in
  test 3. Report it; do not invent one that happens to appear.
- Test 2 fails — a note naming the target role and employer is being dropped.
  That means the function is not using the `allowed` set it was handed. Fix the
  function, never the test.

## Maintenance notes

- **What interacts with this**: `clean_gaps` and `clean_fit_notes` are
  deliberate mirror images with opposite conditions on the same helper. If
  `unsupported_terms` is ever changed, both must be re-read together — a change
  that makes it stricter makes gaps *more* likely to survive and fit notes
  *less* likely, in the same edit.
- **What a reviewer should scrutinize**: the inverted condition. Copying
  `clean_gaps`' `if not unsupported_terms(...)` into the new function would
  keep exactly the notes that should be dropped, and every test in step 4 is
  written to catch that specific mistake.
- **Deliberately not done here**: the prompt is unchanged. Rule 7 already asks
  for concrete evidence; adding a "you will be checked" clause is a separate,
  independently-testable idea and belongs in its own change if wanted.
- **Follow-up worth knowing about**: `fit_notes` is the last unvalidated model
  output in the tailoring path. After this lands, every string the model
  produces is checked against the resume before a human sees it.
