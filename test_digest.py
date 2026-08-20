#!/usr/bin/env python3
"""
Offline tests for digest.py. No network: every fetcher, verifier, and LLM call
is stubbed. Run with:  python test_digest.py

Covers:
  - the six real jobs from the 2026-07-31 digest through the new filters
  - location_conflict (the Bangkok-as-Riyadh bug)
  - the seniority ceiling (the Lead/Staff-scored-98 bug)
  - strip_html helper
  - main() end-to-end with mocks: full job snapshots written, no unsolicited
    tailored resumes, seen.json updated
  - fail-soft paths: ranker down -> keyword scores
  - the JD the ranker actually reads (jd_signal), teaser grading and capping,
    and the score-instability detector
"""

import os
import sys
import json
import datetime
import shutil
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# Import from a temp cwd so test runs never touch the real seen.json/stats.json.
_TMP = tempfile.mkdtemp(prefix="digest_test_")
for f in ("profile.json", "resume.md"):
    src = os.path.join(REPO, f)
    if os.path.exists(src):
        shutil.copy(src, _TMP)
os.chdir(_TMP)

import digest  # noqa: E402
import job_archive  # noqa: E402


# The six jobs exactly as the 2026-07-31 digest delivered them (JSearch source,
# Arabic city + expanded country, old Gemini fit scores in comments).
REAL_JOBS = [
    {"company": "intelmatix", "id": "jsearch_a", "title": "Lead AI Engineer",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/a",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Lead our AI engineering team."},                      # old fit 98
    {"company": "Next", "id": "jsearch_b", "title": "Senior Software Engineer (AI Systems)",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/b",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Build production LLM systems, RAG pipelines, prompt engineering, Python."},  # old fit 95
    {"company": "Qualcomm", "id": "jsearch_c",
     "title": "Machine Learning Engineer, Staff level (Datacentre AI Engineering) - Riyadh, KSA",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/c",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Staff-level ML engineering."},                        # old fit 92
    {"company": "Riyad Bank", "id": "jsearch_d", "title": "Data Engineering Team Leader",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/d",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Lead the data engineering team."},                    # old fit 85
    {"company": "Datamatics Technologies", "id": "jsearch_e", "title": "Senior Data Architect",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/e",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Design data architecture, SQL, cloud."},              # old fit 80
    {"company": "Agoda", "id": "jsearch_f",
     "title": "Staff Data Engineer - PPC Bidding (Bangkok based, relocation provided)",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/f",
     "first_published": "2026-07-30", "source": "jsearch", "token": "",
     "description": "Bangkok-based role, relocation provided."},           # old fit 60
]

EXPECT_PASS = {"jsearch_b", "jsearch_e"}          # Next senior, Datamatics senior
EXPECT_DROP = {"jsearch_a", "jsearch_c", "jsearch_d", "jsearch_f"}


class TestFilters(unittest.TestCase):
    def test_six_real_jobs(self):
        passed = {j["id"] for j in REAL_JOBS if digest.matches_filters(j)}
        self.assertEqual(passed, EXPECT_PASS)

    def test_seniority_ceiling(self):
        for title in ("Lead AI Engineer", "Staff Software Engineer",
                      "Principal Engineer", "Head of Engineering",
                      "Engineering Manager - Platform", "Data Engineering Team Leader",
                      "Director of Data", "Tech Lead"):
            job = {"title": title, "location": "Riyadh, Saudi Arabia"}
            self.assertFalse(digest.matches_filters(job), title)

    def test_senior_ic_still_passes(self):
        for title in ("Senior Software Engineer", "Senior AI Engineer",
                      "Machine Learning Engineer", "Senior Data Engineer",
                      "Senior Backend Engineer", "Solutions Engineer"):
            job = {"title": title, "location": "Riyadh, Saudi Arabia"}
            self.assertTrue(digest.matches_filters(job), title)

    def test_junior_still_dropped(self):
        job = {"title": "Junior Software Engineer", "location": "Riyadh, Saudi Arabia"}
        self.assertFalse(digest.matches_filters(job))

    def test_location_conflict(self):
        self.assertTrue(digest.location_conflict("Staff Data Engineer (Bangkok based, relocation provided)"))
        self.assertTrue(digest.location_conflict("Senior Engineer (Dubai based)"))
        self.assertTrue(digest.location_conflict("Backend Engineer - relocation to Berlin"))
        self.assertFalse(digest.location_conflict("Riyadh based Senior Engineer"))
        self.assertFalse(digest.location_conflict("Senior Software Engineer (AI Systems)"))


