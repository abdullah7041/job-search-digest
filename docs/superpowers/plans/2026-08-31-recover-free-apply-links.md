# Recover Free Apply Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop discarding jobs that match every filter but arrive with a paywalled link, by preferring a free link JSearch already supplies and falling back to reading the employer's posting off the relister page.

**Architecture:** Two new pure functions in `digest.py` extract candidate apply links from a JSearch job object (tolerating an unverified field name) and choose the first non-paywalled one. `fetch_jsearch` records that on `job["apply_url"]` — never `job["url"]`, which would re-key `job_identity` and redeliver the back catalogue. `main()` then tests the paid-relister cut against `apply_link(job)` instead of the raw URL, and gives `rescue_apply_link` a chance *before* the cut instead of three steps after it.

**Tech Stack:** Python 3.12, `requests`, `unittest`. No new dependencies.

## Global Constraints

- **Three runtime dependencies only:** `requests`, `pypdf`, `reportlab`. Add none.
- **Never write `job["url"]`.** `job_identity` keys `VOLATILE_ID_SOURCES` (includes `jsearch`) on `canonical_url(job["url"])`. Writing it redelivers every affected posting daily. Write `job["apply_url"]` only.
- **Fail-soft:** a failure costs the user the feature, never the run, and always says why. Nothing added here may raise into `main()`.
- **Comments explain *why*** and name the concrete failure they prevent, in full sentences.
- Tests are `unittest`, offline. No test may make a real network call.
- Conventional Commits. Recent examples: `fix: only trust an apply link the employer's own token vouches for`.

**Baseline, verified at `30e7274`:**

| Purpose | Command | Expected |
|---|---|---|
| Python tests | `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` | `OK`, `Ran 212 tests` (skipped=1) |
| Worker tests | `cd worker && npm test` | `pass 44`, `fail 0` |

Record the count you observe; express done criteria as a delta. No Worker file is touched by this plan, so `npm test` must stay at 44 throughout.

## File Structure

- **Modify `digest.py`** — two new pure functions beside `fetch_jsearch`; three edits inside `fetch_jsearch`; one guard in `rescue_apply_link`; one block in `main()`. This file is the engine by design ("One file, no framework" — README), so new JSearch-response logic belongs here next to its only caller, not in `jd_fetch.py`, which is about reading *pages*, not the aggregator API.
- **Modify `test_digest.py`** — one new test class, plus two tests in `TestEndToEnd`.

No new files. No Worker changes.

---

### Task 1: Extract and choose a free apply link

**Files:**
- Modify: `digest.py` (new functions immediately after `fetch_jsearch`, which ends at the `return out` on line 597)
- Test: `test_digest.py` (new class, place after `class TestBoundedFetch` which starts at line 892)

**Interfaces:**
- Consumes: `on_paid_relister(url)` (`digest.py:736`), already imported in scope.
- Produces:
  - `jsearch_apply_options(raw_job: dict) -> tuple[str, list[dict]]` — returns `(field_name, [{"publisher": str, "url": str}])`; `("", [])` when nothing is found.
  - `best_free_link(options: list[dict]) -> str` — a non-paywalled `http(s)` URL, or `""`.

- [ ] **Step 1: Write the failing tests**

Add to `test_digest.py`:

```python
class TestJsearchApplyLinks(unittest.TestCase):
    """Choosing a free apply link out of whatever shape JSearch returns.

    The digest calls /search-v2, whose field names are not fully verified in
    this repository -- fetch_jsearch's docstring lists the ones checked live and
    apply_options is not among them. These tests pin the behaviour for the
    expected name, an alternative name, an unknown name, and absence.
    """

    def test_a_free_option_beside_a_paid_primary_is_chosen(self):
        field, options = digest.jsearch_apply_options({
            "job_apply_link": "https://www.jobleads.com/job/1",
            "apply_options": [
                {"publisher": "Jobleads", "apply_link": "https://www.jobleads.com/job/1"},
                {"publisher": "Greenhouse", "apply_link": "https://job-boards.greenhouse.io/acme/jobs/7"},
            ],
        })
        self.assertEqual(field, "apply_options")
        self.assertEqual(len(options), 2)
        self.assertEqual(digest.best_free_link(options),
                         "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_every_option_paywalled_yields_no_link(self):
        _, options = digest.jsearch_apply_options({
            "apply_options": [
                {"publisher": "Jobleads", "apply_link": "https://www.jobleads.com/job/1"},
                {"publisher": "Jooble", "apply_link": "https://sa.jooble.org/jdp/2"},
            ],
        })
        self.assertEqual(digest.best_free_link(options), "")

    def test_an_unexpected_field_name_is_still_found(self):
        # The generic scan is why this design does not depend on a field name
        # nothing in this repository has verified against /search-v2.
        field, options = digest.jsearch_apply_options({
            "employer_name": "Acme",
            "some_future_name": [
                {"name": "Lever", "url": "https://jobs.lever.co/acme/abc"},
            ],
        })
        self.assertEqual(field, "some_future_name")
        self.assertEqual(digest.best_free_link(options), "https://jobs.lever.co/acme/abc")

    def test_no_options_field_at_all_yields_nothing(self):
        field, options = digest.jsearch_apply_options({
            "job_apply_link": "https://www.jobleads.com/job/1",
            "job_title": "Data Engineer",
        })
        self.assertEqual(field, "")
        self.assertEqual(options, [])
        self.assertEqual(digest.best_free_link(options), "")

    def test_a_non_http_option_is_refused(self):
        # A mailto: or javascript: entry must never become the apply link.
        self.assertEqual(digest.best_free_link([
            {"publisher": "X", "url": "mailto:jobs@acme.test"},
            {"publisher": "Y", "url": "https://jobs.ashbyhq.com/acme/1"},
        ]), "https://jobs.ashbyhq.com/acme/1")

    def test_a_malformed_payload_never_raises(self):
        for payload in (None, [], "text", {"apply_options": "not-a-list"},
                        {"apply_options": [None, 3, "x"]}):
            field, options = digest.jsearch_apply_options(payload)
            self.assertEqual((field, options), ("", []))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_digest.TestJsearchApplyLinks -v`
Expected: FAIL — `AttributeError: module 'digest' has no attribute 'jsearch_apply_options'`

- [ ] **Step 3: Write the implementation**

Insert into `digest.py` immediately after `fetch_jsearch`'s `return out`:

```python
# JSearch names its alternative apply links differently across API versions, and
# /search-v2's field list was never verified here -- fetch_jsearch's docstring
# records only the fields checked live, and this is not one of them. Rather than
# hardcode a guess, the known names are tried first and any list of link-bearing
# dicts is accepted after that. An unrecognised response yields nothing and the
# caller keeps the behaviour it has today.
JSEARCH_APPLY_OPTION_KEYS = ("apply_options", "apply_links")

_LINK_KEY_HINTS = ("link", "url")
_PUBLISHER_KEYS = ("publisher", "name", "source", "site")


def _apply_options_from(value):
    """Normalise one candidate field into [{"publisher", "url"}], or []."""
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = ""
        for key, candidate in item.items():
            if isinstance(candidate, str) and any(h in str(key).lower() for h in _LINK_KEY_HINTS):
                url = candidate.strip()
                break
        if not url:
            continue
        publisher = ""
        for key in _PUBLISHER_KEYS:
            if isinstance(item.get(key), str):
                publisher = item[key].strip()
                break
        found.append({"publisher": publisher, "url": url})
    return found


def jsearch_apply_options(raw_job):
    """Every apply link one JSearch job object offers, and the field that held them.

    Returns (field_name, options). The field name travels back so the run log can
    name it once: that line is how the apply_options expectation gets confirmed
    from a real run without a separate probe workflow.
    """
    if not isinstance(raw_job, dict):
        return "", []
    for key in JSEARCH_APPLY_OPTION_KEYS:
        options = _apply_options_from(raw_job.get(key))
        if options:
            return key, options
    for key, value in raw_job.items():
        if key in JSEARCH_APPLY_OPTION_KEYS:
            continue
        options = _apply_options_from(value)
        if options:
            return str(key), options
    return "", []


def best_free_link(options):
    """The first apply link that does not cost money to act on, or "".

    Order is the publisher order JSearch returned; nothing here re-ranks by
    quality, because the only property this needs to decide is free vs paywalled.
    """
    for option in options or []:
        url = ((option or {}).get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if on_paid_relister(url):
            continue
        return url
    return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_digest.TestJsearchApplyLinks -v`
Expected: PASS, `Ran 6 tests`, `OK`

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
Expected: `OK`, `Ran 218 tests` (baseline 212 + 6)

- [ ] **Step 6: Commit**

