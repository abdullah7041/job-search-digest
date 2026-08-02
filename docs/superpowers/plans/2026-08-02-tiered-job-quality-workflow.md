# Tiered Job-Quality Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver more viable Saudi senior-IC jobs without overstating confidence, by distinguishing definite link closure from runner-inaccessible pages and presenting Strong and Review fit tiers.

**Architecture:** Keep `digest.py` as the single engine. Convert `verify_link` from a boolean into evidence statuses, enrich eligible jobs before ranking, classify validated rankings into two lists, and render/tailor the two lists separately. Extend `stats.json` additively and keep the existing JSON state and Actions commit-back model.

**Tech Stack:** Python 3.12, `requests`, standard-library `unittest` and `unittest.mock`, GitHub Actions, Discord webhook.

## Global Constraints

- Do not add a runtime dependency, database, scraper, browser automation, or automated job application.
- Preserve the existing Saudi/location, title, and senior-IC hard filters in `matches_filters`.
- Never edit `resume.md` or `profile.json`; tailoring can only use the existing master resume and Gemini key.
- Keep ATS and JD fetching fail-soft; keep Discord delivery fail-loud and only mark jobs seen after successful delivery.
- Do not persist secret values or log request headers or keys.
- Keep existing `seen.json` and `stats.json` files readable; missing new metric keys must be safe.
- Use `python test_digest.py` as the regression command. Every task must leave it passing.

---

## File structure

| File | Responsibility after this work |
|---|---|
| `digest.py` | Link-evidence result, JD enrichment, ranking validation, tier classification, Discord rendering, strong-only tailoring, additive metrics. |
| `test_digest.py` | Offline tests for evidence status, ranking/classification boundaries, tier rendering, tailoring, and metrics. |
| `.github/workflows/digest.yml` | Run offline tests before the live digest and commit only generated state/resume output. |
| `README.md` | Explain Strong/Review semantics, link-evidence labels, the test gate, and weekly calibration. |
| `docs/weekly-job-review-template.md` | A short, versioned weekly review template; not read by runtime code. |

## Task 1: Introduce link-evidence statuses

**Files:**

- Modify: `digest.py:188-195,794-840`
- Modify: `test_digest.py:106-133`

**Interfaces:**

- Produces: `LINK_OPEN = "open"`, `LINK_CLOSED = "closed"`, `LINK_UNKNOWN = "unknown"` constants near the existing state/config constants.
- Produces: `verify_link(url: str) -> str`, returning exactly one of the constants.
- Consumes: `_get(url)`, `CLOSED_MARKERS`, and the current Greenhouse/Workable/Ashby positive closure proofs.

- [ ] **Step 1: Write failing unit tests for the link-evidence contract**

Add this test class after `TestHelpers` in `test_digest.py`:

```python
class TestLinkEvidence(unittest.TestCase):
    def _response(self, status, *, url="https://example.com/job/123", text="", history=None):
        response = mock.Mock()
        response.status_code = status
        response.url = url
        response.text = text
        response.history = history if history is not None else []
        return response

    def test_forbidden_and_rate_limited_links_are_unknown(self):
        for status in (403, 429):
            with mock.patch.object(digest, "_get", return_value=self._response(status)):
                self.assertEqual(digest.verify_link("https://example.com/job/123"), digest.LINK_UNKNOWN)

    def test_request_error_is_unknown(self):
        with mock.patch.object(digest, "_get", side_effect=digest.requests.Timeout("slow")):
            self.assertEqual(digest.verify_link("https://example.com/job/123"), digest.LINK_UNKNOWN)

    def test_definite_not_found_and_closed_marker_are_closed(self):
        with mock.patch.object(digest, "_get", return_value=self._response(404)):
            self.assertEqual(digest.verify_link("https://example.com/job/123"), digest.LINK_CLOSED)
        with mock.patch.object(digest, "_get", return_value=self._response(200, text="This position has been filled")):
            self.assertEqual(digest.verify_link("https://example.com/job/123"), digest.LINK_CLOSED)

    def test_live_page_is_open_and_missing_url_is_closed(self):
        with mock.patch.object(digest, "_get", return_value=self._response(200, text="Apply now")):
            self.assertEqual(digest.verify_link("https://example.com/job/123"), digest.LINK_OPEN)
        self.assertEqual(digest.verify_link(""), digest.LINK_CLOSED)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python -m unittest test_digest.TestLinkEvidence -v`  
Expected: FAIL because the status constants do not exist and `verify_link` still returns booleans.

- [ ] **Step 3: Write the minimal verifier**