class TestHelpers(unittest.TestCase):
    def test_strip_html_escaped(self):
        # Greenhouse ships escaped HTML in 'content'
        s = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;li&gt;Python&lt;/li&gt;"
        out = digest.strip_html(s)
        self.assertIn("Build", out)
        self.assertIn("Python", out)
        self.assertNotIn("<", out)

    def test_strip_html_raw(self):
        self.assertEqual(digest.strip_html("<b>Senior</b> role"), "Senior role")


class TestLinkEvidence(unittest.TestCase):
    def _response(self, status, *, url="https://example.com/job/123", text="", history=None):
        response = mock.Mock()
        response.status_code = status
        response.url = url
        response.text = text
        response.history = history if history is not None else []
        return response

    def test_forbidden_and_rate_limited_links_are_unknown(self):
        for status in (401, 403, 429):
            with mock.patch.object(digest, "_get", return_value=self._response(status)):
                self.assertEqual(
                    digest.verify_link("https://example.com/job/123"), digest.LINK_UNKNOWN
                )

    def test_request_error_is_unknown(self):
        with mock.patch.object(digest, "_get", side_effect=digest.requests.Timeout("slow")):
            self.assertEqual(
                digest.verify_link("https://example.com/job/123"), digest.LINK_UNKNOWN
            )

    def test_definite_not_found_and_closed_marker_are_closed(self):
        with mock.patch.object(digest, "_get", return_value=self._response(404)):
            self.assertEqual(
                digest.verify_link("https://example.com/job/123"), digest.LINK_CLOSED
            )
        with mock.patch.object(
            digest,
            "_get",
            return_value=self._response(200, text="This position has been filled"),
        ):
            self.assertEqual(
                digest.verify_link("https://example.com/job/123"), digest.LINK_CLOSED
            )

    def test_live_page_is_open_and_missing_url_is_closed(self):
        with mock.patch.object(digest, "_get", return_value=self._response(200, text="Apply now")):
            self.assertEqual(
                digest.verify_link("https://example.com/job/123"), digest.LINK_OPEN
            )
        self.assertEqual(digest.verify_link(""), digest.LINK_CLOSED)


class TestGetDescription(unittest.TestCase):
    def _resp(self, payload):
        r = mock.Mock()
        r.status_code = 200
        r.json = lambda: payload
        return r

    def test_inline_description_short_circuits(self):
        job = {"description": "already here", "source": "ashby", "token": "x"}
        with mock.patch.object(digest, "_get", side_effect=AssertionError("no fetch expected")):
            self.assertEqual(digest.get_description(job), "already here")

    def test_greenhouse_on_demand(self):
        job = {"id": "123", "title": "t", "company": "c", "description": "",
               "source": "greenhouse", "token": "hala"}
        payload = {"content": "&lt;p&gt;Build LLM systems in Riyadh&lt;/p&gt;"}
        with mock.patch.object(digest, "_get", return_value=self._resp(payload)) as g:
            out = digest.get_description(job)
        self.assertIn("Build LLM systems", out)
        self.assertIn("boards-api.greenhouse.io/v1/boards/hala/jobs/123", g.call_args[0][0])

    def test_workable_widget_cached_per_company(self):
        digest._workable_details_cache.clear()
        j1 = {"id": "1", "title": "t1", "company": "Salla", "description": "",
              "source": "workable", "token": "salla", "shortcode": "AAA"}
        j2 = {"id": "2", "title": "t2", "company": "Salla", "description": "",
              "source": "workable", "token": "salla", "shortcode": "BBB"}
        payload = {"jobs": [{"shortcode": "AAA", "description": "<b>JD A</b>"},
                            {"shortcode": "BBB", "description": "JD B"}]}
        with mock.patch.object(digest, "_get", return_value=self._resp(payload)) as g:
            self.assertEqual(digest.get_description(j1), "JD A")
            self.assertEqual(digest.get_description(j2), "JD B")
        self.assertEqual(g.call_count, 1)  # second job served from cache

    def test_network_error_fails_soft(self):
        job = {"id": "9", "title": "t", "company": "c", "description": "",
               "source": "greenhouse", "token": "hala"}
        with mock.patch.object(digest, "_get", side_effect=digest.requests.ConnectionError("down")):
            self.assertEqual(digest.get_description(job), "")


