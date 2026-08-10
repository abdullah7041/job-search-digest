#!/usr/bin/env python3
"""
Offline tests for digest.py. No network: every fetcher, verifier, and LLM call
is stubbed. Run with:  python test_digest.py

Covers:
  - the six real jobs from the 2026-07-31 digest through the new filters
  - location_conflict (the Bangkok-as-Riyadh bug)
  - the seniority ceiling (the Lead/Staff-scored-98 bug)
  - strip_html / slugify helpers
  - main() end-to-end with mocks: full job snapshots written, no unsolicited
    tailored resumes, seen.json updated
  - fail-soft paths: Gemini down -> keyword scores, no key -> no tailoring,
    tailor failure -> job still delivered
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

    def test_slugify(self):
        self.assertEqual(digest.slugify("Senior Software Engineer (AI Systems)"),
                         "senior-software-engineer-ai-systems")
        self.assertEqual(digest.slugify("شركة"), "job")  # non-latin falls back safely

    def test_tailored_url_local_and_actions(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_REPOSITORY", None)
            self.assertEqual(digest.tailored_url("tailored/x.md"), "tailored/x.md")
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "abdullah7041/job-search-digest"}):
            self.assertEqual(
                digest.tailored_url("tailored/x.md"),
                "https://github.com/abdullah7041/job-search-digest/blob/main/tailored/x.md")


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


def run_main(monkey_env, gemini_ranking, tailor_result, link_statuses=None):
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
             mock.patch.object(digest, "gemini_rank", return_value=gemini_ranking), \
             mock.patch.object(digest, "tailor_resume", return_value=tailor_result), \
             mock.patch.object(digest.requests, "post", side_effect=fake_post), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.main()
    finally:
        os.chdir(old_cwd)
    return posts, work


class TestEndToEnd(unittest.TestCase):
    ENV = {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
           "GEMINI_API_KEY": "test-key",
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
            posts, work = run_main(env, ranking, None)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_failed_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
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

        with mock.patch.object(digest, "post_interactive_cards", side_effect=failed):
            posts, work = run_main(env, ranking, None)

        self.assertEqual(posts, [])
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_full_run_archives_jobs_without_auto_tailoring(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"}}
        posts, work = run_main(self.ENV, ranking, "# Tailored resume\ncontent")
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
            "# Tailored resume\ncontent",
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
            "# Tailored resume\ncontent",
            link_statuses=[digest.LINK_CLOSED, digest.LINK_OPEN],
        )
        body = "\n".join(posts)
        self.assertNotIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("Senior Data Architect", body)
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertEqual(set(seen), {"jsearch_e"})

    def test_gemini_down_keyword_fallback_still_delivers(self):
        posts, work = run_main(self.ENV, None, None)
        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("fit ", body)
        self.assertNotIn("tailored resume:", body)  # tailor also failed -> no links, no crash

    def test_no_gemini_key_no_tailoring(self):
        env = dict(self.ENV)
        env.pop("GEMINI_API_KEY")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            posts, work = run_main(env, None, None)
        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertNotIn("tailored resume:", body)
        self.assertFalse(os.path.exists(os.path.join(work, "tailored")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