Add the constants after `STATS_TTL_DAYS`:

```python
LINK_OPEN = "open"
LINK_CLOSED = "closed"
LINK_UNKNOWN = "unknown"
```

Make `verify_link` return `LINK_UNKNOWN` for request errors and 403, 408, 429, or 5xx status codes; return `LINK_CLOSED` for a missing URL, 404/other non-200 code, existing redirect/Ashby/marker closure proofs; and return `LINK_OPEN` only at the final success exit. Keep existing messages, but replace `keeping job` with `status unknown` for request errors.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m unittest test_digest.TestLinkEvidence -v`  
Expected: all four new tests PASS.

Run: `python test_digest.py`  
Expected: all pre-existing tests still PASS.

- [ ] **Step 5: Commit the evidence contract**

Run:

```bash
git add digest.py test_digest.py
git commit -m "feat: retain jobs with inconclusive link checks"
```

## Task 2: Enrich jobs, validate ranking, and classify fit tiers

**Files:**

- Modify: `digest.py:173-186,607-650,925-995`
- Modify: `test_digest.py:133-206`

**Interfaces:**

- Produces: `STRONG_MATCH_MIN_SCORE = 80` near `MAX_JOBS_IN_DIGEST`.
- Produces: `enrich_descriptions(jobs: list[dict]) -> None`; it sets each job's bounded `description` using `get_description`.
- Produces: `apply_ranking(jobs: list[dict], ranking: dict | None) -> None`; every job leaves with integer `score`, string `why`, and boolean `ranking_verified`.
- Produces: `split_tiers(jobs: list[dict]) -> tuple[list[dict], list[dict]]`, where a strong job has a validated score `>= STRONG_MATCH_MIN_SCORE` and non-empty description; every other eligible job is review.
- Consumes: existing `gemini_rank`, `keyword_score`, `get_description`, `JD_RANK_CHARS`, and `PROFILE`.

- [ ] **Step 1: Write failing tests for enrichment and tier boundaries**

Add this class before `TestEndToEnd`:

```python
class TestTierClassification(unittest.TestCase):
    def test_enrichment_populates_each_surviving_job(self):
        jobs = [dict(REAL_JOBS[1], description=""), dict(REAL_JOBS[4], description="")]
        with mock.patch.object(digest, "get_description", side_effect=["LLM Python", "SQL cloud"]):
            digest.enrich_descriptions(jobs)
        self.assertEqual([job["description"] for job in jobs], ["LLM Python", "SQL cloud"])

    def test_only_validated_high_score_with_jd_is_strong(self):
        jobs = [
            {"id": "strong", "title": "Senior Software Engineer", "description": "Python LLM"},
            {"id": "low", "title": "Senior Data Engineer", "description": "SQL"},
            {"id": "no-jd", "title": "Senior Backend Engineer", "description": ""},
            {"id": "malformed", "title": "Senior AI Engineer", "description": "Python"},
        ]
        ranking = {
            "strong": {"score": 80, "why": "Requirements fit"},
            "low": {"score": 79, "why": "Adjacent data work"},
            "no-jd": {"score": 95, "why": "Title looks strong"},
            "malformed": {"score": "not-a-number", "why": "bad"},
        }
        digest.apply_ranking(jobs, ranking)
        strong, review = digest.split_tiers(jobs)
        self.assertEqual([job["id"] for job in strong], ["strong"])
        self.assertEqual({job["id"] for job in review}, {"low", "no-jd", "malformed"})
        self.assertFalse(next(job for job in jobs if job["id"] == "malformed")["ranking_verified"])
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python -m unittest test_digest.TestTierClassification -v`  
Expected: FAIL because the enrichment, ranking, and tier functions do not exist.

- [ ] **Step 3: Implement bounded enrichment and defensive ranking**

Add the threshold with the other digest limits:

```python
STRONG_MATCH_MIN_SCORE = 80
```

Implement the helpers immediately after `get_description`:

```python
def enrich_descriptions(jobs):
    for job in jobs:
        job["description"] = get_description(job)


def apply_ranking(jobs, ranking):
    for job in jobs:
        result = ranking.get(job["id"]) if isinstance(ranking, dict) else None
        try:
            score = int(result["score"])
            why = str(result["why"]).strip()[:140]
            valid = 0 <= score <= 100 and bool(why)
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            job.update(score=score, why=why, ranking_verified=True)
        else:
            job.update(score=keyword_score(job), why="matches your target titles and skills", ranking_verified=False)