# A posting shaped like the real ones: recruiting copy first, the job second.
# The McKinsey posting archived on 2026-08-20 puts "Skills" at character 1931,
# so the old 900-character excerpt was culture text and nothing else.
BOILERPLATE = (
    "Who You'll Work With. Driving lasting impact with our clients is not easy "
    "work. You are the kind of person who thrives in a high performance culture. "
    "In return we provide mentorship and the resources to become a stronger "
    "leader faster than you thought possible. Continuous learning, a voice that "
    "matters, a global community, world-class benefits. " * 6
)
REQUIREMENTS = (
    "Your Impact. You will design and ship production services for clients. "
    "Requirements: 5+ years building production Python services, experience with "
    "LLM application development, and a track record shipping backend systems."
)
FULL_JD = BOILERPLATE + REQUIREMENTS

# 497 characters of aggregator marketing copy, the length of the Pasqal quantum
# posting that was delivered as a scored match on 2026-08-20.
TEASER_JD = (
    "Pasqal seeks a Senior Machine Learning Engineer to adapt and deploy quantum "
    "ML methods for client datasets, leveraging around 5 years of experience in "
    "ML. You will build pipelines, collaborate with R&D, and ensure client-ready "
    "solutions with strong maintainability. The position entails close "
    "coordination with hardware teams, domain experts from partner enterprises, "
    "and ongoing scientific monitoring to keep the company at the cutting edge."
)


class TestRankedExcerpt(unittest.TestCase):
    """What the ranker reads, which is what the whole score rests on."""

    def test_the_excerpt_starts_at_the_requirements_not_at_the_top(self):
        self.assertGreater(FULL_JD.index("Requirements:"), 900)
        excerpt = job_archive.jd_signal(FULL_JD, digest.JD_RANK_CHARS)

        self.assertIn("5+ years building production Python", excerpt)
        self.assertFalse(excerpt.startswith("Who You'll Work With"))

    def test_a_heading_word_inside_a_sentence_does_not_start_the_window(self):
        """A bare substring search put the McKinsey window at '...your location,
        experience, and skills), we provide a comprehensive benefits package' —
        the word 'skills' inside a sentence about perks, three lines before the
        real heading."""
        jd = ("Padding. " * 200 + "based on your location, experience, and skills), "
              "we provide a comprehensive benefits package.\n\nYour Impact\n\n"
              "You will drive architectural design and make key technology decisions.")
        excerpt = job_archive.jd_signal(jd, 500)

        self.assertTrue(excerpt.startswith("Your Impact"))

    def test_a_description_that_fits_the_window_is_sent_whole(self):
        """The Pasqal teaser's only heading sits near its end, so skipping ahead
        left 199 characters of a 497-character description. When it all fits
        there is nothing to choose between."""
        excerpt = job_archive.jd_signal(TEASER_JD, digest.JD_RANK_CHARS)

        self.assertEqual(excerpt, TEASER_JD)

    def test_a_posting_with_no_heading_is_read_from_the_top_as_before(self):
        plain = "We need someone to write software. " * 40
        self.assertEqual(job_archive.jd_signal(plain, 100), plain[:100])

    def test_an_empty_description_stays_empty_rather_than_raising(self):
        self.assertEqual(job_archive.jd_signal("", 2000), "")
        self.assertEqual(job_archive.jd_signal(None, 2000), "")

    def test_the_listing_order_does_not_depend_on_fetch_order(self):
        """The same job scored 40 to 75 across ten runs on a byte-identical
        description. One input the run controls is which jobs sit next to it."""
        jobs = [dict(REAL_JOBS[i], description=FULL_JD) for i in range(3)]
        captured = {}

        def fake(prompt, *args, **kwargs):
            captured[len(captured)] = prompt
            return {"scores": []}

        with mock.patch.object(digest, "chat_json", side_effect=fake):
            digest.rank_jobs(jobs, "key")
            digest.rank_jobs(list(reversed(jobs)), "key")

        self.assertEqual(captured[0], captured[1])