```bash
git add digest.py test_digest.py
git commit -m "feat: read the free apply link JSearch already returns"
```

---

### Task 2: Record the free link on `apply_url`, never `url`

**Files:**
- Modify: `digest.py:581-590` (the `out.append({...})` inside `fetch_jsearch`) and the loop around it
- Test: `test_digest.py` (add to `class TestJsearchApplyLinks`)

**Interfaces:**
- Consumes: `jsearch_apply_options`, `best_free_link` from Task 1.
- Produces: JSearch job dicts that may carry an `apply_url` key. `apply_link(job)` (`digest.py:1078`) already prefers it.

- [ ] **Step 1: Write the failing tests**

Add to `class TestJsearchApplyLinks` in `test_digest.py`:

```python
    def test_setting_apply_url_never_changes_job_identity(self):
        """The trap this whole design exists to avoid.

        job_identity keys a jsearch posting on canonical_url(job["url"]).
        Improving the link by writing job["url"] would re-key every affected
        posting, making the entire back catalogue look new and redelivering it
        daily for as long as the rescue kept working. record_apply_link's own
        comment says so. apply_url must leave identity untouched.
        """
        job = {"company": "Acme", "id": "jsearch_1", "title": "Data Engineer",
               "location": "Riyadh, Saudi Arabia", "source": "jsearch", "token": "",
               "url": "https://www.jobleads.com/job/1", "description": "d"}
        before = digest.job_identity(job)
        job["apply_url"] = "https://job-boards.greenhouse.io/acme/jobs/7"
        self.assertEqual(digest.job_identity(job), before)
        self.assertEqual(digest.apply_link(job), "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_fetch_jsearch_records_the_free_link_without_moving_the_url(self):
        payload = {"data": {"jobs": [{
            "job_id": "abc", "job_title": "Data Engineer", "employer_name": "Acme",
            "job_city": "Riyadh", "job_country": "SA",
            "job_apply_link": "https://www.jobleads.com/job/1",
            "job_description": "Build pipelines.",
            "apply_options": [
                {"publisher": "Jobleads", "apply_link": "https://www.jobleads.com/job/1"},
                {"publisher": "Greenhouse", "apply_link": "https://job-boards.greenhouse.io/acme/jobs/7"},
            ],
        }]}}
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = payload
        with mock.patch.object(digest.requests, "get", return_value=response), \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            jobs = digest.fetch_jsearch("key")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["url"], "https://www.jobleads.com/job/1")
        self.assertEqual(jobs[0]["apply_url"], "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_fetch_jsearch_sets_no_apply_url_when_every_option_is_paywalled(self):
        payload = {"data": {"jobs": [{
            "job_id": "abc", "job_title": "Data Engineer", "employer_name": "Acme",
            "job_city": "Riyadh", "job_country": "SA",
            "job_apply_link": "https://www.jobleads.com/job/1",
            "job_description": "Build pipelines.",
            "apply_options": [
                {"publisher": "Jobleads", "apply_link": "https://www.jobleads.com/job/1"},
            ],
        }]}}
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = payload
        with mock.patch.object(digest.requests, "get", return_value=response), \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            jobs = digest.fetch_jsearch("key")
        self.assertEqual(jobs[0].get("apply_url"), None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_digest.TestJsearchApplyLinks -v`
Expected: `test_setting_apply_url_never_changes_job_identity` PASSES already (it only exercises existing helpers — that is fine and expected, it is a guard test). The two `fetch_jsearch` tests FAIL with `KeyError: 'apply_url'` and an assertion error respectively.

- [ ] **Step 3: Write the implementation**

In `digest.py`, inside `fetch_jsearch`, add a flag before the `for query in AGGREGATOR_QUERIES:` loop — put it directly after the existing `seen_ids = set()` line (line 541):

```python
    apply_field_logged = False   # the discovered field is named once per run, not per job
```

Then replace the `out.append({...})` block (lines 581-590) with:

```python
            # The primary link is whichever publisher JSearch ranked first, and
            # that is frequently a paid relister -- 100 of the 174 aggregator
            # postings archived to 2026-08-29 sit on hosts the paid-relister cut
            # now discards. Any free alternative in the same response costs no
            # extra request, so it is read here and recorded as apply_url.
            # NEVER as url: job_identity keys a jsearch posting on its URL, so
            # rewriting it would make the whole back catalogue look new.
            apply_field, apply_options = jsearch_apply_options(j)
            free_link = best_free_link(apply_options)
            if apply_field and not apply_field_logged:
                publishers = sorted({o["publisher"] for o in apply_options if o["publisher"]})
                log(f"JSearch apply options read from {apply_field!r}"
                    + (f" (publishers: {', '.join(publishers)[:120]})" if publishers else ""))
                apply_field_logged = True

            job = {
                "company": (j.get("employer_name") or "Unknown").strip(),
                "id":      "jsearch_" + jid,   # prefix avoids id collision with ATS ids
                "title":   (j.get("job_title") or "").strip(),
                "location": location,
                "url":     j.get("job_apply_link") or j.get("job_google_link") or "",
                "first_published": (j.get("job_posted_at_datetime_utc") or "")[:10],
                "source": "jsearch", "token": "",
                "description": (j.get("job_description") or "").strip(),
            }
            if free_link:
                job["apply_url"] = free_link
            out.append(job)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_digest.TestJsearchApplyLinks -v`
Expected: PASS, `Ran 9 tests`, `OK`

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
Expected: `OK`, `Ran 221 tests` (218 + 3)

- [ ] **Step 6: Commit**

```bash
git add digest.py test_digest.py
git commit -m "feat: record the employer's free link as apply_url on aggregator jobs"
```

---

### Task 3: Cut on the link actually handed over

**Files:**
- Modify: `digest.py:1423-1424` (the two `paid_only` / `matched` lines in `main()`)
- Test: `test_digest.py`, `class TestEndToEnd`

**Interfaces:**
- Consumes: `apply_link(job)` (`digest.py:1078`), `apply_url` set by Task 2.
- Produces: nothing new. This is the line that makes Task 2 have any effect.

- [ ] **Step 1: Write the failing test**

Add to `class TestEndToEnd` in `test_digest.py`:

```python
    def test_a_paywalled_job_with_a_free_link_is_delivered(self):
        """The paid-relister cut must judge the link the candidate is handed.

        rescue_apply_link is patched out here: this job already carries a free
        apply_url, so the recovery loop must not touch it, and patching proves
        no page fetch is attempted for it.
        """
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz",
                   apply_url="https://job-boards.greenhouse.io/next/jobs/1")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "rescue_apply_link") as rescue:
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_OPEN, digest.LINK_OPEN])
        rescue.assert_not_called()
        body = "\n".join(posts)
        self.assertIn("job-boards.greenhouse.io/next/jobs/1", body)
        self.assertNotIn("jobleads.com", body)
        # Delivered means remembered: a rescued job must not return tomorrow.
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertTrue(seen, "a delivered job must be recorded in seen.json")

    def test_a_paywalled_job_with_no_free_link_is_still_dropped(self):
        # Note: with nothing left to deliver, main() sends its quiet-day
        # heartbeat, so `posts` is NOT empty. Assert on what it contains.
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "rescue_apply_link"):
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_OPEN])
        body = "\n".join(posts)
        self.assertNotIn("jobleads.com", body)
        self.assertIn("no new roles today", body)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_digest.TestEndToEnd.test_a_paywalled_job_with_a_free_link_is_delivered -v`
Expected: FAIL — the job is dropped because the cut still reads `j.get("url")`, so `posts` is empty and `assertIn` fails.

- [ ] **Step 3: Write the implementation**

In `digest.py`, replace lines 1423-1424:

```python
    paid_only = [j for j in matched if on_paid_relister(j.get("url"))]
    matched = [j for j in matched if not on_paid_relister(j.get("url"))]
```

with:

```python
    # Judge the link the candidate is actually handed, not the one the
    # aggregator happened to rank first. apply_link prefers apply_url, which is
    # where both the API's free alternative and the page rescue put their result
    # -- testing j["url"] here discarded jobs whose free link was already known.
    paid_only = [j for j in matched if on_paid_relister(apply_link(j))]
    matched = [j for j in matched if not on_paid_relister(apply_link(j))]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_digest.TestEndToEnd -v`
Expected: PASS, all tests in the class

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
Expected: `OK`, `Ran 223 tests` (221 + 2)

- [ ] **Step 6: Commit**

```bash
git add digest.py test_digest.py
git commit -m "fix: cut paid relisters on the link the candidate is handed"
```

---

### Task 4: Give the page rescue its chance before the drop