def split_tiers(jobs):
    strong = [job for job in jobs if (
        job["ranking_verified"]
        and job["score"] >= STRONG_MATCH_MIN_SCORE
        and bool(job.get("description"))
    )]
    review = [job for job in jobs if job not in strong]
    return strong, review
```

Update `main` to enrich fresh jobs before calling `gemini_rank`, call `apply_ranking(fresh, ranking)`, and call `split_tiers(fresh)` before sorting. Do not call the model when `fresh` is empty.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m unittest test_digest.TestTierClassification -v`  
Expected: both tests PASS.

Run: `python test_digest.py`  
Expected: all tests PASS. Update legacy mocks that return `True` from `verify_link` in Task 3; do not weaken the new status contract.

- [ ] **Step 5: Commit ranking and tier classification**

Run:

```bash
git add digest.py test_digest.py
git commit -m "feat: classify digest jobs into strong and review tiers"
```

## Task 3: Render tiers, tailor only strong jobs, and persist additive metrics

**Files:**

- Modify: `digest.py:872-995`
- Modify: `test_digest.py:167-260`

**Interfaces:**

- Consumes: `LINK_OPEN`, `LINK_CLOSED`, `LINK_UNKNOWN`, `apply_ranking`, and `split_tiers` from Tasks 1–2.
- Produces: `save_stats(stats, today, fetched, matched, new, avg_score, **metrics) -> None`, retaining legacy keys and adding counts only when supplied.
- Produces: Discord sections headed `**Strong matches**` and `**Worth reviewing**` whenever their respective lists are non-empty.
- Produces: review-tier `link check inconclusive` and `title-only assessment` flags where applicable.

- [ ] **Step 1: Update end-to-end mocks and write failing tier-delivery tests**