class TestTeaserGrading(unittest.TestCase):
    """An aggregator blurb clears every completeness check while evidencing
    nothing: 400 characters, 60 words, and the word 'experience'."""

    def test_the_pasqal_length_blurb_is_graded_a_teaser_not_a_description(self):
        self.assertEqual(job_archive.jd_completeness_failures(TEASER_JD), [])
        self.assertEqual(job_archive.jd_grade(TEASER_JD), job_archive.GRADE_TEASER)

    def test_a_real_posting_is_graded_full(self):
        self.assertEqual(job_archive.jd_grade(FULL_JD), job_archive.GRADE_FULL)

    def test_a_short_but_structured_posting_is_a_posting_not_a_teaser(self):
        """Length alone was the wrong test. A brief posting that says what the
        work is and what it needs is real evidence; a teaser is one paragraph of
        prose with no structure at all. The shortest description graded full in
        the archive is 1300 characters; the longest teaser is 512."""
        short = ("About the role: we are hiring an engineer to build and own the "
                 "data pipelines behind our reporting stack. Responsibilities "
                 "include designing production services, reviewing architecture "
                 "with the platform team, monitoring pipeline health, and "
                 "documenting the decisions you make along the way. "
                 "Requirements: five years of Python, solid SQL, and experience "
                 "running workloads on a major cloud platform. Nice to have: "
                 "exposure to streaming systems and infrastructure as code.")
        self.assertLess(len(short), job_archive.TEASER_MIN_CHARS)
        self.assertEqual(job_archive.jd_grade(short), job_archive.GRADE_FULL)

    def test_a_long_posting_without_familiar_headings_is_still_a_posting(self):
        """Two archived descriptions over 3000 characters carry no heading this
        module recognises. Unstructured is not the same as absent."""
        wall = ("We are a fast growing company and you will write software, ship "
                "it, and keep it running. Experience with backend systems "
                "matters more to us than any particular qualification. " * 40)
        self.assertEqual(job_archive.signal_headings(wall), [])
        self.assertEqual(job_archive.jd_grade(wall), job_archive.GRADE_FULL)

    def test_a_fragment_and_an_absent_description_keep_their_old_grades(self):
        self.assertEqual(job_archive.jd_grade("Too short."), job_archive.GRADE_INCOMPLETE)
        self.assertEqual(job_archive.jd_grade(""), job_archive.GRADE_MISSING)

    def test_a_snapshot_written_before_the_field_existed_still_grades(self):
        snapshot = job_archive.make_snapshot(
            dict(REAL_JOBS[0], description=TEASER_JD), "2026-08-20T00:00:00Z")
        del snapshot["jd"]["grade"]

        self.assertEqual(job_archive.snapshot_grade(snapshot), job_archive.GRADE_TEASER)

    def test_a_teaser_is_capped_below_the_strong_match_line(self):
        job = dict(REAL_JOBS[0], description=TEASER_JD, score=95,
                   why="Senior IC role", ranking_verified=True)
        capped = digest.cap_teaser_scores([job])

        self.assertEqual(capped, [job])
        self.assertLess(job["score"], digest.STRONG_MATCH_MIN_SCORE)
        self.assertIn("teaser", job["why"])
        self.assertEqual(digest.split_tiers([job])[0], [])

    def test_a_full_posting_keeps_the_score_the_model_gave_it(self):
        job = dict(REAL_JOBS[0], description=FULL_JD, score=92,
                   why="Strong IC fit", ranking_verified=True)

        self.assertEqual(digest.cap_teaser_scores([job]), [])
        self.assertEqual(job["score"], 92)

    def test_the_card_says_what_it_is_instead_of_showing_a_bare_number(self):
        job = dict(REAL_JOBS[0], description=TEASER_JD, score=69, why="x",
                   ranking_verified=True, tier="Worth reviewing")
        footer = job_archive.job_card_payload(
            job_archive.make_snapshot(job, "2026-08-20T00:00:00Z"))["embeds"][0]["footer"]["text"]

        self.assertIn("teaser", footer.lower())

    def test_a_keyword_fallback_score_is_never_dressed_up_as_a_model_score(self):
        """Two cards on 2026-08-19 read 'fit 55 - matches your target titles and
        skills'. That is 40 + 15 per keyword, rendered identically to a real
        score, on a day the ranker was simply down."""
        job = dict(REAL_JOBS[0], description=FULL_JD, score=55, why="x",
                   ranking_verified=False, tier="Worth reviewing")
        footer = job_archive.job_card_payload(
            job_archive.make_snapshot(job, "2026-08-20T00:00:00Z"))["embeds"][0]["footer"]["text"]

        self.assertIn("Heuristic", footer)