**Files:**
- Modify: `digest.py:1039-1053` (add a guard at the top of `rescue_apply_link`) and `digest.py` `main()` (insert a recovery loop above the cut from Task 3)
- Test: `test_digest.py`, `class TestEndToEnd`

**Interfaces:**
- Consumes: `rescue_apply_link(job)` (`digest.py:1039`), `apply_link`, `on_paid_relister`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

Add to `class TestEndToEnd` in `test_digest.py`:

```python
    def test_a_paywalled_job_is_rescued_before_it_is_dropped(self):
        """rescue_apply_link used to run inside enrich_descriptions, three steps
        after the paid-relister cut, so the postings that most needed it were
        already gone. It must run first now."""
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}

        def rescue(j):
            j["apply_url"] = "https://job-boards.greenhouse.io/next/jobs/1"

        with mock.patch.object(digest, "rescue_apply_link", side_effect=rescue):
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_OPEN, digest.LINK_OPEN])
        body = "\n".join(posts)
        self.assertIn("job-boards.greenhouse.io/next/jobs/1", body)

    def test_a_job_that_already_has_a_free_link_is_not_fetched_twice(self):
        """rescue_apply_link is also called from get_description. Without the
        guard, a job rescued before the cut pays for a second GET later."""
        job = {"company": "Acme", "id": "jsearch_z", "title": "Data Engineer",
               "location": "Riyadh, Saudi Arabia", "source": "jsearch", "token": "",
               "url": "https://www.jobleads.com/job/1", "description": "text",
               "apply_url": "https://job-boards.greenhouse.io/acme/jobs/7"}
        with mock.patch.object(digest, "_get") as get:
            digest.rescue_apply_link(job)
        get.assert_not_called()
        self.assertEqual(job["apply_url"], "https://job-boards.greenhouse.io/acme/jobs/7")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_digest.TestEndToEnd.test_a_paywalled_job_is_rescued_before_it_is_dropped test_digest.TestEndToEnd.test_a_job_that_already_has_a_free_link_is_not_fetched_twice -v`
Expected: both FAIL — the first because the job is dropped before any rescue runs, the second because `_get` is called.

- [ ] **Step 3: Write the implementation**

First, in `digest.py`, add the guard inside `rescue_apply_link` immediately after the existing `if not url or job.get("source") in EMPLOYER_SOURCES:` return (line 1053):

```python
    if job.get("apply_url"):
        # Already holding the employer's own link, from the API's apply options
        # or from an earlier rescue this run. get_description calls this function
        # too, so without this guard a job rescued before the paid-relister cut
        # pays for a second identical GET a few steps later.
        return
```

Second, in `main()`, insert directly above the `paid_only` lines from Task 3:

```python
    # Last chance before the cut below discards the job. This used to run inside
    # enrich_descriptions, three steps after the drop, so the postings it exists
    # for -- an aggregator link to a posting that lives on a free employer board
    # -- were already gone. It runs on `matched` rather than `fresh` because the
    # cut precedes the freshness check; the cost is bounded by the number of
    # paywalled jobs in a run (7 on 2026-08-31).
    api_rescued = sum(1 for j in matched if j.get("apply_url"))
    page_rescued = 0
    for job in matched:
        if not on_paid_relister(apply_link(job)):
            continue
        rescue_apply_link(job)
        if not on_paid_relister(apply_link(job)):
            page_rescued += 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_digest.TestEndToEnd -v`
Expected: PASS, all tests in the class

- [ ] **Step 5: Run the full suite**

Run: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
Expected: `OK`, `Ran 225 tests` (223 + 2)

- [ ] **Step 6: Commit**

```bash
git add digest.py test_digest.py
git commit -m "fix: rescue an employer link before the paid-relister cut, not after"
```

---

### Task 5: Report what the run recovered

**Files:**
- Modify: `digest.py` `main()` (one `log` call after the `paid_only` loop)
- Test: `test_digest.py`, `class TestEndToEnd`

**Interfaces:**
- Consumes: `api_rescued`, `page_rescued` (Task 4), `paid_only` (Task 3).
- Produces: nothing. Observability only.

- [ ] **Step 1: Write the failing test**

Add to `class TestEndToEnd` in `test_digest.py`:

```python
    def test_the_run_reports_how_many_apply_links_it_recovered(self):
        """Without this line there is no way to tell a run where the API
        supplied free links from one where the feature is inert."""
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz",
                   apply_url="https://job-boards.greenhouse.io/next/jobs/1")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "rescue_apply_link"), \
             mock.patch.object(digest, "log") as logged:
            run_main(self.ENV, ranking, jobs=[job],
                     link_statuses=[digest.LINK_OPEN, digest.LINK_OPEN])
        lines = [c.args[0] for c in logged.call_args_list if c.args]
        self.assertTrue(any("apply links: 1 from the API" in line for line in lines),
                        f"no apply-link summary in: {lines}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_digest.TestEndToEnd.test_the_run_reports_how_many_apply_links_it_recovered -v`
Expected: FAIL — `no apply-link summary in: [...]`

- [ ] **Step 3: Write the implementation**

In `digest.py` `main()`, immediately after the existing `for job in paid_only:` logging loop, add:

```python
    log(f"apply links: {api_rescued} from the API, {page_rescued} rescued from the page, "
        f"{len(paid_only)} still paywalled and dropped")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_digest.TestEndToEnd.test_the_run_reports_how_many_apply_links_it_recovered -v`
Expected: PASS

- [ ] **Step 5: Run both suites**

Run: `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py`
Expected: `OK`, `Ran 226 tests` (225 + 1)

Run: `cd worker && npm test`
Expected: `pass 44`, `fail 0` — unchanged, no Worker file was touched

- [ ] **Step 6: Commit**

```bash
git add digest.py test_digest.py
git commit -m "feat: report how many apply links each run recovered"
```

---

## Done criteria

ALL must hold:

- [ ] `grep -c "on_paid_relister(j.get(\"url\"))" digest.py` returns `0` — the cut no longer reads the raw URL
- [ ] `grep -c "on_paid_relister(apply_link(j))" digest.py` returns `2`
- [ ] `grep -c "def jsearch_apply_options" digest.py` returns `1`
- [ ] `grep -c "def best_free_link" digest.py` returns `1`
- [ ] `grep -n 'job\["url"\] =' digest.py` returns no matches — nothing writes `url`
- [ ] `python -m unittest test_digest.py test_interactive.py test_workflow_guard.py` → `OK`, `Ran 226 tests`
- [ ] `cd worker && npm test` → `pass 44`, `fail 0`
- [ ] `git status --porcelain` lists only `digest.py` and `test_digest.py` as modified

## STOP conditions

Stop and report back — do not improvise — if:

- Any pre-existing test fails. The only intended behaviour change is which jobs survive the paid-relister cut; a broken existing test means something else moved.
- `test_setting_apply_url_never_changes_job_identity` fails. That means identity is being re-keyed and the back catalogue would be redelivered. This is the failure this design exists to prevent.
- You find yourself wanting to write `job["url"]`. Do not. See Global Constraints.
- A test makes a real network call — visible as a `WARN apply-link page HTTP ...` line naming a host you did not mock. Patch `digest.rescue_apply_link` or `digest._get` in that test instead.
- The `paid_only` count in `main()` is needed before the recovery loop runs. It is deliberately computed after, so it reflects what survived the rescue.

## Maintenance notes

- **`jsearch_apply_options`'s generic scan is the load-bearing part.** `apply_options` is an expectation about `/search-v2`, not a fact verified in this repository. If the first real run's log line never appears, the API supplies no alternatives and the page rescue is carrying the feature alone — which is the designed fallback, not a bug.
- **The generic scan is deliberately greedy, and that is its one risk.** It accepts any list of dicts holding a `link`/`url` string, so a future response field such as an employer-reviews or similar-jobs array could be mistaken for apply options. Three things bound the damage: it runs only when both known names are absent; `best_free_link` still refuses anything that is not `http(s)`; and the run log names the field it chose. **If that log line ever names a field that is not about applying, add it to a deny-list or drop the generic scan** — do not leave it guessing. The first run's log is the check.
- **Watch the first run's summary line.** `apply links: N from the API, M rescued from the page, K still paywalled and dropped` is the whole success measure. On 2026-08-31 the same run would have read `0, ?, 7`.
- **A reviewer should scrutinise** that `apply_url` and never `url` is written, and that the recovery loop sits above the cut rather than below it. Those two facts are the entire design.
- **Not done here:** the `stats.json` rebase conflict (one row per day means two same-day writers conflict irreconcilably; the retry loop has no conflict handling) and Cloudflare's disabled Workers Logs. Both are recorded in the spec's Out of Scope section.