Change `run_main` to accept an explicit verifier result sequence and use it in its own patch (an outer patch cannot override this helper's internal mock):

```python
def run_main(monkey_env, gemini_ranking, tailor_result, link_statuses=None):
    # existing setup
    if link_statuses is None:
        link_statuses = [digest.LINK_OPEN] * len(REAL_JOBS)
    # existing with block, changing only this patch line:
    mock.patch.object(digest, "verify_link", side_effect=link_statuses),
```

Replace the flat-list assertions in `test_full_run_with_tailoring` with checks that the 93-score role is strong, the 74-score role is review, and only one tailored-resume link appears. Add this test method to `TestEndToEnd`:

```python
def test_unknown_link_is_reviewable_and_only_strong_is_tailored(self):
    ranking = {
        "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
        "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
    }
    posts, work = run_main(
        self.ENV,
        ranking,
        "# Tailored resume\ncontent",
        link_statuses=[digest.LINK_UNKNOWN, digest.LINK_OPEN],
    )
    body = "\n".join(posts)
    self.assertIn("**Strong matches**", body)
    self.assertIn("**Worth reviewing**", body)
    self.assertIn("link check inconclusive", body)
    self.assertEqual(body.count("tailored resume:"), 1)
    with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
        stats = json.load(handle)
    self.assertEqual(stats[-1]["link_unknown"], 1)
    self.assertEqual(stats[-1]["strong"], 1)
    self.assertEqual(stats[-1]["review"], 1)
```

Add a second test that calls `run_main(..., link_statuses=[digest.LINK_CLOSED, digest.LINK_OPEN])`; assert the closed role is absent from Discord and `seen.json` while the other eligible role is delivered.

- [ ] **Step 2: Run tier-delivery tests to verify they fail**

Run: `python -m unittest test_digest.TestEndToEnd -v`  
Expected: FAIL because `main` treats status strings as truthy booleans, has one flat Discord list, and tailors every delivered role.

- [ ] **Step 3: Wire statuses, tiers, rendering, tailoring, and metrics through `main`**

Replace boolean verification with explicit status assignment:

```python
for job in fresh:
    job["link_status"] = verify_link(job["url"])
closed = [job for job in fresh if job["link_status"] == LINK_CLOSED]
fresh = [job for job in fresh if job["link_status"] != LINK_CLOSED]
unknown_count = sum(job["link_status"] == LINK_UNKNOWN for job in fresh)
```

After Task 2 classification, sort both lists by score. Apply the cap deterministically: `delivered_strong = strong[:MAX_JOBS_IN_DIGEST]`, then `delivered_review = review[:MAX_JOBS_IN_DIGEST - len(delivered_strong)]`. Render only non-empty tier headers. Use these delivered lists for Discord, `seen.json`, tailoring, and `strong`/`review` stats. For review items, calculate flags with:

```python
flags = []
if job.get("link_status") == LINK_UNKNOWN:
    flags.append("link check inconclusive")
if not job.get("description"):
    flags.append("title-only assessment")
flag_line = f"\n  _{'; '.join(flags)}_" if flags else ""
```

Change the tailoring loop to `for job in strong[:TAILOR_MAX]:`; retain current non-blocking tailoring failure behavior. Mark every delivered strong and review job seen only after `post_discord` returns.

Extend `save_stats` to merge named counts into each row while retaining `date`, `fetched`, `matched`, `new`, and `avg_score`. Pass `link_closed`, `link_unknown`, `strong`, `review`, `tailored`, and `tailor_failed` from both the heartbeat and delivery branches.

- [ ] **Step 4: Run full regression suite**

Run: `python test_digest.py`  
Expected: all existing and new offline tests PASS, including review-only and closed-link cases.

- [ ] **Step 5: Inspect the generated state shape**

Run: `python -m unittest test_digest.TestEndToEnd.test_unknown_link_is_reviewable_and_only_strong_is_tailored -v`  
Expected: PASS; the temporary `stats.json` row has legacy fields plus `link_unknown`, `strong`, and `review`.

- [ ] **Step 6: Commit delivery and metrics**

Run:

```bash
git add digest.py test_digest.py
git commit -m "feat: deliver tiered job digest with quality metrics"
```

## Task 4: Gate the workflow and document weekly calibration

**Files:**

- Modify: `.github/workflows/digest.yml:20-42`
- Modify: `README.md:82-130`
- Create: `docs/weekly-job-review-template.md`

**Interfaces:**

- Consumes: `python test_digest.py` and the Discord headings/flags from Task 3.
- Produces: a GitHub Actions test step before `Run digest`.
- Produces: a manually completed weekly review template that is not read or modified by `digest.py`.

- [ ] **Step 1: Record the expected workflow order**

In the implementation PR description, state this exact order: `Install dependencies` → `Run offline tests` → `Run digest` → `Persist state files and tailored resumes`.

- [ ] **Step 2: Add the Actions regression gate**

Insert this step between `pip install -r requirements.txt` and `Run digest`:

```yaml
      - name: Run offline regression tests
        run: python test_digest.py
```

Do not modify the secrets block. Keep the persistence step restricted to `seen.json`, `stats.json`, and `tailored/`.

- [ ] **Step 3: Add the weekly review template**

Create `docs/weekly-job-review-template.md` with this content:

```markdown
# Weekly job-digest review

**Week of:** YYYY-MM-DD

## Strong matches

| Role URL or ID | Applied / saved / skipped | Why |
|---|---|---|
| | | |

## Worth reviewing

| Role URL or ID | Useful / noise / inaccessible | Why |
|---|---|---|
| | | |

## Calibration decision

- Keep or change `STRONG_MATCH_MIN_SCORE`:
- Filter/profile wording to revisit:
- Link hosts inaccessible from Actions but usable locally:
```

Do not create a runtime parser for this file.

- [ ] **Step 4: Update README behavior documentation**

Replace the one-list delivery description with these rules:

```markdown
- **Strong matches** have a validated Gemini score of at least 80 and JD text; only these receive automatic tailored resumes.
- **Worth reviewing** passed every hard eligibility filter but have a lower/invalid score, no JD text, or other fit uncertainty.
- A link check marked **inconclusive** means GitHub Actions received a block, rate limit, or network failure. The role is kept; only definite closure is dropped.
```

Add `python test_digest.py` to the setup verification checklist and link to `docs/weekly-job-review-template.md` for the weekly two-minute calibration review.

- [ ] **Step 5: Run all verification gates**

Run: `python test_digest.py`  
Expected: PASS.

Run: `actionlint .github/workflows/digest.yml`  
Expected: no output and exit code 0. If `actionlint` is unavailable, do not install a project dependency; report the unavailable local lint and rely on GitHub Actions for hosted YAML validation.

Run: `git diff --check`  
Expected: no output and exit code 0.

- [ ] **Step 6: Commit workflow and documentation**

Run:

```bash
git add .github/workflows/digest.yml README.md docs/weekly-job-review-template.md
git commit -m "ci: test tiered digest before delivery"
```

## Final verification

- [ ] Run `python test_digest.py` and confirm every test passes.
- [ ] Run `git status --short` and confirm only intended files are changed or the worktree is clean after commits.
- [ ] Review `git log --oneline -4` and confirm commits are limited to link evidence, tiered delivery, and workflow/docs changes.
- [ ] Trigger or observe one manual GitHub Actions run only after the implementation branch is published; confirm the test step precedes live delivery and state persistence occurs after it.