class TestScoreInstability(unittest.TestCase):
    """The daily average cannot show one posting scoring 40 then 75."""

    def _archive(self, root, date, score, jd=FULL_JD, verified=True):
        os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
        job = dict(REAL_JOBS[0], description=jd, score=score, why="x",
                   ranking_verified=verified, tier="Worth reviewing")
        snapshot = job_archive.make_snapshot(job, f"{date}T00:00:00Z")
        with open(os.path.join(root, "jobs", f"{date}.json"), "w") as handle:
            json.dump({"schema_version": 1, "date": date, "jobs": [snapshot]}, handle)

    def test_a_score_that_moved_on_an_unchanged_description_is_reported(self):
        root = tempfile.mkdtemp(prefix="instability_")
        self._archive(root, "2026-08-19", 40)
        self._archive(root, "2026-08-20", 75)

        unstable = job_archive.score_instability(
            os.path.join(root, "jobs"), today=datetime.date(2026, 8, 20))

        self.assertEqual(len(unstable), 1)
        self.assertEqual(unstable[0]["spread"], 35)

    def test_a_steady_score_is_not_reported(self):
        root = tempfile.mkdtemp(prefix="instability_steady_")
        self._archive(root, "2026-08-19", 70)
        self._archive(root, "2026-08-20", 72)

        self.assertEqual(job_archive.score_instability(
            os.path.join(root, "jobs"), today=datetime.date(2026, 8, 20)), [])

    def test_keyword_fallback_scores_are_left_out_of_the_comparison(self):
        """keyword_score is a constant for a given title, so including it would
        read as perfect stability on exactly the days the ranker was down."""
        root = tempfile.mkdtemp(prefix="instability_fallback_")
        self._archive(root, "2026-08-19", 40, verified=False)
        self._archive(root, "2026-08-20", 75, verified=False)

        self.assertEqual(job_archive.score_instability(
            os.path.join(root, "jobs"), today=datetime.date(2026, 8, 20)), [])

    def test_a_missing_archive_directory_is_not_an_error(self):
        self.assertEqual(job_archive.score_instability("no-such-directory"), [])


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


class TestStatsRows(unittest.TestCase):
    def _rows(self, work):
        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_rerunning_the_same_day_replaces_that_days_row(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            stats = []
            digest.save_stats(stats, "2026-08-08", 200, 20, 5, 70.0)
            stats = digest.load_stats()
            digest.save_stats(stats, "2026-08-08", 214, 21, 8, 76.9)
            rows = digest.load_stats()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["avg_score"], 76.9)
        self.assertEqual(rows[0]["fetched"], 214)

    def test_a_different_day_is_appended_and_history_stays_sorted(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            stats = []
            digest.save_stats(stats, "2026-08-09", 214, 21, 8, 68.3)
            stats = digest.load_stats()
            digest.save_stats(stats, "2026-08-08", 214, 21, 8, 76.9)
            rows = digest.load_stats()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        self.assertEqual([row["date"] for row in rows], ["2026-08-08", "2026-08-09"])

    def test_drift_counts_days_not_runs(self):
        # 14 distinct scored days are required before a comparison is trusted.
        # 13 days plus a duplicate must NOT be enough.
        rows = [{"date": f"2026-07-{day:02d}", "avg_score": 80.0} for day in range(1, 14)]
        rows.append({"date": "2026-07-13", "avg_score": 20.0})
        self.assertIsNone(digest.check_drift(rows))

    def test_drift_warns_on_a_real_multi_day_drop(self):
        rows = [{"date": f"2026-07-{day:02d}", "avg_score": 85.0} for day in range(1, 8)]
        rows += [{"date": f"2026-07-{day:02d}", "avg_score": 60.0} for day in range(8, 15)]
        warning = digest.check_drift(rows)
        self.assertIsNotNone(warning)
        self.assertIn("profile may be stale", warning)


def run_main(monkey_env, ranking, link_statuses=None):
    """Run digest.main() fully mocked; return (discord_posts, workdir)."""
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json["content"])
        resp = mock.Mock()
        resp.status_code = 204
        resp.text = ""
        return resp

    work = tempfile.mkdtemp(prefix="digest_run_")
    for f in ("profile.json", "resume.md"):
        src = os.path.join(REPO, f)
        if os.path.exists(src):
            shutil.copy(src, work)
    old_cwd = os.getcwd()
    os.chdir(work)
    if link_statuses is None:
        link_statuses = [digest.LINK_OPEN] * len(REAL_JOBS)
    try:
        with mock.patch.dict(os.environ, monkey_env, clear=False), \
             mock.patch.object(digest, "fetch_jsearch", return_value=[dict(j) for j in REAL_JOBS]), \
             mock.patch.object(digest, "COMPANIES", []), \
             mock.patch.object(digest, "verify_link", side_effect=link_statuses), \
             mock.patch.object(digest, "rank_jobs", return_value=ranking), \
             mock.patch.object(digest.requests, "post", side_effect=fake_post), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.main()
    finally:
        os.chdir(old_cwd)
    return posts, work


class TestEndToEnd(unittest.TestCase):
    ENV = {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
           "OPENROUTER_API_KEY": "test-key",
           "GITHUB_REPOSITORY": "abdullah7041/job-search-digest"}

    def test_worker_only_quiet_day_does_not_call_an_absent_webhook(self):
        work = tempfile.mkdtemp(prefix="digest_quiet_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            with mock.patch.dict(os.environ, {
                     "INTERACTIVE_WORKER_URL": "https://worker.test",
                     "WORKER_CALLBACK_SECRET": "secret",
                 }, clear=True), \
                 mock.patch.object(digest, "fetch_jsearch", return_value=[]), \
                 mock.patch.object(digest, "COMPANIES", []), \
                 mock.patch.object(digest, "post_interactive_cards", return_value={"posted": ["heartbeat"], "existing": [], "failed": []}) as post_worker, \
                 mock.patch.object(digest, "post_discord") as post_webhook:
                digest.main()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        post_worker.assert_called_once()
        self.assertIn("no new roles today", post_worker.call_args.kwargs["heartbeat"]["content"])
        self.assertTrue(post_worker.call_args.kwargs["heartbeat"]["key"].startswith("heartbeat:"))
        post_webhook.assert_not_called()

    def test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def retryable(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [],
                "existing": [],
                "in_progress": [snapshots[0]["job_key"]],
                "retryable": [snapshots[1]["job_key"]],
                "failed": [],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=retryable):
            posts, work = run_main(env, ranking)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_failed_interactive_delivery_fails_loudly_without_falling_back(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def failed(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [],
                "existing": [],
                "in_progress": [],
                "retryable": [],
                "failed": [
                    {"job_key": snapshot["job_key"], "error": "Discord card post failed: 503"}
                    for snapshot in snapshots
                ],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=failed), \
             mock.patch.object(digest, "post_discord") as post_webhook, \
             mock.patch.object(digest, "save_seen") as save_seen:
            with self.assertRaises(SystemExit) as raised:
                run_main(env, ranking)

        # The run fails loudly...
        self.assertEqual(raised.exception.code, 1)
        # ...without double-posting through the webhook...
        post_webhook.assert_not_called()
        # ...and without marking anything seen, so tomorrow retries these jobs.
        save_seen.assert_called_once()
        self.assertEqual(save_seen.call_args[0][0], {})

    def test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }

        def partial(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [snapshots[0]["job_key"]],
                "existing": [],
                "in_progress": [],
                "retryable": [],
                "failed": [{"job_key": snapshots[1]["job_key"], "error": "Discord card post failed: 503"}],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=partial):
            posts, work = run_main(env, ranking)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)), 1)

    def test_full_run_archives_jobs_without_auto_tailoring(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"}}
        posts, work = run_main(self.ENV, ranking)
        body = "\n".join(posts)
        # Only the two senior IC roles delivered
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("Senior Data Architect", body)
        for gone in ("Lead AI Engineer", "Staff level", "Team Leader", "Bangkok"):
            self.assertNotIn(gone, body)
        self.assertIn("**Strong matches**", body)
        self.assertIn("**Worth reviewing**", body)
        # Tailoring is explicit through a Discord button; daily delivery is
        # never allowed to spend credits or leave a resume in the repository.
        self.assertNotIn("tailored resume:", body)
        self.assertFalse(os.path.exists(os.path.join(work, "tailored")))
        # Every posted job keeps its full JD in a durable daily archive.
        archive_date = datetime.date.today().isoformat()
        with open(os.path.join(work, "jobs", f"{archive_date}.json"), encoding="utf-8") as handle:
            archive = json.load(handle)
        self.assertEqual({item["source_id"] for item in archive["jobs"]}, {"jsearch_b", "jsearch_e"})
        self.assertTrue(all(item["jd"]["text"] for item in archive["jobs"]))
        # State written
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertEqual(set(seen), {"jsearch_b", "jsearch_e"})

    def test_unknown_link_is_reviewable_and_only_strong_is_tailored(self):
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }
        posts, work = run_main(
            self.ENV,
            ranking,
            link_statuses=[digest.LINK_UNKNOWN, digest.LINK_OPEN],
        )
        body = "\n".join(posts)
        self.assertIn("link check inconclusive", body)
        self.assertNotIn("tailored resume:", body)
        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            stats = json.load(handle)
        self.assertEqual(stats[-1]["link_unknown"], 1)
        self.assertEqual(stats[-1]["strong"], 1)
        self.assertEqual(stats[-1]["review"], 1)

    def test_closed_link_is_not_delivered_or_seen(self):
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
        }
        posts, work = run_main(
            self.ENV,
            ranking,
            link_statuses=[digest.LINK_CLOSED, digest.LINK_OPEN],
        )
        body = "\n".join(posts)
        self.assertNotIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("Senior Data Architect", body)
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertEqual(set(seen), {"jsearch_e"})

    def test_gemini_down_keyword_fallback_still_delivers(self):
        posts, work = run_main(self.ENV, None)
        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("fit ", body)
        self.assertNotIn("tailored resume:", body)  # tailor also failed -> no links, no crash

    def test_no_ranker_key_no_tailoring(self):
        env = dict(self.ENV)
        env.pop("OPENROUTER_API_KEY")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            posts, work = run_main(env, None)
        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertNotIn("tailored resume:", body)
        self.assertFalse(os.path.exists(os.path.join(work, "tailored")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
