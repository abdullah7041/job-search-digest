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
import contextlib
import json
import datetime
import shutil
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# Import from a temp cwd so test runs never touch the real seen.json/stats.json.
_TMP = tempfile.mkdtemp(prefix="digest_test_")
for f in ("profile.json", "resume.md", "boards.json"):
    src = os.path.join(REPO, f)
    if os.path.exists(src):
        shutil.copy(src, _TMP)
os.chdir(_TMP)

import digest  # noqa: E402
import fit_check as fit_check_module  # noqa: E402
import jd_fetch  # noqa: E402
import job_archive  # noqa: E402


# Postings carry a date RELATIVE TO TODAY, not the literal 2026-07-30 the
# original digest recorded. MAX_JOB_AGE_DAYS drops anything older than a week,
# so a hardcoded date turns every end-to-end test into a time bomb that passes
# until the fixture ages past the cutoff and then fails for a reason that has
# nothing to do with the behaviour under test.
RECENT = (digest.riyadh_today() - datetime.timedelta(days=1)).isoformat()


# The six jobs exactly as the 2026-07-31 digest delivered them (JSearch source,
# Arabic city + expanded country, old Gemini fit scores in comments).
REAL_JOBS = [
    {"company": "intelmatix", "id": "jsearch_a", "title": "Lead AI Engineer",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/a",
     "first_published": RECENT, "source": "jsearch", "token": "",
     "description": "Lead our AI engineering team."},                      # old fit 98
    {"company": "Next", "id": "jsearch_b", "title": "Senior Software Engineer (AI Systems)",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/b",
     "first_published": RECENT, "source": "jsearch", "token": "",
     "description": "Build production LLM systems, RAG pipelines, prompt engineering, Python."},  # old fit 95
    {"company": "Qualcomm", "id": "jsearch_c",
     "title": "Machine Learning Engineer, Staff level (Datacentre AI Engineering) - Riyadh, KSA",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/c",
     "first_published": RECENT, "source": "jsearch", "token": "",
     "description": "Staff-level ML engineering."},                        # old fit 92
    {"company": "Riyad Bank", "id": "jsearch_d", "title": "Data Engineering Team Leader",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/d",
     "first_published": RECENT, "source": "jsearch", "token": "",
     "description": "Lead the data engineering team."},                    # old fit 85
    {"company": "Datamatics Technologies", "id": "jsearch_e", "title": "Senior Data Architect",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/e",
     "first_published": RECENT, "source": "jsearch", "token": "",
     "description": "Design data architecture, SQL, cloud."},              # old fit 80
    {"company": "Agoda", "id": "jsearch_f",
     "title": "Staff Data Engineer - PPC Bidding (Bangkok based, relocation provided)",
     "location": "الرياض, Saudi Arabia", "url": "https://example.com/f",
     "first_published": RECENT, "source": "jsearch", "token": "",
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

    def test_junior_software_is_a_valid_early_career_target(self):
        job = {"title": "Junior Software Engineer", "location": "Riyadh, Saudi Arabia"}
        self.assertTrue(digest.matches_filters(job))

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

    # LinkedIn's country subdomains (sa.linkedin.com, the host JSearch relays for
    # Saudi roles) serve the posting in the local language, so the English strings
    # in CLOSED_MARKERS never appear on a closed Saudi posting -- it reads
    # "لم نعد نقبل طلبات التقدم". Verified live 7 Sep 2026 against
    # sa.linkedin.com/jobs/view/...-4455833108, a posting LinkedIn had closed:
    # verify_link called it open. The markup class is the same in every locale.
    LINKEDIN_CLOSED_FIXTURE = (
        '<figure class="closed-job">'
        '<figcaption class="closed-job__flavor--closed">'
        'لم نعد نقبل طلبات التقدم</figcaption></figure>'
    )

    def test_a_closed_linkedin_posting_in_another_language_is_closed(self):
        """The English marker is absent; only the markup class says it's dead."""
        url = "https://sa.linkedin.com/jobs/view/full-stack-developer-at-perfect-vision-4458712297"
        body = self.LINKEDIN_CLOSED_FIXTURE
        for marker in digest.CLOSED_MARKERS:
            self.assertNotIn(marker, body.lower())
        with mock.patch.object(digest, "_get", return_value=self._response(200, url=url, text=body)):
            self.assertEqual(digest.verify_link(url), digest.LINK_CLOSED)

    def test_an_open_linkedin_posting_stays_open(self):
        """The class is absent on a live posting, so nothing here drops it."""
        url = "https://sa.linkedin.com/jobs/view/senior-solution-engineer-at-omniops-4454605146"
        body = '<div class="jobs-apply">قدم الآن</div>'
        with mock.patch.object(digest, "_get", return_value=self._response(200, url=url, text=body)):
            self.assertEqual(digest.verify_link(url), digest.LINK_OPEN)

    def test_the_linkedin_closed_class_is_scoped_to_linkedin(self):
        """Another host using a similar class name is not judged by this gate."""
        url = "https://careers.example.com/job/123"
        with mock.patch.object(
            digest, "_get",
            return_value=self._response(200, url=url, text=self.LINKEDIN_CLOSED_FIXTURE),
        ):
            self.assertEqual(digest.verify_link(url), digest.LINK_OPEN)


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

    def test_partial_inline_text_is_recovered_from_the_posting(self):
        job = {"id": "1", "title": "Data Analyst", "company": "Example",
               "description": "Build Power BI reports.", "source": "jsearch",
               "url": "https://example.test/jobs/1"}
        response = self._resp({})
        response.text = '<script type="application/ld+json">' + json.dumps({
            "@type": "JobPosting", "description": FULL_JD}) + '</script>'
        with mock.patch.object(digest, "_get", return_value=response):
            description = digest.get_description(job)
        self.assertEqual(description, FULL_JD[:digest.JD_MAX_CHARS])
        self.assertEqual(job["description"], FULL_JD)

    def test_failed_partial_text_recovery_preserves_the_original(self):
        job = {"id": "1", "title": "Data Analyst", "company": "Example",
               "description": "Build Power BI reports.", "source": "jsearch",
               "url": "https://example.test/jobs/1"}
        response = self._resp({})
        response.status_code = 429
        response.text = "Too many requests"
        with mock.patch.object(digest, "_get", return_value=response):
            description = digest.get_description(job)
        self.assertEqual(description, "Build Power BI reports.")

    def test_partial_text_recovery_does_not_use_another_employers_posting(self):
        job = {"id": "1", "title": "Data Analyst", "company": "Example Labs",
               "description": "Build Power BI reports.", "source": "jsearch",
               "url": "https://example.test/jobs/1"}
        def get(url):
            response = self._resp({"content": FULL_JD})
            response.text = '<a href="https://boards.greenhouse.io/othercompany/jobs/99">Other jobs</a>'
            return response
        with mock.patch.object(digest, "_get", side_effect=get):
            description = digest.get_description(job)
        self.assertEqual(description, "Build Power BI reports.")
        self.assertNotIn("apply_url", job)


# Synthetic posting with the structure used by the held Workable/LinkedIn jobs.
TASK_HEADING_JD = (
    "Our reporting team builds practical dashboards for commercial operations and customer accounts.\n"
    "{heading}\n"
    "Build Power BI dashboards for weekly sales performance and inventory reporting. "
    "Analyze customer segments, pricing trends, product demand, and operational waste. "
    "Design useful data models, validate report accuracy with business users, document the process, "
    "and maintain scheduled refreshes so the team can act on reliable information.\n"
    "Requirements\n"
    "Experience building Power BI dashboards and communicating business insights is required. "
    "Use Excel and SQL to prepare data, investigate discrepancies, and explain findings. "
    "Work independently with sales colleagues and support recurring reporting improvements."
)


class TestPostingTaskHeadings(unittest.TestCase):
    def test_complete_postings_with_normal_task_headings_are_usable(self):
        for heading in ("What you'll do", "What You’ll Do", "What You Will Do", "Job Description:",
                        "THE ROLE: DEPLOYING IMPACT"):
            with self.subTest(heading=heading):
                text = TASK_HEADING_JD.format(heading=heading)
                self.assertNotIn("too_short", job_archive.jd_completeness_failures(text))
                self.assertEqual(job_archive.jd_completeness_failures(text), [])
                self.assertEqual(job_archive.jd_grade(text), "full")
                snapshot = job_archive.make_snapshot({"description": text}, "2026-10-24T00:00:00Z")
                self.assertEqual(snapshot["jd"]["status"], "captured")

    def test_prose_mentions_of_task_headings_do_not_make_marketing_copy_complete(self):
        text = ("We will explain what you'll do when you join our community and share the job description later. "
                "Our people value experience, learning, collaboration, and benefits. ") * 12
        self.assertIn("responsibilities_not_evidenced", job_archive.jd_completeness_failures(text))
        self.assertNotEqual(job_archive.jd_grade(text), "full")

    def test_structured_posting_country_can_contradict_provider_location(self):
        import jd_fetch
        page = '<script type="application/ld+json">' + json.dumps({
            "@type": "JobPosting", "description": TASK_HEADING_JD.format(heading="Responsibilities"),
            "jobLocation": {"address": {"addressLocality": "Casablanca", "addressCountry": "MA"}},
        }) + '</script>'
        self.assertEqual(jd_fetch.jsonld_posting_countries(page), ["MA"])
        self.assertEqual(jd_fetch.jsonld_posting_countries("<html>No metadata</html>"), [])
        job = dict(REAL_JOBS[1], title="Data Analyst", location="Riyadh, Saudi Arabia")
        self.assertTrue(digest.matches_filters(dict(job, posting_countries=["SA"])))
        self.assertFalse(digest.matches_filters(dict(job, posting_countries=["MA"])))


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
# Delivery tests need full postings; title/location fixture checks are unchanged.
REAL_JOBS = [dict(job, description=FULL_JD) for job in REAL_JOBS]
CLEAN_CHECK = {"blockers": [], "unclear": [], "strengths": [], "ceiling": "high", "note": ""}

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


class TestJobIdentity(unittest.TestCase):
    """What seen.json remembers a posting by."""

    def test_an_aggregator_posting_keys_on_its_url_not_its_id(self):
        job = {"source": "jsearch", "id": "jsearch_opaque_token_1",
               "url": "https://sa.linkedin.com/jobs/view/senior-engineer-4442"}
        same = dict(job, id="jsearch_opaque_token_2")

        self.assertEqual(digest.job_identity(job), digest.job_identity(same))
        self.assertNotIn("opaque", digest.job_identity(job))

    def test_linkedin_tracking_parameters_are_not_part_of_the_identity(self):
        """refId and trackingId change on every response, so leaving them in
        would recreate exactly the churn this replaces."""
        plain = {"source": "jsearch", "id": "a", "url": "https://sa.linkedin.com/jobs/view/x-1"}
        tracked = {"source": "jsearch", "id": "b",
                   "url": "https://sa.linkedin.com/jobs/view/x-1?refId=Q%3D%3D&trackingId=zz9"}

        self.assertEqual(digest.job_identity(plain), digest.job_identity(tracked))

    def test_a_meaningful_query_parameter_is_kept(self):
        """Indeed's ?jk= is the job. Dropping the whole query string would
        collapse every Indeed posting onto one key."""
        first = {"source": "jsearch", "id": "a", "url": "https://sa.indeed.com/viewjob?jk=aaa"}
        second = {"source": "jsearch", "id": "b", "url": "https://sa.indeed.com/viewjob?jk=bbb"}

        self.assertNotEqual(digest.job_identity(first), digest.job_identity(second))

    def test_an_ats_job_keeps_the_bare_id_already_in_seen_json(self):
        """Changing this would make every previously delivered ATS role look new
        on the first run after deploy."""
        job = {"source": "greenhouse", "id": "4556956101",
               "url": "https://boards.greenhouse.io/hala/jobs/4556956101"}

        self.assertEqual(digest.job_identity(job), "4556956101")

    def test_trailing_slashes_and_host_case_do_not_split_one_posting(self):
        first = {"source": "jsearch", "id": "a", "url": "https://Apply.Workable.com/foodics/j/902C/"}
        second = {"source": "jsearch", "id": "b", "url": "https://apply.workable.com/foodics/j/902C"}

        self.assertEqual(digest.job_identity(first), digest.job_identity(second))

    def test_an_aggregator_posting_with_no_url_falls_back_to_its_id(self):
        self.assertEqual(
            digest.job_identity({"source": "jsearch", "id": "jsearch_x", "url": ""}),
            "jsearch_x")

    def test_a_description_already_delivered_is_split_out(self):
        seen = {}
        job = {"source": "jsearch", "id": "a", "url": "https://x/1",
               "company": "Acme", "title": "Backend Engineer",
               "description": "About the role. Responsibilities. Requirements."}
        seen[digest.description_key(job)] = "2026-08-20"
        relisted = dict(job, id="b", url="https://x/2")

        kept, repeats = digest.drop_repeat_descriptions([relisted], seen)

        self.assertEqual(kept, [])
        self.assertEqual(repeats, [relisted])

    def test_a_job_with_no_description_is_never_treated_as_a_repeat(self):
        kept, repeats = digest.drop_repeat_descriptions(
            [{"source": "jsearch", "id": "a", "url": "https://x/1", "description": ""}], {})

        self.assertEqual(len(kept), 1)
        self.assertEqual(repeats, [])

    def test_a_shared_description_for_a_different_role_is_not_a_repeat(self):
        description = "About the role. Responsibilities. Requirements."
        delivered = {"source": "jsearch", "id": "a", "url": "https://x/1",
                     "company": "Acme", "title": "Backend Engineer",
                     "description": description}
        distinct = {"source": "jsearch", "id": "b", "url": "https://x/2",
                    "company": "Beta", "title": "Data Engineer",
                    "description": description}
        seen = {digest.description_key(delivered): "2026-08-20"}

        kept, repeats = digest.drop_repeat_descriptions([distinct], seen)

        self.assertEqual(kept, [distinct])
        self.assertEqual(repeats, [])

    def test_description_key_is_stable_for_normalized_role_context(self):
        job = {"company": "Acme", "title": "Backend Engineer",
               "description": "About the role. Responsibilities. Requirements."}
        normalized = dict(job, company="  ACME ", title="Backend   Engineer")
        distinct = dict(job, company="Beta")

        self.assertEqual(digest.description_key(job), digest.description_key(normalized))
        self.assertNotEqual(digest.description_key(job), digest.description_key(distinct))


class TestEmployerLinkPreference(unittest.TestCase):
    """Which of two links to the same posting the digest hands over."""

    def test_a_board_cannot_hide_an_employer_behind_its_own_decoration(self):
        """JSearch returns 'Zid | زد' where Workable returns 'Zid', and legal
        forms come and go. The latin core is the half both boards agree on."""
        self.assertEqual(digest.company_key("Zid | زد"), "zid")
        self.assertEqual(digest.company_key("Qiddiya | القدية"), "qiddiya")
        self.assertEqual(digest.company_key("HungerStation | هنقرستيشن"), "hungerstation")
        self.assertEqual(digest.company_key("JAL International Co. Ltd."),
                         digest.company_key("JAL International"))

    def test_two_different_employers_do_not_collapse_onto_one_key(self):
        self.assertNotEqual(digest.company_key("Foodics"), digest.company_key("Foodics Labs"))
        self.assertNotEqual(digest.company_key("Lean"), digest.company_key("Lean Tech"))

    def test_a_company_named_only_by_its_legal_form_survives(self):
        """Stripping down to nothing would group it with every other empty key."""
        self.assertEqual(digest.company_key("Co"), "co")

    def test_the_foodics_case_this_was_built_for(self):
        """Archived 2026-08-11 through 08-19: Workable and JobLeads carried the
        identical title under different URLs, so the URL dedupe never joined
        them and the paid copy went out three more times."""
        employer = {"source": "workable", "company": "Foodics",
                    "title": "Senior Data Scientist / Senior ML Engineer",
                    "url": "https://apply.workable.com/foodics/j/E2707EDFCE/"}
        relisted = {"source": "jsearch", "company": "Foodics",
                    "title": "Senior Data Scientist / Senior ML Engineer",
                    "url": "https://www.jobleads.com/sa/job/senior-data-scientist-x"}

        kept, dropped = digest.prefer_employer_postings([relisted, employer])

        self.assertEqual(kept, [employer])
        self.assertEqual(dropped, [relisted])

    def test_an_employer_posting_is_never_dropped_for_another_employer_posting(self):
        """Two boards would never both be the employer, but a same-titled role
        at one company must not eat itself."""
        first = {"source": "workable", "company": "Foodics", "title": "Senior Engineer",
                 "url": "https://apply.workable.com/foodics/j/A/"}
        second = {"source": "greenhouse", "company": "Foodics", "title": "Senior Engineer",
                  "url": "https://job-boards.greenhouse.io/foodics/jobs/1"}

        kept, dropped = digest.prefer_employer_postings([first, second])

        self.assertEqual(kept, [first, second])
        self.assertEqual(dropped, [])

    def test_an_aggregator_posting_with_no_employer_copy_survives(self):
        relisted = {"source": "jsearch", "company": "Qiddiya | القدية",
                    "title": "Senior Data Engineer",
                    "url": "https://sa.linkedin.com/jobs/view/senior-data-engineer-1"}

        kept, dropped = digest.prefer_employer_postings([relisted])

        self.assertEqual(kept, [relisted])
        self.assertEqual(dropped, [])

    def test_a_card_missing_its_company_or_title_groups_with_nothing(self):
        """A key of '::' would collapse every incomplete card in the run."""
        employer = {"source": "workable", "company": "", "title": "",
                    "url": "https://apply.workable.com/x/j/A/"}
        relisted = {"source": "jsearch", "company": "", "title": "",
                    "url": "https://www.jobleads.com/sa/job/x"}

        kept, dropped = digest.prefer_employer_postings([relisted, employer])

        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), 2)


class TestPaidRelisterLinks(unittest.TestCase):
    """JobLeads charges to reach the apply step; the employer's posting is free."""

    def test_the_paid_relisters_are_recognised_however_they_are_spelled(self):
        for url in ("https://www.jobleads.com/sa/job/senior-ml-engineer-riyadh",
                    "https://jobleads.com/sa/job/x",
                    "https://sa.jooble.org/jdp/-1913570090770118991",
                    "https://sa.jobrapido.com/jobpreview/x",
                    "https://www.naukrigulf.com/senior-data-scientist-x",
                    "https://bebee.com/job/x",
                    "https://sa.thebigjobsite.com/job/x",
                    "https://WWW.JOBLEADS.COM:443/sa/job/x"):
            with self.subTest(url=url):
                self.assertTrue(digest.on_paid_relister(url))

    def test_free_boards_and_employer_ats_hosts_are_left_alone(self):
        """LinkedIn is 65 of the 178 archived links and is free to act on.
        Blocking it would empty the digest to fix a different problem."""
        for url in ("https://sa.linkedin.com/jobs/view/senior-engineer-4442",
                    "https://sa.indeed.com/viewjob?jk=aaa",
                    "https://www.glassdoor.co.uk/job-listing/x",
                    "https://apply.workable.com/foodics/j/E2707EDFCE/",
                    "https://job-boards.greenhouse.io/hala/jobs/5364098008",
                    "https://tabby.pinpointhq.com/en/postings/d8044ef0",
                    ""):
            with self.subTest(url=url):
                self.assertFalse(digest.on_paid_relister(url))

    def test_a_lookalike_domain_is_not_treated_as_the_blocked_one(self):
        """Suffix matching has to stop at a label boundary, or notjobleads.com
        and jobleads.com.example.test would both be dropped."""
        self.assertFalse(digest.on_paid_relister("https://notjobleads.com/job/x"))
        self.assertFalse(digest.on_paid_relister("https://jobleads.com.example.test/job/x"))

    def test_the_host_is_reported_without_www_for_the_log_line(self):
        self.assertEqual(digest.link_host("https://www.jobleads.com/sa/job/x"), "jobleads.com")
        self.assertEqual(digest.link_host("https://sa.jooble.org/jdp/-1"), "sa.jooble.org")


class TestRankedExcerpt(unittest.TestCase):
    """What the ranker reads, which is what the whole score rests on."""

    def test_the_excerpt_starts_at_the_requirements_not_at_the_top(self):
        """Pinned against an explicit limit rather than JD_RANK_CHARS. The
        constant is now wide enough to swallow this fixture whole, which is the
        right outcome and the wrong way to test the property: jd_signal only
        chooses a start when the text does not fit."""
        self.assertGreater(FULL_JD.index("Requirements:"), 900)
        excerpt = job_archive.jd_signal(FULL_JD, len(FULL_JD) - 100)

        self.assertIn("5+ years building production Python", excerpt)
        self.assertFalse(excerpt.startswith("Who You'll Work With"))

    def test_the_ranking_window_covers_a_description_of_this_size_whole(self):
        """JD_RANK_CHARS went 2000 to 2600 because the median archived posting
        is 2650 characters. Recording the consequence so a later trim is a
        visible decision rather than a silent re-truncation."""
        self.assertEqual(job_archive.jd_signal(FULL_JD, digest.JD_RANK_CHARS), FULL_JD)

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

    def test_ranker_denies_provider_data_collection(self):
        captured = {}

        def fake(*args, **kwargs):
            captured.update(kwargs)
            return {"scores": []}

        with mock.patch.object(digest, "chat_json", side_effect=fake):
            self.assertEqual(digest.rank_jobs([REAL_JOBS[0]], "key"), {})

        self.assertEqual(captured["provider"].get("data_collection"), "deny")
        self.assertTrue(captured["provider"]["require_parameters"])


class TestRankerEvidence(unittest.TestCase):
    """What the ranker is given to score the candidate ON.

    It used to be PROFILE["blurb"] alone -- a paragraph the candidate wrote
    about himself. Watheq's match feature scored the same role 58 where this
    digest said 85, and its anti-inflation rule names exactly why: a term whose
    only appearance is a summary or a skills list is an unproven claim, not
    evidence. The blurb IS a summary, so every keyword in it read as
    demonstrated capability. The resume goes in as separately labelled
    evidence now, and the blurb keeps the job it is actually good at -- stating
    constraints and wants. See plans/018.
    """

    def _prompt_for(self, jobs=None):
        captured = {}

        def fake(prompt, *args, **kwargs):
            captured["prompt"] = prompt
            return {"scores": []}

        with mock.patch.object(digest, "chat_json", side_effect=fake):
            digest.rank_jobs(jobs or [dict(REAL_JOBS[0], description=FULL_JD)], "key")
        return captured["prompt"]

    def test_a_missing_resume_returns_empty_rather_than_raising(self):
        """The tailoring path raises on a missing resume because a person is
        waiting on it. The daily ranker must degrade instead of crashing."""
        self.assertEqual(digest.load_resume_evidence("no-such-resume.md"), "")

    def test_an_empty_resume_reads_as_no_evidence(self):
        path = os.path.join(tempfile.mkdtemp(prefix="empty_resume_"), "resume.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("   \n\n  ")
        self.assertEqual(digest.load_resume_evidence(path), "")

    def test_the_resume_is_read_and_bounded(self):
        path = os.path.join(tempfile.mkdtemp(prefix="big_resume_"), "resume.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x" * 20000)
        self.assertEqual(len(digest.load_resume_evidence(path, limit=8000)), 8000)

    def test_the_prompt_carries_the_resume_as_a_labelled_evidence_block(self):
        with mock.patch.object(digest, "load_resume_evidence",
                               return_value="Built an LLM evaluation pipeline."):
            prompt = self._prompt_for()
        self.assertIn("<candidate_evidence>", prompt)
        self.assertIn("Built an LLM evaluation pipeline.", prompt)
        evidence = prompt.split("<candidate_evidence>")[1].split("</candidate_evidence>")[0]
        self.assertIn("LLM evaluation pipeline", evidence)

    def test_the_preferences_block_stays_separate_from_the_evidence(self):
        """The blurb carries MUST BE BASED IN SAUDI ARABIA and the
        never-managed-a-team constraint. resume.md carries neither, so the
        blurb cannot simply be replaced by it."""
        with mock.patch.object(digest, "load_resume_evidence", return_value="RESUME BODY"):
            prompt = self._prompt_for()
        self.assertIn("<candidate_preferences>", prompt)
        preferences = prompt.split("<candidate_preferences>")[1].split(
            "</candidate_preferences>")[0]
        self.assertIn(digest.PROFILE["blurb"][:60], preferences)
        self.assertNotIn("RESUME BODY", preferences)

    def test_the_evidence_block_is_omitted_entirely_when_there_is_no_resume(self):
        """An empty <candidate_evidence></candidate_evidence> invites the model
        to read 'no evidence' as 'no capability' and would tank every score on
        a day the file went missing."""
        with mock.patch.object(digest, "load_resume_evidence", return_value=""):
            prompt = self._prompt_for()
        self.assertNotIn("<candidate_evidence>", prompt)
        self.assertIn("<candidate_preferences>", prompt)

    def test_the_prompt_says_only_the_evidence_block_proves_capability(self):
        with mock.patch.object(digest, "load_resume_evidence", return_value="RESUME BODY"):
            prompt = self._prompt_for().lower()
        self.assertIn("proves nothing", prompt)

    def test_a_listed_only_skill_is_not_treated_as_demonstrated(self):
        """Ported from Watheq's keyword-inflation counter."""
        with mock.patch.object(digest, "load_resume_evidence", return_value="RESUME BODY"):
            prompt = self._prompt_for().lower()
        self.assertIn("## skills", prompt)
        self.assertIn("## experience", prompt)

    def test_the_rubric_weighs_stated_years_and_credentials(self):
        """45 of Watheq's 100 points sit in experience and education. This
        rubric had a band for neither."""
        prompt = self._prompt_for().lower()
        self.assertIn("years", prompt)
        self.assertIn("credential", prompt)

    def test_the_unknown_is_not_a_gap_clause_survives(self):
        """Load-bearing: jd_excerpt is a 2000-char window, so a requirement
        missing from it is usually truncation, not absence from the job."""
        prompt = self._prompt_for()
        self.assertIn("is unknown, not a gap", prompt)

    def test_the_listing_order_still_does_not_depend_on_fetch_order(self):
        jobs = [dict(REAL_JOBS[i], description=FULL_JD) for i in range(3)]
        self.assertEqual(self._prompt_for(jobs),
                         self._prompt_for(list(reversed(jobs))))


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

    def test_newline_delimited_headings_survive_teaser_grading(self):
        """A real posting often puts headings on their own lines, without a
        colon or a preceding sentence ending mark."""
        structured = (
            "Our platform team builds the data services used across the company\n"
            "Responsibilities\n"
            + ("Design reliable APIs, review production changes, improve monitoring, "
               "and collaborate with product engineers on delivery plans " * 3)
            + "\nRequirements\n"
            + ("Five years building Python services, strong SQL skills, experience "
               "operating cloud workloads, and clear written communication " * 3)
        )
        self.assertLess(len(structured), job_archive.TEASER_MIN_CHARS)
        self.assertEqual(len(job_archive.signal_headings(structured)), 2)
        self.assertEqual(job_archive.jd_completeness_failures(structured), [])
        self.assertEqual(job_archive.jd_grade(structured), job_archive.GRADE_FULL)

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


class TestKeywordMatching(unittest.TestCase):
    """A skill keyword matches at a word start, with the suffix left free.

    The fallback matcher used a plain substring test, so "ml" fired on AML,
    "api" on Scraping, and "ai" a second time on GenAI where "genai" had
    already scored it. Anchoring the start removes those. Anchoring the END
    too would remove real hits -- English builds meaning by suffix
    (agent->agentic) and noise by prefix (scr+api+ng) -- so the trailing
    boundary is deliberately absent and the two tests below guard it.

    Measured over the 91 distinct titles in jobs/*.json at ab0eb45: the
    word-start rule drops 6 spurious hits and 0 real ones; requiring both
    boundaries drops 8 and destroys 2. See plans/017.
    """

    def test_a_keyword_inside_a_word_does_not_fire(self):
        # "AML Officer" is anti-money-laundering compliance, not ML.
        self.assertEqual(digest.keyword_hits("AML Officer", ["ml"]), 0)

    def test_api_does_not_fire_on_scraping(self):
        self.assertEqual(
            digest.keyword_hits("Senior Data Engineer - Web Scraping", ["api"]), 0)

    def test_a_keyword_still_matches_a_suffixed_word(self):
        """Regression guard: adding a trailing boundary must fail here."""
        self.assertEqual(digest.keyword_hits("AI Engineer - Agentic", ["agent"]), 1)

    def test_a_keyword_still_matches_a_compound_word(self):
        """The second hit a trailing boundary would destroy."""
        self.assertEqual(digest.keyword_hits(
            "Full Stack Machine Learning Engineer (Datacentre AI Engineering)",
            ["data"]), 1)

    def test_matching_is_case_insensitive(self):
        self.assertEqual(digest.keyword_hits("SENIOR PYTHON ENGINEER", ["python"]), 1)

    def test_a_keyword_holding_regex_metacharacters_is_matched_literally(self):
        """full-stack, full stack and forward deployed are all live keywords."""
        self.assertEqual(digest.keyword_hits(
            "Senior Full-Stack Engineer", ["full-stack"]), 1)
        self.assertEqual(digest.keyword_hits(
            "Forward Deployed Engineer", ["forward deployed"]), 1)
        # "." must not behave as the any-character wildcard it is in a regex.
        self.assertEqual(digest.keyword_hits("Data Engineer", ["d.ta"]), 0)

    def test_the_default_profile_keywords_get_the_same_rule(self):
        """A fix that only helps profile.json is half a fix: DEFAULT_PROFILE
        carries ai, ml and api too, and load_profile falls back to it whenever
        profile.json is missing, corrupt or incomplete."""
        defaults = digest.DEFAULT_PROFILE["skill_keywords"]
        self.assertIn("ml", defaults)
        self.assertEqual(digest.keyword_hits("AML Officer", defaults), 0)

    def test_the_score_contract_is_unchanged(self):
        """40 base for passing the filters, +15 per hit, capped at 100."""
        with mock.patch.dict(digest.PROFILE, {"skill_keywords": ["python", "backend"]}):
            self.assertEqual(digest.keyword_score({"title": "Chef"}), 40)
            self.assertEqual(digest.keyword_score({"title": "Python Chef"}), 55)
            self.assertEqual(digest.keyword_score({"title": "Python Backend Chef"}), 70)
        with mock.patch.dict(digest.PROFILE, {"skill_keywords": ["k" + str(i) for i in range(9)]}):
            self.assertEqual(digest.keyword_score({"title": " ".join(
                "k" + str(i) for i in range(9))}), 100)


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


class TestBlockedScoreCap(unittest.TestCase):
    """A verified blocker caps the number as well as annotating the card.

    Mirrors cap_teaser_scores exactly, and for the same reason: "strong match"
    has to mean something, and a role whose stated hard requirements the resume
    cannot evidence is not one however well the ranker liked the title.
    See plans/018.
    """

    def _job(self, ceiling, blockers, score=88, why="Strong senior IC match in Riyadh.",
             severity="high", kind="missing_required_skill"):
        return {"title": "T", "company": "C", "score": score, "why": why,
                "ranking_verified": True, "description": FULL_JD,
                "fit_check": {"blockers": [{"title": f"b{n}", "severity": severity,
                                            "type": kind} for n in range(blockers)],
                              "unclear": [], "strengths": [], "ceiling": ceiling, "note": ""}}

    def test_two_gaps_cap_the_score(self):
        job = self._job("low", 2)
        capped = digest.cap_blocked_scores([job])
        self.assertEqual(capped, [job])
        self.assertEqual(job["score"], fit_check_module.SEVERITY_CEILING["high"])
        self.assertIn("2 verified requirement gaps", job["why"])

    def test_one_stated_degree_the_resume_lacks_caps_on_its_own(self):
        """The case that produced this whole change. A single verified
        education_certification_gap used to return a 'mid' ceiling, cap
        nothing, and show nothing -- so a Ma'aden posting stating a bachelor's
        degree the candidate does not hold was delivered at 91."""
        job = self._job("mid", 1, severity="medium",
                        kind="education_certification_gap")
        self.assertEqual(digest.cap_blocked_scores([job]), [job])
        self.assertEqual(job["score"], fit_check_module.CREDENTIAL_CEILING)
        self.assertIn("1 verified requirement gap", job["why"])
        self.assertNotIn("gaps)", job["why"], "one gap must not read as plural")

    def test_the_lowest_ceiling_wins_and_nothing_stacks(self):
        """Three gaps are not three times worse than the worst of them.
        Stacked subtraction took a measured example from 91 to 46, well past
        the 67 Watheq gave the same posting."""
        job = self._job("low", 1, severity="critical")
        job["fit_check"]["blockers"] += [
            {"title": "b1", "severity": "medium", "type": "domain_gap"},
            {"title": "b2", "severity": "high", "type": "experience_gap"}]
        digest.cap_blocked_scores([job])
        self.assertEqual(job["score"], fit_check_module.SEVERITY_CEILING["critical"])

    def test_a_score_already_under_the_ceiling_is_left_alone(self):
        job = self._job("mid", 1, severity="medium", score=60)
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["score"], 60)

    def test_a_high_ceiling_does_not_cap(self):
        job = self._job("high", 0)
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["score"], 88)

    def test_an_unchecked_job_is_never_capped(self):
        """Silence must mean 'no verdict', never 'bad verdict'. A ranker outage
        must not quietly demote every strong match in the digest."""
        job = self._job("unknown", 0)
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["score"], 88)

    def test_a_job_with_no_check_at_all_is_never_capped(self):
        job = self._job("low", 2)
        del job["fit_check"]
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["score"], 88)

    def test_a_score_already_at_the_cap_gains_no_second_suffix(self):
        """cap_teaser_scores runs first and appends its own reason. Two suffixes
        on a 140-character field means the second truncation eats the first."""
        job = self._job("low", 2, score=digest.TEASER_SCORE_CAP,
                        why="Title fits. (capped: description is a short teaser)")
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["why"].count("(capped:"), 1)

    def test_the_cap_reason_survives_a_full_length_why(self):
        """apply_ranking truncates `why` to exactly 140, so appending the suffix
        and slicing the result used to eat the explanation entirely: the card
        read "fit 69" with a reason written for an 88 and nothing saying why.
        Trim the reason, keep the suffix."""
        job = self._job("low", 3, why="x" * 140)
        digest.cap_blocked_scores([job])
        self.assertLessEqual(len(job["why"]), 140)
        self.assertTrue(job["why"].endswith("(capped: 3 verified requirement gaps)"),
                        f"suffix lost: {job['why'][-50:]!r}")

    def test_the_teaser_cap_reason_also_survives(self):
        job = {"title": "T", "company": "C", "score": 90, "why": "y" * 140,
               "ranking_verified": True, "description": TEASER_JD}
        digest.cap_teaser_scores([job])
        self.assertLessEqual(len(job["why"]), 140)
        self.assertTrue(job["why"].endswith("(capped: description is a short teaser)"),
                        f"suffix lost: {job['why'][-50:]!r}")


class TestStrongPredicate(unittest.TestCase):
    def test_is_strong_and_split_tiers_agree(self):
        jobs = [
            {"ranking_verified": True, "score": 85, "description": FULL_JD},
            {"ranking_verified": True, "score": 70, "description": FULL_JD},
            {"ranking_verified": False, "score": 95, "description": FULL_JD},
            {"ranking_verified": True, "score": 95, "description": ""},
        ]
        strong, competitive, review = digest.split_tiers(jobs)
        self.assertEqual(strong, [j for j in jobs if digest.is_strong(j)])
        self.assertEqual(sorted(id(j) for j in competitive + review),
                         sorted(id(j) for j in jobs if not digest.is_strong(j)))

    def test_duplicate_jobs_are_preserved_in_both_tiers(self):
        """split_tiers built the review tier with `job not in strong` -- a dict
        equality compare, O(n^2) with deep comparison. plans/README.md:296
        flagged it and correctly judged the collapse unreachable, since
        strength is a pure function of the dict: two equal dicts are either
        both strong or both not. The predicate refactor makes it a `not
        is_strong(job)` anyway, so this pins that neither tier loses a
        duplicate."""
        weak = {"ranking_verified": True, "score": 40, "description": FULL_JD}
        strong_job = {"ranking_verified": True, "score": 95, "description": FULL_JD, "fit_check": CLEAN_CHECK}

        strong, competitive, review = digest.split_tiers([dict(weak), dict(weak)])
        self.assertEqual((len(strong), len(competitive), len(review)), (0, 0, 2))

        strong, competitive, review = digest.split_tiers(
            [dict(strong_job), dict(strong_job)])
        self.assertEqual((len(strong), len(competitive), len(review)), (2, 0, 0))


class TestTierClassification(unittest.TestCase):
    def test_enrichment_populates_each_surviving_job(self):
        jobs = [dict(REAL_JOBS[1], description=""), dict(REAL_JOBS[4], description="")]
        with mock.patch.object(digest, "get_description", side_effect=["LLM Python", "SQL cloud"]):
            digest.enrich_descriptions(jobs)
        self.assertEqual([job["description"] for job in jobs], ["LLM Python", "SQL cloud"])

    def test_only_validated_high_score_with_jd_is_strong(self):
        jobs = [
            {"id": "strong", "title": "Senior Software Engineer", "description": FULL_JD, "fit_check": CLEAN_CHECK},
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
        strong, competitive, review = digest.split_tiers(jobs)
        self.assertEqual([job["id"] for job in strong], ["strong"])
        # 79 is the top of the rubric's "competitive with gaps" band, and a
        # title-only 95 still belongs there rather than in Strong matches.
        self.assertEqual({job["id"] for job in competitive}, {"low", "no-jd"})
        self.assertEqual({job["id"] for job in review}, {"malformed"})
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
        self.assertIn("Average fit of new postings down", warning)

    def test_the_warning_names_what_it_measured_not_what_it_guessed(self):
        """It fired on 2026-09-09 saying "profile may be stale. Check
        profile.json" when the profile had just been checked and was fine. The
        average is taken over every new posting BEFORE the delivery floor, so it
        moves with what the sources send: fetched went 198 to 738 a day as the
        LinkedIn, Indeed and Bayt feeds landed and boards grew 3 to 22, while
        the best delivered score never left 88-96."""
        rows = [{"date": f"2026-07-{day:02d}", "avg_score": 85.0} for day in range(1, 8)]
        rows += [{"date": f"2026-07-{day:02d}", "avg_score": 60.0} for day in range(8, 15)]

        warning = digest.check_drift(rows)

        self.assertIn("fetched and below_floor", warning)
        self.assertNotIn("Check profile.json", warning)


class TestAMeasuredAverageSurvivesARerun(unittest.TestCase):
    """A day's average is measured once and cannot be re-measured. The second
    run of 2026-09-09 found every posting already in seen.json, reported 0 new,
    and wrote None over the ~23 the first run had measured -- corrupting the
    history check_drift reads."""

    def _row(self, work, **kwargs):
        cwd = os.getcwd()
        try:
            os.chdir(work)
            digest.save_stats(kwargs.pop("stats"), "2026-09-09", 700, 60, **kwargs)
            with open(digest.STATS_FILE, encoding="utf-8") as handle:
                return json.load(handle)[-1]
        finally:
            os.chdir(cwd)

    def test_a_rerun_with_no_new_jobs_keeps_the_measured_average(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        measured = [{"date": "2026-09-09", "avg_score": 23.4, "new": 19}]
        try:
            row = self._row(work, stats=measured, new=0, avg_score=None)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        self.assertEqual(row["avg_score"], 23.4)
        self.assertEqual(row["new"], 0, "the re-run's own counts still win")

    def test_a_first_run_with_no_new_jobs_still_records_none(self):
        """Nothing was measured, so there is nothing to keep. Inventing a number
        here would be worse than the bug."""
        work = tempfile.mkdtemp(prefix="digest_stats_")
        try:
            row = self._row(work, stats=[], new=0, avg_score=None)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        self.assertIsNone(row["avg_score"])

    def test_a_rerun_that_scored_something_overwrites_the_old_average(self):
        work = tempfile.mkdtemp(prefix="digest_stats_")
        measured = [{"date": "2026-09-09", "avg_score": 23.4, "new": 19}]
        try:
            row = self._row(work, stats=measured, new=4, avg_score=61.0)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        self.assertEqual(row["avg_score"], 61.0)


# What the last run_main() passed to check_jobs. See fake_check below.
LAST_CHECK_CALL = {}


def _board_probe_stub(probe_boards):
    """No-network stand-in for the board probe, unless a test asked for the real one."""
    if probe_boards:
        return contextlib.nullcontext()
    return mock.patch.object(digest, "_probe_employer_board", return_value=None)


def run_main(monkey_env, ranking, link_statuses=None, jobs=None, work=None,
             fit_checks=None, probe_boards=False, companies=()):
    """Run digest.main() fully mocked; return (discord_posts, workdir).

    Pass `work` from a previous call to run a second day against the seen.json
    and archives the first one wrote, which is how repeat delivery is tested.

    `fit_checks` maps a job title to the requirements result. Default runs use
    completed clean checks; explicit mappings leave unnamed jobs unverified.

    `digest.check_jobs` is patched unconditionally. No network escapes without
    it either -- `digest.requests` and `openrouter.requests` are the same module
    object, so the fake_post below already covers chat_json -- but that fake
    returns a Discord-shaped 204 with an empty body, which chat_json would fail
    to parse into a check on every strong job of every run. Patching keeps that
    noise out and, more usefully, lets a test say exactly what the reality pass
    found.

    `companies` fills the hand-curated COMPANIES list, which defaults to empty
    so a test's jobs arrive only through the aggregator. Pass it when the test
    is about the company loop itself -- the run budget rations discovered
    boards and must never ration these.

    `_probe_employer_board` is stubbed out for a blunter reason: it is the one
    call here that reaches the network with requests.get rather than
    requests.post, so nothing above covers it. Board discovery is seeded from
    matched postings, which every one of these runs has, so leaving it live sent
    five real requests to Greenhouse, Ashby, Workable, Lever and Pinpoint per
    test -- 45 seconds of suite time became six minutes, and every result
    depended on those APIs being up. Pass probe_boards=True to exercise the real
    discovery path, having patched the `_probe` underneath it.
    """
    posts = []

    def fake_check(jobs_to_check, resume_text, api_key, *args, **kwargs):
        # Recorded, not discarded. Without this nothing pins WHICH jobs get a
        # paid call: a call site changed to check_jobs(fresh, ...) would leave
        # every test green while the daily run made one model call per delivered
        # job instead of per strong-tier job.
        LAST_CHECK_CALL.clear()
        LAST_CHECK_CALL.update(titles=[j["title"] for j in jobs_to_check],
                               resume_chars=len(resume_text or ""),
                               api_key=api_key)
        for job in jobs_to_check:
            job["fit_check"] = ({"blockers": [], "unclear": [], "strengths": [],
                                 "ceiling": "high", "note": ""} if fit_checks is None else fit_checks.get(
                job["title"], fit_check_module.neutral_result("not checked in test")))

    def fake_post(url, json=None, timeout=None):
        posts.append(json["content"])
        resp = mock.Mock()
        resp.status_code = 204
        resp.text = ""
        return resp

    if work is None:
        work = tempfile.mkdtemp(prefix="digest_run_")
        for f in ("profile.json", "resume.md"):
            src = os.path.join(REPO, f)
            if os.path.exists(src):
                shutil.copy(src, work)
    supplied = REAL_JOBS if jobs is None else jobs
    blocked = digest.requests.Response()
    blocked.status_code, blocked._content, blocked._content_consumed = 404, b"", True
    blocked.url = "https://example.test"
    offline_get = (contextlib.nullcontext() if isinstance(digest.requests.get, mock.Mock)
                   else mock.patch.object(digest.requests, "get", return_value=blocked))
    old_cwd = os.getcwd()
    os.chdir(work)
    if link_statuses is None:
        link_statuses = [digest.LINK_OPEN] * len(supplied)
    try:
        with offline_get, mock.patch.dict(os.environ, monkey_env, clear=False), \
             mock.patch.object(digest, "fetch_jsearch", return_value=[dict(j) for j in supplied]), \
             mock.patch.object(digest, "COMPANIES", list(companies)), \
             mock.patch.object(digest, "verify_link", side_effect=link_statuses), \
             mock.patch.object(digest, "rank_jobs", return_value=ranking), \
             mock.patch.object(digest, "check_jobs", side_effect=fake_check),              _board_probe_stub(probe_boards), \
             mock.patch.object(digest.requests, "post", side_effect=fake_post), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.main()
    finally:
        os.chdir(old_cwd)
    return posts, work


class TestJdFetch(unittest.TestCase):
    """The aggregator recovery paths. Every response is injected; no network."""

    def _page(self, body, status=200):
        response = mock.Mock()
        response.status_code = status
        response.text = body
        return response

    def _json(self, payload, status=200):
        response = mock.Mock()
        response.status_code = status
        response.json = lambda: payload
        response.text = ""
        return response

    def test_structured_data_is_read_when_the_body_is_a_login_wall(self):
        """The case that produced "Job description needed before tailoring".

        A relister renders a sign-in prompt and publishes the whole posting in
        the JobPosting block anyway, because that is what Google indexes.
        """
        page = (
            "<html><body><h1>Sign in to view this job</h1>"
            '<script type="application/ld+json">'
            + json.dumps({"@context": "https://schema.org", "@type": "JobPosting",
                          "title": "Senior ML Engineer",
                          "description": "<p>" + FULL_JD + "</p>"})
            + "</script></body></html>"
        )

        out = jd_fetch.extract_jd("https://sa.jooble.org/jdp/-19135", get=lambda _u: self._page(page))

        self.assertIn("Your Impact", out)
        self.assertNotIn("Sign in to view", out)

    def test_a_graph_wrapper_is_walked_rather_than_assumed(self):
        page = (
            '<script type="application/ld+json">'
            + json.dumps({"@context": "https://schema.org",
                          "@graph": [{"@type": "BreadcrumbList"},
                                     {"@type": ["JobPosting"], "description": FULL_JD}]})
            + "</script>"
        )

        self.assertIn("Your Impact", jd_fetch.jsonld_job_posting(page))

    def test_an_escaped_block_is_still_parsed(self):
        page = ('<script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "description": FULL_JD}).replace('"', "&quot;")
                + "</script>")

        self.assertIn("Your Impact", jd_fetch.jsonld_job_posting(page))

    def test_the_ats_the_aggregator_relisted_is_read_instead_of_the_aggregator(self):
        """Jooble relists an employer's Greenhouse posting and links to it.

        The employer's API is the posting; the relister's page is a rendering
        of it, usually a partial one. So the hop outranks the page body.
        """
        page = ('<html><body><p>Apply on the company site.</p>'
                '<a href="https://boards.greenhouse.io/examplelabs/jobs/4412">Apply</a>'
                "</body></html>")

        def get(url):
            if "greenhouse" in url:
                return self._json({"content": "&lt;p&gt;" + FULL_JD + "&lt;/p&gt;"})
            return self._page(page)

        out = jd_fetch.extract_jd("https://sa.jooble.org/jdp/-771", get=get)

        self.assertIn("Your Impact", out)
        self.assertNotIn("Apply on the company site", out)

    def test_an_ats_url_is_read_even_when_the_page_itself_never_loads(self):
        """A 403 on the landing page does not have to end the attempt: the URL
        already names the board and the posting."""
        def get(url):
            if "api.lever.co" in url:
                return self._json([{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                    "descriptionPlain": FULL_JD}])
            return self._page("blocked", status=403)

        out = jd_fetch.extract_jd(
            "https://jobs.lever.co/examplelabs/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", get=get)

        self.assertIn("Your Impact", out)

    def test_a_bot_blocked_page_still_yields_nothing(self):
        """Unchanged behaviour, and the point of the completeness gate: chrome
        that reads as a description would score and tailor on noise."""
        page = "<html><body><nav>Jobs Companies Salaries</nav><p>Access denied.</p></body></html>"

        self.assertEqual(jd_fetch.extract_jd("https://x.test/1", get=lambda _u: self._page(page)), "")

    def test_a_teaser_from_structured_data_is_refused_like_any_other(self):
        page = ('<script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "description": "We are hiring an engineer."})
                + "</script>")

        self.assertEqual(jd_fetch.extract_jd("https://x.test/2", get=lambda _u: self._page(page)), "")

    def test_a_network_failure_returns_empty_rather_than_raising(self):
        def get(_url):
            raise digest.requests.ConnectionError("down")

        self.assertEqual(jd_fetch.extract_jd("https://x.test/3", get=get), "")

    def test_fetch_posting_page_routes_through_the_shared_extractor(self):
        job = {"id": "1", "title": "t", "company": "c", "description": "",
               "source": "jsearch", "token": "", "url": "https://x.test/4"}
        page = ('<script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "description": FULL_JD}) + "</script>")

        with mock.patch.object(digest, "_get", return_value=self._page(page)):
            self.assertIn("Your Impact", digest.get_description(job))


class TestBoundedFetch(unittest.TestCase):
    """The byte cap on third-party page reads (jd_fetch.bounded_get /
    BoundedResponse). rescue_apply_link and verify_link hand these to URLs
    JSearch relayed from LinkedIn, Indeed, Glassdoor and the like -- pages this
    project does not control and cannot bound the size of any other way."""

    def _raw_response(self, body: bytes, status=200, url="https://x.test/page",
                       history=(), encoding="utf-8"):
        """A stand-in for the requests.Response bounded_get streams from."""
        response = mock.Mock()
        response.status_code = status
        response.url = url
        response.history = list(history)
        response.headers = {}
        response.encoding = encoding
        response.close = mock.Mock()

        def iter_content(chunk_size):
            for i in range(0, len(body), chunk_size):
                yield body[i:i + chunk_size]

        response.iter_content = iter_content
        return response

    def test_a_normal_page_is_returned_whole(self):
        body = "hello job seeker".encode("utf-8")
        raw = self._raw_response(body, status=200)

        with mock.patch.object(jd_fetch.requests, "get", return_value=raw):
            out = jd_fetch.bounded_get("https://x.test/page")

        self.assertFalse(out.truncated)
        self.assertEqual(out.text, "hello job seeker")
        self.assertEqual(out.status_code, 200)
        raw.close.assert_called_once()

    def test_an_oversized_page_is_truncated_rather_than_read_whole(self):
        # A slow-drip body that never ends on its own -- ignores the chunk size
        # bounded_get asks for and hands back small chunks forever. If the loop
        # in bounded_get did not break once the limit was reached, this test
        # would hang rather than fail: the generator never raises StopIteration.
        raw = self._raw_response(b"", status=200)

        def iter_content(_chunk_size):
            while True:
                yield b"x" * 256

        raw.iter_content = iter_content

        with mock.patch.object(jd_fetch.requests, "get", return_value=raw):
            out = jd_fetch.bounded_get("https://x.test/page", limit=1024)

        self.assertTrue(out.truncated)
        self.assertEqual(len(out.content), 1024)
        self.assertEqual(len(out.text), 1024)
        raw.close.assert_called_once()

    def test_a_json_body_still_parses(self):
        payload = {"job_id": "1", "job_title": "Engineer"}
        raw = self._raw_response(json.dumps(payload).encode("utf-8"))

        with mock.patch.object(jd_fetch.requests, "get", return_value=raw):
            out = jd_fetch.bounded_get("https://x.test/page")

        self.assertEqual(out.json(), payload)

        broken = self._raw_response(b"{not valid json")
        with mock.patch.object(jd_fetch.requests, "get", return_value=broken):
            bad_out = jd_fetch.bounded_get("https://x.test/page")

        with self.assertRaises(ValueError):
            bad_out.json()

    def test_url_and_history_survive_for_verify_link(self):
        """verify_link reads .url and .history to detect a posting redirected
        off its own page -- both have to come through unchanged."""
        redirect_hop = mock.Mock()
        raw = self._raw_response(b"ok", url="https://x.test/final-landing",
                                  history=[redirect_hop])

        with mock.patch.object(jd_fetch.requests, "get", return_value=raw):
            out = jd_fetch.bounded_get("https://x.test/page")

        self.assertEqual(out.url, "https://x.test/final-landing")
        self.assertEqual(out.history, [redirect_hop])

    def test_a_truncated_page_gives_up_a_description_or_nothing_but_never_raises(self):
        """Simulates MAX_RESPONSE_BYTES cutting a posting page off mid-block:
        the JobPosting JSON never closes, so it can't parse as JSON, and the
        surrounding text is short of everything jd_completeness_failures wants.
        extract_posting must degrade to no description, not raise."""
        cut_off = (
            "<html><body><h1>Senior Backend Engineer</h1>"
            '<script type="application/ld+json">'
            + json.dumps({"@type": "JobPosting", "description": FULL_JD})[:60]
        )
        raw = self._raw_response(cut_off.encode("utf-8"))
        raw_content = cut_off.encode("utf-8")
        bounded = jd_fetch.BoundedResponse(raw, raw_content, truncated=True)

        result = jd_fetch.extract_posting("https://x.test/job", get=lambda _u: bounded)

        if result["text"]:
            self.assertEqual(job_archive.jd_completeness_failures(result["text"]), [])
        else:
            self.assertEqual(result["text"], "")


class TestJsearchApplyLinks(unittest.TestCase):
    """Choosing a free apply link out of whatever shape JSearch returns.

    The digest calls /search-v2, whose field names are not fully verified in
    this repository -- fetch_jsearch's docstring lists the ones checked live and
    apply_options is not among them. These tests pin the behaviour for the
    expected name, an alternative name, an unrecognised name that still says it
    carries apply links, a link list that does not, and absence.
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
        self.assertEqual(digest.best_free_link(options, "Acme"),
                         "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_an_ats_link_for_another_employer_is_refused(self):
        """The same rule record_apply_link applies to a link read off a page.

        Both paths write apply_url, which is the link on the card and the URL a
        later Tailor CV click re-reads the JD from, so a posting at another
        company would be tailored against. verify_link cannot catch it: the
        other company's posting is genuinely open.
        """
        options = [{"publisher": "Greenhouse",
                    "url": "https://job-boards.greenhouse.io/othercorp/jobs/7"}]
        self.assertEqual(digest.best_free_link(options, "Acme"), "")

    def test_a_free_link_that_is_not_an_ats_posting_is_kept(self):
        # Only an ATS link carries a token to check. An employer careers page or
        # another free board is accepted on the same terms as before.
        options = [{"publisher": "Acme", "url": "https://acme.test/careers/7"}]
        self.assertEqual(digest.best_free_link(options, "Acme"),
                         "https://acme.test/careers/7")

    def test_every_option_paywalled_yields_no_link(self):
        _, options = digest.jsearch_apply_options({
            "apply_options": [
                {"publisher": "Jobleads", "apply_link": "https://www.jobleads.com/job/1"},
                {"publisher": "Jooble", "apply_link": "https://sa.jooble.org/jdp/2"},
            ],
        })
        self.assertEqual(digest.best_free_link(options), "")

    def test_an_unexpected_apply_field_name_is_still_found(self):
        # The scan is why this design does not depend on a field name nothing in
        # this repository has verified against /search-v2. It is limited to
        # fields that name themselves apply fields; see the test below.
        field, options = digest.jsearch_apply_options({
            "employer_name": "Acme",
            "apply_destinations": [
                {"name": "Lever", "url": "https://jobs.lever.co/acme/abc"},
            ],
        })
        self.assertEqual(field, "apply_destinations")
        self.assertEqual(digest.best_free_link(options, "Acme"),
                         "https://jobs.lever.co/acme/abc")

    def test_a_link_list_that_is_not_an_apply_field_is_ignored(self):
        """An unrestricted scan took any list of link-bearing dicts anywhere in
        the response -- a related-postings rail, a publisher list -- and handed
        whatever it found to the candidate as the apply link."""
        field, options = digest.jsearch_apply_options({
            "employer_name": "Acme",
            "related_jobs": [
                {"name": "Othercorp", "url": "https://jobs.lever.co/othercorp/xyz"},
            ],
        })
        self.assertEqual((field, options), ("", []))

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
        ], "Acme"), "https://jobs.ashbyhq.com/acme/1")

    def test_a_malformed_payload_never_raises(self):
        for payload in (None, [], "text", {"apply_options": "not-a-list"},
                        {"apply_options": [None, 3, "x"]}):
            field, options = digest.jsearch_apply_options(payload)
            self.assertEqual((field, options), ("", []))

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
        response.headers = {}   # a real response always carries them; none needed here
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
        response.headers = {}   # a real response always carries them; none needed here
        response.json.return_value = payload
        with mock.patch.object(digest.requests, "get", return_value=response), \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            jobs = digest.fetch_jsearch("key")
        # Refused outright now: no free alternative and a paywalled primary link
        # is exactly the posting fetch_jsearch stopped carrying.
        self.assertEqual(jobs, [])


class TestApplyLinkRescue(unittest.TestCase):
    """The link the candidate is handed, when the run found a better one.

    Shares TestJdFetch's page/json helpers by construction rather than
    inheritance: these are about the URL, not the description.
    """

    def _page(self, body, status=200):
        response = mock.Mock()
        response.status_code = status
        response.text = body
        return response

    def _json(self, payload, status=200):
        response = mock.Mock()
        response.status_code = status
        response.json = lambda: payload
        response.text = ""
        return response

    def _linkedin_page(self, href):
        return ('<html><body><h1>Sign in to view this job</h1>'
                f'<a href="{href}">Apply on company website</a></body></html>')

    def test_a_linkedin_card_is_rescued_to_the_employers_own_posting(self):
        """65 of the 178 archived links were LinkedIn. The hop to the employer
        was already being made for the description and thrown away."""
        job = {"id": "1", "title": "Senior ML Engineer", "company": "Example Labs",
               "description": "", "source": "jsearch", "token": "",
               "url": "https://sa.linkedin.com/jobs/view/senior-ml-engineer-4442"}
        page = self._linkedin_page("https://boards.greenhouse.io/examplelabs/jobs/4412")

        def get(url):
            if "greenhouse" in url:
                return self._json({"content": "&lt;p&gt;" + FULL_JD + "&lt;/p&gt;"})
            return self._page(page)

        with mock.patch.object(digest, "_get", side_effect=get):
            digest.get_description(job)

        self.assertEqual(job["apply_url"],
                         "https://job-boards.greenhouse.io/examplelabs/jobs/4412")
        self.assertEqual(digest.apply_link(job), job["apply_url"])

    def test_the_rescue_happens_even_when_the_description_came_from_elsewhere(self):
        """A page with usable JSON-LD returns before the ATS hop is needed for
        text. The employer's link is still worth having."""
        job = {"id": "1", "title": "t", "company": "Example Labs", "description": "",
               "source": "jsearch", "token": "",
               "url": "https://sa.linkedin.com/jobs/view/x-1"}
        page = (self._linkedin_page("https://jobs.lever.co/examplelabs/"
                                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
                + '<script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "description": FULL_JD})
                + "</script>")

        with mock.patch.object(digest, "_get", return_value=self._page(page)):
            text = digest.get_description(job)

        self.assertIn("Your Impact", text)
        self.assertEqual(job["apply_url"],
                         "https://jobs.lever.co/examplelabs/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_a_page_that_names_no_employer_board_leaves_the_link_alone(self):
        """LinkedIn blocks datacenter IPs, so most of these fetches fail. The
        job keeps its original link and nothing else changes."""
        job = {"id": "1", "title": "t", "company": "c", "description": "",
               "source": "jsearch", "token": "",
               "url": "https://sa.linkedin.com/jobs/view/x-2"}

        with mock.patch.object(digest, "_get", return_value=self._page("blocked", status=999)):
            digest.get_description(job)

        self.assertNotIn("apply_url", job)
        self.assertEqual(digest.apply_link(job), "https://sa.linkedin.com/jobs/view/x-2")

    def test_a_posting_already_on_the_employers_board_is_not_rewritten(self):
        """The URL resolves to itself, so there is nothing to rescue and no
        apply_url to explain in the log."""
        job = {"id": "1", "title": "t", "company": "c", "description": "",
               "source": "jsearch", "token": "",
               "url": "https://job-boards.greenhouse.io/examplelabs/jobs/4412"}

        def get(_url):
            return self._json({"content": "&lt;p&gt;" + FULL_JD + "&lt;/p&gt;"})

        with mock.patch.object(digest, "_get", side_effect=get):
            digest.get_description(job)

        self.assertNotIn("apply_url", job)

    def test_the_snapshot_and_the_digest_line_both_carry_the_rescued_link(self):
        """Two delivery paths read the link independently; a rescue that only
        reached one of them would ship a paid link on the other."""
        job = {"id": "1", "title": "t", "company": "c", "location": "Riyadh",
               "source": "jsearch", "description": FULL_JD, "score": 80,
               "url": "https://sa.linkedin.com/jobs/view/x-3",
               "apply_url": "https://apply.workable.com/foodics/j/E2707EDFCE/"}

        snapshot = job_archive.make_snapshot(job, "2026-08-29T00:00:00Z")

        self.assertEqual(snapshot["url"], "https://apply.workable.com/foodics/j/E2707EDFCE/")
        self.assertEqual(digest.apply_link(job), snapshot["url"])

    def test_a_link_naming_another_employer_is_refused(self):
        """`ats_reference` matches any ATS href on the page, and a relister's
        'people also viewed' rail is full of them. Nothing downstream catches a
        wrong one: verify_link confirms it open because it IS a live posting,
        and the snapshot hands it to the tailoring run as the JD to read."""
        job = {"id": "1", "title": "Senior ML Engineer", "company": "Example Labs",
               "description": "", "source": "jsearch", "token": "",
               "url": "https://sa.linkedin.com/jobs/view/x-4"}
        page = ('<html><body>'
                '<script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "description": FULL_JD})
                + '</script><aside>People also viewed'
                '<a href="https://jobs.lever.co/othercorp/'
                'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">Other role</a>'
                '</aside></body></html>')

        with mock.patch.object(digest, "_get", return_value=self._page(page)):
            text = digest.get_description(job)

        self.assertIn("Your Impact", text)  # the JD itself was still right
        self.assertNotIn("apply_url", job)
        self.assertEqual(digest.apply_link(job), "https://sa.linkedin.com/jobs/view/x-4")

    def test_an_aggregator_job_that_already_has_its_text_is_still_rescued(self):
        """JSearch always ships a description, so this early return is the path
        every aggregator posting takes. The rescue reached none of the 171
        archived ones while it lived only on the description-less branch."""
        job = {"id": "1", "title": "Senior ML Engineer", "company": "Example Labs",
               "source": "jsearch", "token": "", "description": FULL_JD,
               "url": "https://sa.linkedin.com/jobs/view/x-5"}
        page = self._linkedin_page("https://boards.greenhouse.io/examplelabs/jobs/4412")

        with mock.patch.object(digest, "_get", return_value=self._page(page)):
            text = digest.get_description(job)

        self.assertIn("Your Impact", text)
        self.assertEqual(job["apply_url"],
                         "https://job-boards.greenhouse.io/examplelabs/jobs/4412")

    def test_a_blocked_relister_page_says_so_in_the_log(self):
        """Glassdoor, Jooble and learn4good all answered 403 to a live probe on
        2026-08-29. A silent skip makes a rescue that found nothing look exactly
        like one that never ran, which is the whole question when reading a run
        log to judge whether the rescue is working."""
        job = {"id": "1", "title": "Senior ML Engineer", "company": "Raqmiyat",
               "source": "jsearch", "token": "", "description": FULL_JD,
               "url": "https://www.glassdoor.co.uk/job-listing/ai-engineer-raqmiyat.htm"}
        lines = []

        with mock.patch.object(digest, "_get", return_value=self._page("denied", status=403)), \
             mock.patch.object(digest, "log", lines.append):
            digest.rescue_apply_link(job)

        self.assertNotIn("apply_url", job)
        self.assertTrue(any("HTTP 403" in line and "glassdoor.co.uk" in line
                            for line in lines), lines)

    def test_a_job_read_from_an_employers_own_board_costs_no_lookup(self):
        """Its URL is already the answer, so the extra GET would buy nothing."""
        job = {"id": "1", "title": "t", "company": "Foodics", "source": "workable",
               "token": "foodics", "description": FULL_JD,
               "url": "https://apply.workable.com/foodics/j/E2707EDFCE/"}

        def boom(*_args, **_kwargs):
            raise AssertionError("no request should be made here")

        with mock.patch.object(digest, "_get", side_effect=boom):
            digest.get_description(job)

        self.assertNotIn("apply_url", job)

    def test_a_board_token_is_matched_against_the_employer_it_claims(self):
        self.assertTrue(digest.employer_matches_token("Example Labs", "examplelabs"))
        self.assertTrue(digest.employer_matches_token("Lean", "LeanTech"))
        self.assertTrue(digest.employer_matches_token("Qiddiya | القدية", "qiddiya"))
        self.assertFalse(digest.employer_matches_token("Example Labs", "othercorp"))
        # A one-letter name would otherwise be a substring of every token.
        self.assertFalse(digest.employer_matches_token("c", "examplelabs"))
        self.assertFalse(digest.employer_matches_token("Example Labs", ""))

    def test_an_eu_hosted_greenhouse_board_keeps_its_region(self):
        """ATS_PATTERNS matches the EU host on purpose. Rebuilding it on the
        global one 404s, and the rescue is then thrown away in silence."""
        reference = jd_fetch.ats_reference(
            "", "https://job-boards.eu.greenhouse.io/tamara/jobs/4412")

        self.assertEqual(jd_fetch.ats_url(reference),
                         "https://job-boards.eu.greenhouse.io/tamara/jobs/4412")

    def test_ats_url_builds_every_supported_board_and_refuses_the_rest(self):
        build = jd_fetch.ats_url
        self.assertEqual(build({"source": "workable", "token": "foodics", "id": "E27"}),
                         "https://apply.workable.com/foodics/j/E27/")
        self.assertEqual(build({"source": "pinpoint", "token": "tabby", "id": "d80"}),
                         "https://tabby.pinpointhq.com/postings/d80")
        self.assertEqual(build({"source": "ashby", "token": "LeanTech", "id": "abc"}),
                         "https://jobs.ashbyhq.com/LeanTech/abc")
        self.assertEqual(build(None), "")
        self.assertEqual(build({"source": "taleo", "token": "x", "id": "y"}), "")
        self.assertEqual(build({"source": "lever", "token": "", "id": "y"}), "")


class TestJsearchPaging(unittest.TestCase):
    """Spending a fixed monthly request allowance across the day's queries.

    The free plan allows 200 requests per month, so this digest reads only a
    bounded, fair rotation of page-one queries. Cursor pages remain disabled.
    """

    def _page(self, ids, cursor="", remaining="150"):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {"x-ratelimit-requests-limit": "200",
                            "x-ratelimit-requests-remaining": remaining}
        response.json.return_value = {"data": {
            "cursor": cursor,
            "jobs": [{"job_id": i, "job_title": "Data Engineer", "employer_name": "Acme",
                      "job_city": "Riyadh", "job_country": "SA",
                      "job_apply_link": f"https://acme.test/{i}",
                      "job_description": "Build pipelines."} for i in ids],
        }}
        return response

    def test_the_default_budget_selects_two_rotating_page_one_queries(self):
        self.assertEqual(digest._request_budget(None), 2)

    def test_zero_disables_jsearch_and_out_of_range_falls_back_to_two(self):
        self.assertEqual(digest._request_budget("0"), 0)
        self.assertEqual(digest._request_budget("4"), 4)
        self.assertEqual(digest._request_budget(str(len(digest.AGGREGATOR_QUERIES) + 1)), 2)

    def test_expanding_search_targets_does_not_expand_the_daily_request_budget(self):
        for raw in ("7", "12"):
            with self.subTest(raw=raw), \
                 mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", digest._request_budget(raw)), \
                 mock.patch.object(digest.requests, "get", return_value=self._page([])) as get, \
                 mock.patch.object(digest.time, "sleep", lambda *_: None):
                digest.fetch_jsearch("test-key")
                self.assertEqual(get.call_count, 2)

    def test_two_queries_rotate_without_a_second_page(self):
        queries = ["a", "b", "c", "d"]
        self.assertEqual(digest.jsearch_query_plan(queries, 2, day=0), ["a", "b"])
        self.assertEqual(digest.jsearch_query_plan(queries, 2, day=1), ["c", "d"])

    def test_two_rotating_queries_never_fetch_a_second_page(self):
        pages = [self._page(["1"], cursor="CUR"), self._page(["2"], cursor="CUR")]
        with mock.patch.object(digest.requests, "get", side_effect=pages) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["a", "b", "c", "d"]), \
             mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", 2), \
             mock.patch.object(digest.datetime, "date", wraps=datetime.date) as date, \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            date.today.return_value = datetime.date(2026, 9, 20)
            digest.fetch_jsearch("key")
        self.assertEqual(get.call_count, 2)

    def test_a_query_with_no_cursor_is_not_asked_for_a_second_page(self):
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"])]) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key")
        self.assertEqual(get.call_count, 1)

    def test_a_nearly_exhausted_quota_stops_the_next_rotating_query(self):
        """A low bootstrap response re-evaluates before the next planned call."""
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"], cursor="CUR", remaining="5")]) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer", "backend engineer"]), \
             mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", 2), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key")
        self.assertEqual(get.call_count, 1)

    def test_the_remaining_allowance_is_logged(self):
        """Nothing reported the balance, so
        the first sign of exhaustion would have been an empty digest."""
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"], remaining="162")]), \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None), \
             mock.patch.object(digest, "log") as logged:
            digest.fetch_jsearch("key")
        lines = [c.args[0] for c in logged.call_args_list if c.args]
        self.assertTrue(any("162 of 200" in line for line in lines),
                        f"no quota line in: {lines}")

    def test_known_jsearch_reserve_pauses_without_a_request(self):
        quota = {"requests_remaining": 40, "reset_at": digest.time.time() + 3600}
        with mock.patch.object(digest.requests, "get") as get:
            self.assertEqual(digest.fetch_jsearch("key", quota=quota), [])
        get.assert_not_called()

    def test_missing_jsearch_state_bootstraps_once_then_rechecks_the_reserve(self):
        quota = {}
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"], remaining="27")]) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["a", "b", "c", "d"]), \
             mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", 2), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key", quota=quota)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(quota["requests_remaining"], 27)
        self.assertIn("observed_at", quota)

    def test_bootstrap_without_quota_headers_stops_before_a_second_query(self):
        quota = {}
        response = self._page(["1"])
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["a", "b"]), \
             mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", 2), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key", quota=quota)
        self.assertEqual(get.call_count, 1)

    def test_missing_headers_debit_a_known_jsearch_balance_conservatively(self):
        quota = {"requests_remaining": 41, "reset_at": digest.time.time() + 3600}
        response = self._page(["1"])
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["a", "b"]), \
             mock.patch.object(digest, "JSEARCH_DAILY_REQUESTS", 2), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key", quota=quota)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(quota["requests_remaining"], 40)

    def test_jsearch_probe_is_one_page_and_records_its_quota(self):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {"x-ratelimit-requests-remaining": "80",
                            "x-ratelimit-requests-reset": "3600"}
        response.json.return_value = {"data": {"cursor": "unused", "jobs": []}}
        quota = {}
        with mock.patch.object(digest.requests, "get", return_value=response) as get:
            self.assertEqual(digest.probe_jsearch("data engineer", "key", quota), 0)
        get.assert_called_once()
        self.assertEqual(quota["requests_remaining"], 80)
        self.assertIn("observed_at", quota)


class TestPaywalledPostingsAreNeverFetched(unittest.TestCase):
    """A paywalled posting is refused at the source rather than carried and cut.

    It was fetched, deduped, filtered, verified, paged into the run's counts and
    logged one line each, only to be discarded every morning -- 11 of 48 on the
    3 Sep 19:33 run -- and neither rescue could ever reach it: the relisters
    answer 403 or 404 to this runner, and apply_options never carries an
    alternative. The employer is still worth keeping, because that is what board
    discovery converts.
    """

    def _payload(self, link, title="Senior Data Engineer", options=None):
        job = {"job_id": "abc", "job_title": title, "employer_name": "Acme",
               "job_city": "Riyadh", "job_country": "SA", "job_apply_link": link,
               "employer_website": "https://acme.test",
               "job_description": "Build pipelines."}
        if options:
            job["apply_options"] = options
        return {"data": {"jobs": [job]}}

    def _fetch(self, payload, paywalled=None):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = payload
        with mock.patch.object(digest.requests, "get", return_value=response),              mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]),              mock.patch.object(digest.time, "sleep", lambda *_: None):
            return digest.fetch_jsearch("key", paywalled)

    def test_a_paywalled_posting_never_enters_the_run(self):
        self.assertEqual(self._fetch(self._payload("https://www.jobleads.com/job/1")), [])

    def test_a_free_posting_is_kept(self):
        jobs = self._fetch(self._payload("https://sa.linkedin.com/jobs/view/1"))
        self.assertEqual(len(jobs), 1)

    def test_a_paywalled_posting_with_a_free_alternative_is_kept(self):
        """The API rescue still decides this, and still runs before the drop."""
        jobs = self._fetch(self._payload(
            "https://www.jobleads.com/job/1",
            options=[{"publisher": "Greenhouse",
                      "apply_link": "https://job-boards.greenhouse.io/acme/jobs/7"}]))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["apply_url"], "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_a_dropped_posting_that_matches_is_handed_to_the_caller(self):
        paywalled = []
        self._fetch(self._payload("https://www.jobleads.com/job/1"), paywalled)
        self.assertEqual(len(paywalled), 1)
        self.assertEqual(paywalled[0]["company"], "Acme")
        self.assertEqual(paywalled[0]["employer_website"], "https://acme.test")

    def test_the_refusal_is_reported_with_the_hosts_responsible(self):
        """A silent drop reads exactly like a quiet market. The count and the
        hosts are what say which one a thin morning was."""
        paywalled = []
        with mock.patch.object(digest, "log") as logged:
            self._fetch(self._payload("https://www.jobleads.com/job/1"), paywalled)
        lines = [c.args[0] for c in logged.call_args_list if c.args]
        self.assertTrue(any("refused 1 paywalled" in line and "jobleads.com" in line
                            for line in lines), f"no refusal line in: {lines}")

    def test_a_dropped_posting_that_the_filters_reject_is_not(self):
        """Board discovery costs five ATS requests an employer. Spending them on
        a role this candidate would never be shown is the waste the filters
        exist to prevent."""
        paywalled = []
        self._fetch(self._payload("https://www.jobleads.com/job/1",
                                  title="Registered Nurse"), paywalled)
        self.assertEqual(paywalled, [])


class TestBoardDiscovery(unittest.TestCase):
    """Finding an employer's own board for a posting stuck behind a paid relister.

    The relisters block this runner outright -- 403 from jooble and jobrapido,
    404 from jobleads, measured 3 Sep 2026 -- and JSearch's apply_options never
    carries an alternative, so neither existing rescue can reach these postings.
    What the response does carry is the employer's name and, most of the time,
    a website. Probed against the five supported ATSs those are a route to the
    employer that never touches the relister.

    employer_website is Google's knowledge-panel guess, not a verified field:
    it gave siccode.com for "Business Codes" and infinitiusa.com for
    "Infinniti" on 3 Sep. So a hit is only believed when the board token agrees
    with the employer name, which is the same test record_apply_link applies.
    """

    def _paid_job(self, company, site="", url="https://www.jobleads.com/job/1"):
        return {"company": company, "id": "jsearch_1", "title": "Data Engineer",
                "location": "Riyadh, Saudi Arabia", "source": "jsearch", "token": "",
                "url": url, "employer_website": site, "description": ""}

    def test_a_website_that_repeats_the_name_adds_no_candidate(self):
        job = self._paid_job("Eram Talent", "https://www.eramtalent.com/careers")
        self.assertEqual(digest.board_token_candidates(job), ["eramtalent", "eram"])

    def test_a_website_that_differs_from_the_name_gives_a_second_candidate(self):
        job = self._paid_job("Lean", "https://leantech.io")
        self.assertEqual(digest.board_token_candidates(job), ["lean", "leantech"])

    def test_a_job_with_no_website_still_offers_its_name(self):
        self.assertEqual(digest.board_token_candidates(self._paid_job("Nabla")), ["nabla"])

    def test_the_first_word_of_a_multi_word_employer_is_offered(self):
        """The live probe on 3 Sep found exequt MENA on Workable under 'exequt'
        and Silver Edge Arabia under 'silveredge'. A board token is the short
        name, not the trading name, so the joined form alone would miss both."""
        self.assertEqual(digest.board_token_candidates(self._paid_job("exequt MENA")),
                         ["exequtmena", "exequt"])

    def test_a_confirmed_board_is_recorded(self):
        boards = {}
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "ashby", 15)):
            digest.discover_boards([self._paid_job("Nabla")], boards, "2026-09-03")
        self.assertEqual(boards["nabla"]["source"], "ashby")
        self.assertEqual(boards["nabla"]["token"], "nabla")
        self.assertEqual(boards["nabla"]["roles"], 15)
        self.assertEqual(boards["nabla"]["found"], "2026-09-03")

    def test_a_dormant_board_is_recorded_too(self):
        """COMPANIES already carries seven confirmed-but-empty Workable accounts.
        A board with no roles today is the same thing: confirmed, and worth
        reading on the day it posts."""
        boards = {}
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "workable", 0)):
            digest.discover_boards([self._paid_job("exequt MENA")], boards, "2026-09-03")
        self.assertEqual(boards["exequtmena"]["source"], "workable")
        self.assertEqual(boards["exequtmena"]["roles"], 0)

    def test_a_board_the_employer_name_cannot_vouch_for_is_refused(self):
        """employer_website gave siccode.com for "Business Codes". Believing it
        would put another company's postings in the digest under this name."""
        boards = {}
        job = self._paid_job("Business Codes", "https://siccode.com")
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (token == "siccode", 9)):
            digest.discover_boards([job], boards, "2026-09-03")
        self.assertNotIn("source", boards.get("businesscodes", {}))
        self.assertEqual(boards["businesscodes"].get("miss"), "2026-09-03")

    def test_a_board_whose_own_name_is_another_company_is_refused(self):
        """Live on 3 Sep 2026 the first-word token 'latitude' reached a
        greenhouse board of automotive service technicians in Pittsburgh, and
        'silver' reached an Argentine recruiter on ashby. Containment against
        the employer name is not enough on its own: the board has to say who it
        belongs to."""
        boards = {}
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "greenhouse", 47)),              mock.patch.object(digest, "board_identity", return_value="Wheelworks Auto"):
            digest.discover_boards([self._paid_job("Latitude Systems")], boards, "2026-09-03")
        self.assertEqual(boards["latitudesystems"].get("miss"), "2026-09-03")

    def test_a_board_whose_own_name_is_this_company_is_accepted(self):
        boards = {}
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "workable", 0)),              mock.patch.object(digest, "board_identity", return_value="exequt MENA"):
            digest.discover_boards([self._paid_job("exequt MENA")], boards, "2026-09-03")
        self.assertEqual(boards["exequtmena"]["source"], "workable")

    def test_a_board_that_publishes_no_name_needs_the_employers_whole_name(self):
        """Ashby's posting API reports no board name at all, so nothing can
        corroborate a guess there. A shortened token is refused; the employer's
        full key is not a guess."""
        boards = {}
        # Only the shortened token exists on ashby, which is exactly the live case.
        short = lambda source, token: (source == "ashby" and token == "silver", 40)
        with mock.patch.object(digest, "_probe", side_effect=short),              mock.patch.object(digest, "board_identity", return_value=""):
            digest.discover_boards([self._paid_job("Silver Edge Arabia")], boards, "2026-09-03")
        self.assertEqual(boards["silveredgearabia"].get("miss"), "2026-09-03")

        boards = {}
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "ashby", 40)),              mock.patch.object(digest, "board_identity", return_value=""):
            digest.discover_boards([self._paid_job("Nabla")], boards, "2026-09-03")
        self.assertEqual(boards["nabla"]["source"], "ashby")

    def test_a_miss_is_not_probed_again_the_next_day(self):
        boards = {"creditninja": {"miss": "2026-09-03"}}
        job = self._paid_job("CreditNinja")
        with mock.patch.object(digest, "_probe") as probe:
            digest.discover_boards([job], boards, "2026-09-04")
        probe.assert_not_called()

    def test_a_stale_miss_is_probed_again(self):
        """A company that had no board in September may have one in December."""
        boards = {"creditninja": {"miss": "2026-09-03"}}
        job = self._paid_job("CreditNinja")
        with mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "lever", 3)):
            digest.discover_boards([job], boards, "2026-12-03")
        self.assertEqual(boards["creditninja"]["source"], "lever")

    def test_a_known_board_is_never_reprobed(self):
        boards = {"nabla": {"source": "ashby", "token": "nabla", "roles": 15,
                            "found": "2026-09-03"}}
        with mock.patch.object(digest, "_probe") as probe:
            digest.discover_boards([self._paid_job("Nabla")], boards, "2026-09-04")
        probe.assert_not_called()

    def test_the_number_of_employers_probed_in_one_run_is_capped(self):
        """A day the aggregator floods the cut must not spend hundreds of
        requests. The rest are probed on later runs; nothing is lost, because a
        paid-relister drop is never remembered and comes back tomorrow."""
        jobs = [self._paid_job(f"Company{n}") for n in range(digest.MAX_BOARD_PROBES + 4)]
        boards = {}
        with mock.patch.object(digest, "_probe", return_value=(False, 0)):
            digest.discover_boards(jobs, boards, "2026-09-03")
        self.assertEqual(len(boards), digest.MAX_BOARD_PROBES)

    def test_one_employer_is_probed_once_however_many_jobs_it_has(self):
        jobs = [self._paid_job("Nabla"), self._paid_job("Nabla")]
        boards = {}
        with mock.patch.object(digest, "_probe", return_value=(False, 0)) as probe:
            digest.discover_boards(jobs, boards, "2026-09-03")
        self.assertEqual(probe.call_count, len(digest.PROBES))

    def test_confirmed_boards_come_back_shaped_like_a_COMPANIES_entry(self):
        boards = {"nabla": {"company": "Nabla", "source": "ashby", "token": "nabla",
                            "roles": 15, "found": "2026-09-03"},
                  "creditninja": {"miss": "2026-09-03"}}
        self.assertEqual(digest.confirmed_boards(boards),
                         [{"name": "Nabla", "source": "ashby", "token": "nabla"}])

    def test_a_board_already_in_COMPANIES_is_not_fetched_twice(self):
        boards = {"salla": {"company": "Salla", "source": "workable", "token": "salla",
                            "roles": 2, "found": "2026-09-03"}}
        with mock.patch.object(digest, "COMPANIES",
                               [{"name": "Salla", "source": "workable", "token": "salla"}]):
            self.assertEqual(digest.confirmed_boards(boards), [])

    def test_fetch_jsearch_records_the_employer_website(self):
        """The field the discovery reads. 7 of 10 jobs carried one on 3 Sep."""
        payload = {"data": {"jobs": [{
            "job_id": "abc", "job_title": "Data Engineer", "employer_name": "Nabla",
            "job_city": "Riyadh", "job_country": "SA",
            "job_apply_link": "https://sa.linkedin.com/jobs/view/1",
            "employer_website": "https://nabla.com",
            "job_description": "Build pipelines.",
        }]}}
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}   # a real response always carries them; none needed here
        response.json.return_value = payload
        with mock.patch.object(digest.requests, "get", return_value=response), \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            jobs = digest.fetch_jsearch("key")
        self.assertEqual(jobs[0]["employer_website"], "https://nabla.com")

    def test_boards_survive_a_save_and_load(self):
        work = tempfile.mkdtemp(prefix="digest_boards_")
        old = os.getcwd()
        os.chdir(work)
        try:
            self.assertEqual(digest.load_boards(), {})
            digest.save_boards({"nabla": {"source": "ashby", "token": "nabla"}})
            self.assertEqual(digest.load_boards()["nabla"]["source"], "ashby")
        finally:
            os.chdir(old)
            shutil.rmtree(work)


class TestPerCompanyCap(unittest.TestCase):
    """One employer must not be able to fill a morning's cards on its own.

    Board discovery works: Innovationteam, found on 3 Sep 2026, supplied 9 of
    that evening's 12 delivered cards and had 7 more roles dropped below the
    floor. split_tiers sorts on score alone and MAX_JOBS_IN_DIGEST is the only
    cap, so a board that posts a batch crowds every other employer out.
    """

    def _jobs(self, *pairs):
        return [{"id": f"j{i}", "company": company, "score": score,
                 "title": f"Engineer {i}", "url": f"https://x.test/{i}"}
                for i, (company, score) in enumerate(pairs)]

    def test_only_the_best_three_of_one_employer_are_kept(self):
        jobs = self._jobs(("Innovationteam", 95), ("Innovationteam", 88),
                          ("Innovationteam", 84), ("Innovationteam", 82),
                          ("Innovationteam", 68))
        kept, deferred = digest.cap_per_company(jobs)
        self.assertEqual([j["score"] for j in kept], [95, 88, 84])
        self.assertEqual(sorted(j["score"] for j in deferred), [68, 82])

    def test_other_employers_are_untouched(self):
        jobs = self._jobs(("Innovationteam", 95), ("Innovationteam", 88),
                          ("Innovationteam", 84), ("Innovationteam", 82),
                          ("FileCloud", 52), ("Perfect Vision", 88))
        kept, _ = digest.cap_per_company(jobs)
        self.assertIn("FileCloud", [j["company"] for j in kept])
        self.assertIn("Perfect Vision", [j["company"] for j in kept])

    def test_an_employer_under_the_cap_defers_nothing(self):
        kept, deferred = digest.cap_per_company(self._jobs(("Salla", 70), ("Salla", 60)))
        self.assertEqual(len(kept), 2)
        self.assertEqual(deferred, [])

    def test_the_same_employer_named_two_ways_counts_once(self):
        """company_key exists because boards decorate a name differently.
        Counting the decorations separately would defeat the cap."""
        jobs = self._jobs(("Zid", 90), ("Zid | زد", 85),
                          ("Zid Ltd.", 80), ("Zid", 75))
        kept, deferred = digest.cap_per_company(jobs)
        self.assertEqual(len(kept), 3)
        self.assertEqual([j["score"] for j in deferred], [75])


class TestDiscordWebhookChunks(unittest.TestCase):
    def test_long_gap_and_question_lines_fit_without_losing_text(self):
        lines = ["Job digest", "Gaps: " + "qualification " * 45 +
                 "Questions to check: " + "experience evidence " * 90, "Next role"]
        response = mock.Mock(status_code=204)
        with mock.patch.object(digest.requests, "post", return_value=response) as post, \
             mock.patch.object(digest.time, "sleep"):
            digest.post_discord("https://discord.test/webhook", lines)
        messages = [call.kwargs["json"]["content"] for call in post.call_args_list]
        self.assertTrue(all(0 < len(message) <= 1900 for message in messages))
        self.assertEqual("".join(messages).replace("\n", ""), "".join(lines))


class TestDeliveryFloor(unittest.TestCase):
    """Scores are relative to MIN_DELIVER_SCORE rather than written out, so
    moving the floor cannot silently stop these testing what they name."""

    def _jobs(self):
        floor = digest.MIN_DELIVER_SCORE
        return [
            {"id": "good", "title": "Senior AI Engineer", "description": "Python LLM",
             "company": "A", "score": floor + 5, "ranking_verified": True},
            {"id": "weak", "title": "Senior Data Engineer", "description": "SQL",
             "company": "B", "score": floor - 5, "ranking_verified": True},
            {"id": "edge", "title": "Senior Backend Engineer", "description": "Go",
             "company": "C", "score": floor, "ranking_verified": True},
        ]

    def test_the_floor_requires_strong_matches(self):
        self.assertEqual(digest.MIN_DELIVER_SCORE, digest.STRONG_MATCH_MIN_SCORE)

    def test_only_scores_under_the_floor_are_dropped(self):
        kept, dropped = digest.drop_below_floor(self._jobs())

        self.assertEqual([job["id"] for job in kept], ["good", "edge"])
        self.assertEqual([job["id"] for job in dropped], ["weak"])

    def test_a_heuristic_score_is_never_dropped(self):
        """A keyword score is what the pipeline produces when the ranker is
        down. Treating it as a verdict would turn an outage into an empty
        digest with nothing anywhere saying why."""
        jobs = [dict(job, ranking_verified=False) for job in self._jobs()]

        kept, dropped = digest.drop_below_floor(jobs)

        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), 3)

    def test_the_floor_is_configurable(self):
        # Built OUTSIDE the patch. _jobs() sizes its scores from the live floor,
        # so building them inside would raise the jobs along with the floor and
        # the test would pass whatever drop_below_floor did.
        jobs = self._jobs()
        with mock.patch.object(digest, "MIN_DELIVER_SCORE", 100):
            kept, dropped = digest.drop_below_floor(jobs)

        self.assertEqual([job["id"] for job in kept], [])
        self.assertEqual(len(dropped), 3)


class TestEndToEnd(unittest.TestCase):
    ENV = {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
           "OPENROUTER_API_KEY": "test-key",
           "GITHUB_REPOSITORY": "abdullah7041/job-search-digest"}

    LINKEDIN_JOB = {
        "company": "Example Labs", "id": "jsearch_ln",
        "title": "Senior AI Engineer", "location": "الرياض, Saudi Arabia",
        "url": "https://sa.linkedin.com/jobs/view/senior-ai-engineer-4442",
        "first_published": RECENT, "source": "jsearch", "token": "",
        "description": "",  # forces enrichment, which is where the rescue happens
    }

    def _linkedin_run(self, verify_statuses):
        """One LinkedIn card whose page links out to the employer's Greenhouse."""
        page = ('<html><body><h1>Sign in to view this job</h1>'
                '<a href="https://boards.greenhouse.io/examplelabs/jobs/4412">'
                'Apply on company website</a></body></html>')

        def fake_get(url, *_args, **_kwargs):
            response = mock.Mock()
            if "greenhouse" in url:
                response.status_code = 200
                response.text = ""
                response.json = lambda: {"content": "&lt;p&gt;" + FULL_JD + "&lt;/p&gt;"}
            else:
                response.status_code = 200
                response.text = page
            return response

        ranking = {"jsearch_ln": {"score": 88, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "_get", side_effect=fake_get):
            return run_main(self.ENV, ranking, link_statuses=verify_statuses,
                            jobs=[dict(self.LINKEDIN_JOB)])

    def test_a_linkedin_card_is_delivered_as_the_employers_own_link(self):
        """The whole point: the candidate clicks through to Greenhouse, not to
        a login wall, and the run already had that link in hand."""
        posts, work = self._linkedin_run([digest.LINK_OPEN, digest.LINK_OPEN])
        shutil.rmtree(work)
        body = "\n".join(posts)

        self.assertIn("https://job-boards.greenhouse.io/examplelabs/jobs/4412", body)
        self.assertNotIn("sa.linkedin.com", body)

    def test_source_yield_is_persisted_for_delivery_and_empty_days(self):
        posts, work = self._linkedin_run([digest.LINK_OPEN, digest.LINK_OPEN])
        try:
            with open(os.path.join(work, "stats.json")) as handle:
                row = json.load(handle)[-1]
            self.assertEqual(row["source_yield"]["jsearch"]["new"], 1)
            self.assertEqual(row["source_yield"]["jsearch"]["delivered"], 1)
            run_main(self.ENV, {}, jobs=[], work=work)
            with open(os.path.join(work, "stats.json")) as handle:
                row = json.load(handle)[-1]
            self.assertEqual(row["delivered"], 0)
            self.assertEqual(row["source_yield"]["jsearch"]["fetched"], 0)
        finally:
            shutil.rmtree(work)

    def test_source_yield_survives_a_failed_heartbeat(self):
        work = tempfile.mkdtemp(prefix="digest_failure_")
        try:
            with mock.patch.object(digest, "post_heartbeat", side_effect=RuntimeError("delivery down")):
                with self.assertRaises(RuntimeError):
                    run_main(self.ENV, {}, jobs=[], work=work)
            with open(os.path.join(work, "stats.json")) as handle:
                row = json.load(handle)[-1]
            self.assertEqual(row["delivered"], 0)
            self.assertIn("jsearch", row["source_yield"])
        finally:
            shutil.rmtree(work)

    def test_source_yield_survives_a_failed_webhook_without_claiming_delivery(self):
        work = tempfile.mkdtemp(prefix="digest_failure_")
        try:
            with mock.patch.object(digest, "post_discord", side_effect=SystemExit(1)), \
                 mock.patch.object(digest, "settle_link", side_effect=lambda job: job.update(link_status=digest.LINK_OPEN)):
                with self.assertRaises(SystemExit):
                    run_main(self.ENV, {"jsearch_ln": {"score": 65, "why": "Supported role"}},
                             jobs=[dict(self.LINKEDIN_JOB, description=FULL_JD)], work=work)
            with open(os.path.join(work, "stats.json")) as handle:
                row = json.load(handle)[-1]
            self.assertEqual(row["source_yield"]["jsearch"]["new"], 1)
            self.assertEqual(row["source_yield"]["jsearch"]["delivered"], 0)
        finally:
            shutil.rmtree(work)

    def test_the_days_row_counts_the_link_status_the_card_actually_shows(self):
        """The original link verified open and the rescued one came back
        inconclusive, so the card footer says so. The count was taken before
        the rescue was verified, and reported 0 for the day."""
        posts, work = self._linkedin_run([digest.LINK_OPEN, digest.LINK_UNKNOWN])
        with open(os.path.join(work, "stats.json")) as handle:
            row = json.load(handle)[-1]
        shutil.rmtree(work)

        self.assertEqual(row["link_unknown"], 1)

    def test_a_rescued_link_that_does_not_resolve_falls_back_to_the_original(self):
        """The rescued URL is BUILT from a token and an id, so a definite
        negative is as likely to mean the shape was wrong as that the posting
        closed. Losing the rescue must never cost the job.

        Three statuses, not two: dropping the rescue puts the job back on the
        link it arrived with, and that link is then checked in its own right --
        which is how a job that survived the paid-relister cut only because the
        rescue was free gets caught before delivery.
        """
        posts, work = self._linkedin_run(
            [digest.LINK_OPEN, digest.LINK_CLOSED, digest.LINK_OPEN])
        shutil.rmtree(work)
        body = "\n".join(posts)

        self.assertIn("sa.linkedin.com/jobs/view/senior-ai-engineer-4442", body)
        self.assertNotIn("job-boards.greenhouse.io", body)
        self.assertIn("Senior AI Engineer", body)

    def test_a_paywalled_job_with_a_free_link_is_delivered(self):
        """The paid-relister cut must judge the link the candidate is handed.

        rescue_apply_link is patched only to keep this test off the network:
        get_description calls it for every aggregator job regardless of any
        recovery loop, so its call count says nothing here. That it makes no
        second page fetch for a job that already has apply_url is a separate
        property, tested directly in
        test_a_job_that_already_has_a_free_link_is_not_fetched_twice.
        """
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz",
                   apply_url="https://job-boards.greenhouse.io/next/jobs/1")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "rescue_apply_link"):
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_OPEN, digest.LINK_OPEN])
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
        self.assertIn("0 delivered today", body)

    def test_a_dropped_employer_is_probed_and_remembered(self):
        """The whole point: the cut discards these eight a day and never
        remembers them, so without this they cost the same fruitless lookup
        every morning and the employer is never found."""
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz",
                   company="Nabla", employer_website="https://nabla.com")
        with mock.patch.object(digest, "rescue_apply_link"), \
             mock.patch.object(digest, "_probe",
                               side_effect=lambda source, token: (source == "ashby", 15)):
            posts, work = run_main(self.ENV, {}, jobs=[job], link_statuses=[],
                                   probe_boards=True)
        with open(os.path.join(work, "boards.json"), encoding="utf-8") as handle:
            boards = json.load(handle)
        self.assertEqual(boards["nabla"]["source"], "ashby")
        self.assertEqual(boards["nabla"]["token"], "nabla")

    def test_a_discovered_board_is_read_on_the_next_run(self):
        """A board found yesterday has to actually be fetched today, or the
        discovery is a log line and nothing more."""
        work = tempfile.mkdtemp(prefix="digest_board_")
        for f in ("profile.json", "resume.md"):
            src = os.path.join(REPO, f)
            if os.path.exists(src):
                shutil.copy(src, work)
        with open(os.path.join(work, "boards.json"), "w", encoding="utf-8") as handle:
            json.dump({"nabla": {"company": "Nabla", "source": "ashby",
                                 "token": "nabla", "roles": 15,
                                 "found": "2026-09-03"}}, handle)
        with mock.patch.object(digest, "fetch_company", return_value=[]) as fetch:
            run_main(self.ENV, {}, jobs=[], link_statuses=[], work=work)
        shutil.rmtree(work)
        self.assertIn({"name": "Nabla", "source": "ashby", "token": "nabla"},
                      [call.args[0] for call in fetch.call_args_list])

    def test_a_paywalled_job_whose_free_link_fails_is_not_delivered(self):
        """Losing the rescue must not resurrect the link the cut rejected.

        The cut judges apply_link, so this job survives on its free link. When
        that link then fails verification the rescue is dropped -- and without
        this the job would be delivered on the jobleads.com URL the cut exists
        to discard. Like every paid-relister drop it is not remembered, so the
        same posting on a free board tomorrow is still delivered.
        """
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz",
                   apply_url="https://job-boards.greenhouse.io/next/jobs/1")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "rescue_apply_link"):
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_CLOSED, digest.LINK_OPEN])
        body = "\n".join(posts)
        self.assertNotIn("jobleads.com", body)
        self.assertNotIn("job-boards.greenhouse.io", body)
        self.assertIn("0 delivered today", body)
        path = os.path.join(work, "seen.json")
        seen = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        self.assertEqual(seen, {}, "a link verdict must not be remembered as a delivery")

    def test_a_paywalled_job_reaching_main_from_elsewhere_is_still_refused(self):
        """fetch_jsearch refuses these now, so main()'s cut is a last line of
        defence rather than the main event. It still has to hold: a paywalled
        link must never reach a card, whatever source produced it."""
        job = dict(REAL_JOBS[1], url="https://www.jobleads.com/job/xyz")
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"}}
        with mock.patch.object(digest, "discover_boards", return_value=0):
            posts, work = run_main(self.ENV, ranking, jobs=[job],
                                   link_statuses=[digest.LINK_OPEN])
        body = "\n".join(posts)
        self.assertNotIn("jobleads.com", body)
        self.assertIn("0 delivered today", body)

    def test_worker_only_quiet_day_does_not_call_an_absent_webhook(self):
        work = tempfile.mkdtemp(prefix="digest_quiet_")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            with mock.patch.dict(os.environ, {
                     "INTERACTIVE_WORKER_URL": "https://worker.test",
                     "WORKER_CALLBACK_SECRET": "secret",
                     "RAPIDAPI_ENABLED": "false",
                 }, clear=True), \
                 mock.patch.object(digest, "fetch_jsearch", return_value=[]) as fetch_aggregator, \
                 mock.patch.object(digest, "COMPANIES", []), \
                 mock.patch.object(digest, "post_interactive_cards", return_value={"posted": ["heartbeat"], "existing": [], "failed": []}) as post_worker, \
                 mock.patch.object(digest, "post_discord") as post_webhook:
                digest.main()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(work)

        post_worker.assert_called_once()
        self.assertEqual(post_worker.call_args.args[2], [])
        fetch_aggregator.assert_not_called()
        self.assertEqual(post_worker.call_args.kwargs["heartbeat"]["content"],
                         f"**Job digest — {digest.riyadh_today().isoformat()}** · 0 delivered today.\n"
                         "Matching evidence unavailable; check your resume and profile.")
        self.assertTrue(post_worker.call_args.kwargs["heartbeat"]["key"].startswith("heartbeat:"))
        post_webhook.assert_not_called()

    def test_quiet_report_keeps_diagnostics_in_logs(self):
        cases = [
            ({"newly_found": 0, "already_seen": 70, "below_floor": 0},
             "70 seen before", "0 newly found", "0 rejected for role or qualification mismatch"),
            ({"newly_found": 14, "already_seen": 56, "below_floor": 14},
             "56 seen before", "14 newly found", "14 rejected for role or qualification mismatch"),
        ]
        for summary, *expected in cases:
            with self.subTest(summary=summary), \
                 mock.patch.object(digest, "post_discord") as post, \
                 mock.patch.object(digest, "log") as log:
                digest.post_heartbeat(
                    "https://discord.test/webhook", "", "", "2026-09-27",
                    880, 70, ["Average fit down"], **summary,
                    boards_read=64, boards_total=79, boards_skipped=15,
                    paused_feeds=["jsearch", "active_ats", "linkedin_api"],
                )

            body = "\n".join(post.call_args.args[1])
            self.assertIn("0 of 10", body)
            self.assertIn("Search coverage incomplete", body)
            self.assertIn("qualification mismatches" if summary["below_floor"] else "previously sent jobs", body)
            diagnostics = "\n".join(str(call.args[0]) for call in log.call_args_list)
            for text in expected:
                self.assertIn(text, diagnostics)
            self.assertIn("Read 64 of 79 employer boards; 15 skipped", diagnostics)
            self.assertIn("Quota-paused feeds: JSearch, Active ATS, LinkedIn", diagnostics)
            self.assertIn("Average fit down", diagnostics)

    def test_added_employer_boards_use_saudi_filters_identity_and_freshness(self):
        added = {
            (company["name"], company["source"], company["token"])
            for company in digest.COMPANIES
            if company["name"] in {"MinIO", "Scale AI", "Lucidya"}
        }
        self.assertEqual(added, {
            ("MinIO", "greenhouse", "minio"),
            ("Scale AI", "greenhouse", "scaleai"),
            ("Lucidya", "workable", "lucidya"),
        })

        recent_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        jobs = [
            {"id": "minio-1", "title": "Senior Software Engineer",
             "company": "MinIO", "location": "Hybrid - Riyadh, Saudi Arabia",
             "source": "greenhouse", "token": "minio",
             "url": "https://job-boards.greenhouse.io/minio/jobs/1",
             "first_published": recent_date},
            {"id": "scale-1", "title": "Senior Full-Stack Software Engineer",
             "company": "Scale AI", "location": "Riyadh, Saudi Arabia",
             "source": "greenhouse", "token": "scaleai",
             "url": "https://job-boards.greenhouse.io/scaleai/jobs/1",
             "first_published": recent_date},
            {"id": "lucidya-1", "title": "Senior Software Engineer",
             "company": "Lucidya", "location": "Riyadh, Saudi Arabia",
             "source": "workable", "token": "lucidya",
             "url": "https://apply.workable.com/lucidya/j/1/",
             "first_published": recent_date},
        ]
        self.assertTrue(all(digest.matches_filters(job) for job in jobs))
        self.assertEqual(len({digest.job_identity(job) for job in jobs}), 3)
        today = datetime.date.today()
        recent, stale = digest.split_by_age(jobs, 7, today)
        self.assertEqual(len(recent), 3)
        self.assertEqual(stale, [])
        recent, stale = digest.split_by_age(
            [dict(jobs[0], first_published=(today - datetime.timedelta(days=8)).isoformat())], 7, today)
        self.assertEqual(recent, [])
        self.assertEqual(len(stale), 1)

    def test_non_saudi_posting_on_an_added_board_is_rejected(self):
        job = {"id": "foreign", "title": "Senior Software Engineer",
               "company": "MinIO", "location": "Dubai, UAE",
               "source": "greenhouse", "token": "minio",
               "url": "https://job-boards.greenhouse.io/minio/jobs/2"}
        self.assertFalse(digest.matches_filters(job))

    def test_retryable_interactive_delivery_does_not_fall_back_or_mark_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
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
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
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

    def test_a_webhook_fallback_delivery_is_remembered_so_it_is_not_resent(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
        }

        def worker_delivery_raises(_url, _secret, snapshots, **kwargs):
            # A quiet-day heartbeat is not what this test is about:
            # post_heartbeat's own Worker/webhook asymmetry is a separate,
            # already-known finding, so let that call succeed rather than
            # crashing the second run below.
            if "heartbeat" in kwargs:
                return {"posted": [], "existing": [], "in_progress": [], "retryable": [], "failed": []}
            raise RuntimeError("worker unreachable")

        with mock.patch.object(digest, "post_interactive_cards", side_effect=worker_delivery_raises):
            posts, work = run_main(env, ranking)

            # The Worker call raised, so the digest went out over the webhook.
            self.assertEqual(len(posts), 1)
            with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
                seen = json.load(handle)
            # remember() writes both a job identity key and a jd: description
            # fingerprint per delivered job, so two jobs delivered means at
            # least four entries. Written as >= rather than an exact count so
            # this test does not become brittle if remember()'s shape changes.
            self.assertGreaterEqual(len(seen), 2)

            posts_again, _ = run_main(env, ranking, work=work)

        # The same digest is not resent the next day: the jobs are remembered.
        self.assertEqual(posts_again, [])

    def test_partial_interactive_delivery_succeeds_and_marks_only_posted_jobs_seen(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
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
            seen = json.load(handle)
        # One posting remembered two ways: its identity, and its description.
        self.assertEqual(len([k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)]), 1)

    def test_a_card_that_fails_beside_successes_says_why(self):
        """The 19:48 run on 3 Sep accepted 7 of 8 cards and printed nothing at
        all about the eighth. The detail loop only ran when EVERY card failed,
        so the common case -- one card lost among successes -- named neither the
        job nor the reason. That job is correctly left out of seen.json and
        retried tomorrow, but a retry that keeps failing is invisible."""
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
        }

        def partial(_url, _secret, snapshots, **_kwargs):
            return {
                "posted": [snapshots[0]["job_key"]],
                "existing": [], "in_progress": [], "retryable": [],
                "failed": [{"job_key": snapshots[1]["job_key"],
                            "error": "Discord card post failed: 503"}],
            }

        with mock.patch.object(digest, "post_interactive_cards", side_effect=partial),              mock.patch.object(digest, "log") as logged:
            run_main(env, ranking)
        lines = [c.args[0] for c in logged.call_args_list if c.args]
        self.assertTrue(any("Discord card post failed: 503" in line for line in lines),
                        f"the failure reason was never logged: {lines}")

    def test_a_deferred_role_is_not_delivered_and_not_remembered(self):
        """Deferring is a verdict on the shape of today's digest, not on the
        role. Remembering it would lose a good fourth role at one employer for
        good, which is the opposite of what the cap is for."""
        jobs = [dict(REAL_JOBS[1], id=f"jsearch_cap{i}", company="Innovationteam",
                     title=f"Senior Backend Engineer {i}",
                     url=f"https://jobs.example.com/cap/{i}") for i in range(4)]
        ranking = {f"jsearch_cap{i}": {"score": 95 - i * 2,
                                       "why": "Senior IC backend role, requirements covered"}
                   for i in range(4)}
        with mock.patch.object(digest, "rescue_apply_link"):
            posts, work = run_main(self.ENV, ranking, jobs=jobs,
                                   link_statuses=[digest.LINK_OPEN] * 4)
        body = "\n".join(posts)
        self.assertIn("Senior Backend Engineer 0", body)
        self.assertNotIn("Senior Backend Engineer 3", body)
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertNotIn("jsearch_cap3", seen,
                         "a deferred role must come back tomorrow, not be remembered")

    def test_full_run_archives_jobs_without_auto_tailoring(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"}}
        posts, work = run_main(self.ENV, ranking)
        body = "\n".join(posts)
        # Only the two senior IC roles delivered
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("Senior Data Architect", body)
        for gone in ("Lead AI Engineer", "Staff level", "Team Leader", "Bangkok"):
            self.assertNotIn(gone, body)
        self.assertIn(f"**{digest.TIER_STRONG}**", body)
        # 74 is the rubric's "competitive with gaps" band. It used to read as
        # "Worth reviewing", which undersold a real match with a real gap.
        self.assertNotIn(f"**{digest.TIER_COMPETITIVE}**", body)
        # Tailoring is explicit through a Discord button; daily delivery is
        # never allowed to spend credits or leave a resume in the repository.
        self.assertNotIn("tailored resume:", body)
        self.assertFalse(os.path.exists(os.path.join(work, "tailored")))
        # Every posted job keeps its full JD in a durable daily archive.
        archive_date = digest.riyadh_today().isoformat()
        with open(os.path.join(work, "jobs", f"{archive_date}.json"), encoding="utf-8") as handle:
            archive = json.load(handle)
        self.assertEqual({item["source_id"] for item in archive["jobs"]}, {"jsearch_b", "jsearch_e"})
        self.assertTrue(all(item["jd"]["text"] for item in archive["jobs"]))
        # State written
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        # An aggregator posting is remembered by URL, never by the per-request
        # id it arrived with, plus one key for its description text.
        identities = {k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)}
        self.assertEqual(identities, {"url:https://example.com/b", "url:https://example.com/e"})
        self.assertEqual(len(seen) - len(identities), 2)
        self.assertFalse(identities & {"jsearch_b", "jsearch_e"})

    def test_unknown_link_is_reviewable_and_only_strong_is_tailored(self):
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
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
        self.assertEqual(stats[-1]["strong"], 2)
        self.assertEqual(stats[-1]["competitive"], 0)
        self.assertEqual(stats[-1]["review"], 0)

    def test_the_same_posting_under_a_new_id_is_not_delivered_twice(self):
        """JSearch mints a fresh opaque id per request, so seen.json keyed on it
        never matched and 65 of the first 122 aggregator cards were repeats. One
        JAL frontend role arrived on 9 days under 11 ids."""
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"}}
        posts, work = run_main(self.ENV, ranking)
        self.assertIn("Senior Software Engineer (AI Systems)", "\n".join(posts))

        # Tomorrow: same postings, same URLs, brand new ids, and a LinkedIn
        # tracking parameter that also changes on every response.
        tomorrow = [dict(job, id=f"jsearch_{job['id']}_regenerated",
                         url=f"{job['url']}?refId=abc%3D%3D&trackingId=xyz")
                    for job in REAL_JOBS]
        posts, _ = run_main(self.ENV, ranking, jobs=tomorrow, work=work)

        body = "\n".join(posts)
        self.assertIn("0 delivered today", body)
        self.assertNotIn("Senior Software Engineer (AI Systems)", body)

    def test_a_relisted_posting_with_a_new_url_is_caught_by_its_description(self):
        """Jooble relisted one posting under two of its own ids in the first 11
        archived days. The description is the last thing that does not move."""
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"}}
        _, work = run_main(self.ENV, ranking)

        relisted = [dict(job, id=f"{job['id']}_new", url=f"{job['url']}-relisted-elsewhere")
                    for job in REAL_JOBS]
        posts, _ = run_main(self.ENV, ranking, jobs=relisted, work=work)

        self.assertIn("0 delivered today", "\n".join(posts))

    def test_a_genuinely_new_posting_still_gets_through(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"}}
        _, work = run_main(self.ENV, ranking)

        fresh_job = dict(REAL_JOBS[1], id="jsearch_brand_new",
                         url="https://example.com/brand-new",
                         title="Senior Machine Learning Engineer",
                         description=REAL_JOBS[1]["description"] + " Extra requirement: Rust.")
        posts, _ = run_main(self.ENV, {"jsearch_brand_new": {"score": 91, "why": "Senior IC role"}},
                            jobs=[fresh_job], work=work)

        self.assertIn("Senior Machine Learning Engineer", "\n".join(posts))

    def test_closed_link_is_not_delivered_or_seen(self):
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 84, "why": "Senior IC data role, some gaps"},
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
        # The closed posting is not remembered at all, so it stays deliverable
        # if the employer reopens it.
        self.assertEqual({k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)},
                         {"url:https://example.com/e"})

    def test_ranker_down_holds_jobs_for_retry(self):
        posts, work = run_main(self.ENV, None)
        body = "\n".join(posts)
        self.assertNotIn("**Senior Software Engineer (AI Systems)**", body)
        self.assertIn("Job matching temporarily unavailable; retrying next run.", body)
        self.assertNotIn("tailored resume:", body)

    def test_a_below_80_job_without_viability_evidence_is_held_without_cooldown(self):
        """An unverified fallback stays retryable rather than receiving a score-only cooldown."""
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": digest.MIN_DELIVER_SCORE - 1,
                          "why": "Adjacent architecture work, most requirements missing"},
        }

        posts, work = run_main(self.ENV, ranking)

        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertNotIn("Senior Data Architect", body)
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX + "url:https://example.com/e", seen)
        self.assertNotIn("url:https://example.com/e", seen,
                         "a below-floor score must not be remembered as a delivery")

    def test_a_qualification_gap_without_fit_evidence_is_held_after_caps(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Supported role"}}
        blocked = dict(CLEAN_CHECK, ceiling="low", blockers=[
            {"title": "Required qualification absent", "severity": "high"}])
        posts, work = run_main(self.ENV, ranking,
            fit_checks={"Senior Software Engineer (AI Systems)": blocked})
        self.assertNotIn("**Senior Software Engineer (AI Systems)**", "\n".join(posts))
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX + "url:https://example.com/b", seen)
        self.assertNotIn("url:https://example.com/b", seen)

    def _snapshot(self, work, title):
        path = os.path.join(work, "jobs", digest.riyadh_today().isoformat() + ".json")
        with open(path, encoding="utf-8") as handle:
            snapshots = json.load(handle)["jobs"]
        return next(s for s in snapshots if s["title"] == title)

    def test_every_delivered_job_gets_the_paid_reality_check(self):
        """Scoped to jobs already scoring 80+, this pass produced exactly one
        low ceiling across 208 archived snapshots -- it never looked at the
        other 196. A gap the ranker missed on a job it scored 78 is the gap
        most worth naming, so every delivered job is checked now.

        It is still a model call per job. MAX_CHECKED_JOBS and
        CHECK_BUDGET_SECONDS bound the cost, and a job past either keeps a
        neutral result that caps nothing."""
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_a": {"score": 55, "why": "Lead title, seniority above target"},
        }

        posts, work = run_main(self.ENV, ranking)
        shutil.rmtree(work, ignore_errors=True)

        checked = LAST_CHECK_CALL["titles"]
        self.assertIn("Senior Software Engineer (AI Systems)", checked)
        self.assertEqual(len(checked), 1,
                         "only validated scores eligible for delivery need a paid check")
        # Pinned against the cards actually sent rather than a title list, so a
        # change to the fixture cannot quietly narrow what this asserts.
        body = "\n".join(posts)
        delivered = [title for title in checked if f"**{title}**" in body]
        self.assertEqual(sorted(checked), sorted(delivered),
                         "a job that was checked but not delivered is a wasted model call")
        self.assertGreater(LAST_CHECK_CALL["resume_chars"], 0,
                           "the check must receive resume evidence to check against")

    def test_an_unchecked_job_is_held_for_retry(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Supported role"}}
        posts, work = run_main(self.ENV, ranking, fit_checks={})
        self.assertNotIn("**Senior Software Engineer (AI Systems)**", "\n".join(posts))
        self.assertIn("Awaiting complete fit verification", "\n".join(posts))
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {})

    def test_a_high_ceiling_job_keeps_its_score_and_tier(self):
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
        }
        clean = {"blockers": [], "unclear": [], "strengths": [],
                 "ceiling": "high", "note": ""}

        posts, work = run_main(
            self.ENV, ranking,
            fit_checks={"Senior Software Engineer (AI Systems)": clean})

        self.assertIn("fit 93", "\n".join(posts))
        snapshot = self._snapshot(work, "Senior Software Engineer (AI Systems)")
        self.assertEqual(snapshot["tier"], "Strong matches")
        self.assertEqual(snapshot["ceiling"], "high")
        card = job_archive.job_card_payload(snapshot)
        self.assertNotIn("Low ceiling", card["embeds"][0]["footer"]["text"])
        self.assertNotIn("Gaps the posting states", [field["name"] for field in card["embeds"][0]["fields"]])

    def test_the_drift_average_ignores_the_blocker_cap(self):
        """avg_score is taken before the cap (digest.py:2131), so a run where
        the reality pass capped everything must record the same average as one
        where it capped nothing. Otherwise the reality pass would masquerade as
        a profile drift and fire check_drift every day it did its job."""
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
        }
        title = "Senior Software Engineer (AI Systems)"
        blocked = {"blockers": [{"title": "No BigQuery or GCP evidence", "severity": "high"},
                                {"title": "No Bachelor's degree", "severity": "high"}],
                   "unclear": [], "strengths": [], "ceiling": "low", "note": ""}
        clean = {"blockers": [], "unclear": [], "strengths": [],
                 "ceiling": "high", "note": ""}

        def average(fit_checks):
            _, work = run_main(self.ENV, ranking, fit_checks=fit_checks)
            with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
                return json.load(handle)[-1]["avg_score"]

        self.assertEqual(average({title: blocked}), average({title: clean}))

    def test_the_drift_average_is_measured_before_the_floor_cuts(self):
        """check_drift compares today's average against the prior window. Taking
        it after a cut that removes exactly the low scores would make a stale
        profile look like a healthy one."""
        # Sized from the live floor so lowering it cannot quietly turn this
        # into a test where nothing is cut and the assertion proves nothing.
        rejected = digest.MIN_DELIVER_SCORE - 1
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": rejected,
                          "why": "Adjacent architecture work, most requirements missing"},
        }

        posts, work = run_main(self.ENV, ranking)

        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            row = json.load(handle)[-1]
        self.assertEqual(row["avg_score"], round((93 + rejected) / 2, 1))
        self.assertEqual(row["new"], 2)
        self.assertEqual(row["below_floor"], 0)
        self.assertEqual(row["unverified_fit"], 1)
        self.assertEqual(row["delivered"], 1)

    def test_no_ranker_key_no_tailoring(self):
        env = dict(self.ENV)
        env.pop("OPENROUTER_API_KEY")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            posts, work = run_main(env, None)
        body = "\n".join(posts)
        self.assertNotIn("**Senior Software Engineer (AI Systems)**", body)
        self.assertIn("Job matching temporarily unavailable; retrying next run.", body)
        self.assertNotIn("tailored resume:", body)
        self.assertFalse(os.path.exists(os.path.join(work, "tailored")))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBoardDiscoverySeeds(unittest.TestCase):
    """Which postings are worth spending a board probe on.

    Discovery was seeded only from postings the paid-relister cut discarded, so
    it only ever asked about employers whose links sat on jobleads and jooble.
    By 7 Sep 2026 that had produced a ledger of Qualcomm, Lucid Motors, Delivery
    Hero and FileCloud -- 8 of 10 boards found with ZERO open roles -- because a
    paid relister carries global postings that have nothing to do with hiring in
    Saudi. Tuning the probe cannot fix a seed list that never contained a Saudi
    employer in the first place.

    A card that passed the filters is the opposite: it is a real Saudi role this
    candidate would be shown. Its employer is exactly who should be asked.
    """

    def _job(self, company, source, url):
        return {"company": company, "title": "Backend Engineer",
                "source": source, "url": url}

    def test_a_delivered_aggregator_card_seeds_a_probe(self):
        matched = [self._job("Tabby", "jsearch", "https://sa.linkedin.com/jobs/view/x-1")]
        seeds = digest.board_discovery_seeds([], [], matched)
        self.assertEqual([j["company"] for j in seeds], ["Tabby"])

    def test_an_employer_sourced_card_is_not_probed(self):
        """Its board is already known -- that is where the job came from."""
        matched = [self._job("Foodics", "workable", "https://apply.workable.com/foodics/j/A/")]
        self.assertEqual(digest.board_discovery_seeds([], [], matched), [])

    def test_paywalled_postings_keep_their_priority(self):
        """Only MAX_BOARD_PROBES run a day. A refused posting is pure gain to
        rescue; an aggregator card was already delivered, so it can wait."""
        paywalled = [self._job("Refused", "jsearch", "https://jobleads.com/1")]
        matched = [self._job("Delivered", "jsearch", "https://sa.linkedin.com/jobs/view/x-1")]
        seeds = digest.board_discovery_seeds(paywalled, [], matched)
        self.assertEqual([j["company"] for j in seeds], ["Refused", "Delivered"])

    def test_one_employer_is_offered_once(self):
        """Five probes a day, so a company with three open roles must not eat
        three of them for the same answer."""
        matched = [self._job("Tabby", "jsearch", "https://sa.linkedin.com/jobs/view/x-1"),
                   self._job("Tabby | تابي", "jsearch", "https://sa.linkedin.com/jobs/view/x-2")]
        seeds = digest.board_discovery_seeds([], [], matched)
        self.assertEqual(len(seeds), 1)

    def test_a_company_with_no_usable_name_is_skipped(self):
        matched = [self._job("", "jsearch", "https://sa.linkedin.com/jobs/view/x-1")]
        self.assertEqual(digest.board_discovery_seeds([], [], matched), [])


def requests_unquote(text):
    from urllib.parse import unquote
    return unquote(text)


class TestFetchActiveAts(unittest.TestCase):
    """The ATS feed that replaced the aggregator as the fresh source.

    Measured 7 Sep 2026, same day, same country, same seniority band:
    JSearch returned ZERO jobs at date_posted=week and a corpus aged 18-159
    days; Active Jobs DB returned ten technical roles aged 0-1 days, every one
    of them on the employer's own ATS. That gap is the whole reason this exists.

    The free plan allows 250 JOBS and 25 REQUESTS a month -- two separate hard
    limits -- so every row this fetcher accepts is spent allowance.
    """

    ROW = {
        "id": "ats-1",
        "title": "Software Engineer - Full Stack",
        "organization": "Moyasar Financial Company",
        "url": "https://jobs.workable.com/view/abc/software-engineer",
        "source": "workable",
        "date_posted": "2026-09-07T06:00:00",
        "date_created": "2026-09-07T08:00:00",
        "locations_derived": ["Riyadh, Saudi Arabia"],
        "description_text": "Build payment APIs in Python.",
    }

    def _response(self, rows, status=200, headers=None):
        response = mock.Mock()
        response.status_code = status
        response.json = lambda: rows
        response.text = ""
        response.headers = headers or {"x-ratelimit-jobs-remaining": "220",
                                       "x-ratelimit-requests-remaining": "22"}
        return response

    def test_a_row_becomes_a_job_the_rest_of_the_pipeline_understands(self):
        with mock.patch.object(digest.requests, "get", return_value=self._response([self.ROW])):
            jobs = digest.fetch_active_ats("key")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["company"], "Moyasar Financial Company")
        self.assertEqual(job["title"], "Software Engineer - Full Stack")
        self.assertEqual(job["source"], "active_ats")
        self.assertEqual(job["first_published"], "2026-09-07")
        self.assertIn("Saudi Arabia", job["location"])
        self.assertEqual(job["description"], "Build payment APIs in Python.")

    def test_the_posting_date_is_the_one_the_age_gate_reads(self):
        """date_created is when Fantastic.jobs indexed the row; date_posted is
        when the employer published it. Gating on the wrong clock would let a
        long-live posting indexed today pass as posted today."""
        row = dict(self.ROW, date_posted="2026-09-01T06:00:00",
                   date_created="2026-09-07T08:00:00")
        with mock.patch.object(digest.requests, "get", return_value=self._response([row])):
            jobs = digest.fetch_active_ats("key")
        self.assertEqual(jobs[0]["first_published"], "2026-09-01")

    def test_a_row_this_source_is_the_employer_for_is_preferred_over_linkedin(self):
        """prefer_employer_postings drops the aggregator copy only for sources
        that ARE the employer. An ATS row read through this feed still comes
        from the employer's own board, so it has to count as one."""
        self.assertIn("active_ats", digest.EMPLOYER_SOURCES)

    def test_a_relister_posting_through_an_ats_is_refused(self):
        """Jobgether posts other companies' roles through Lever, so the host is
        jobs.lever.co and PAID_RELISTER_HOSTS never sees it. Three of ten rows
        in the 7 Sep probe were Jobgether. Left in, EMPLOYER_SOURCES would make
        prefer_employer_postings favour it over the real employer's link."""
        row = dict(self.ROW, organization="Jobgether", id="ats-2")
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([row, self.ROW])):
            jobs = digest.fetch_active_ats("key")
        self.assertEqual([j["company"] for j in jobs], ["Moyasar Financial Company"])

    def test_no_key_spends_nothing(self):
        with mock.patch.object(digest.requests, "get") as get:
            self.assertEqual(digest.fetch_active_ats(""), [])
        get.assert_not_called()

    def test_a_failed_request_is_survivable(self):
        with mock.patch.object(digest.requests, "get",
                               side_effect=digest.requests.Timeout("slow")):
            self.assertEqual(digest.fetch_active_ats("key"), [])
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([], status=429)):
            self.assertEqual(digest.fetch_active_ats("key"), [])

    def test_the_monthly_job_allowance_is_read_off_the_response(self):
        """Both caps are hard and neither resets early. A static day-count
        assumes every request returns a full page; the header is the truth."""
        quota = {}
        headers = {"x-ratelimit-jobs-remaining": "31",
                   "x-ratelimit-requests-remaining": "3"}
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([self.ROW], headers=headers)):
            digest.fetch_active_ats("key", quota=quota)
        self.assertEqual(quota["jobs_remaining"], 31)
        self.assertEqual(quota["requests_remaining"], 3)

    def test_the_last_of_the_monthly_allowance_is_never_spent(self):
        """Running the jobs cap to zero takes the only fresh source out until
        the reset, which is the failure this whole change exists to prevent."""
        with mock.patch.object(digest.requests, "get") as get:
            jobs = digest.fetch_active_ats(
                "key", quota={"jobs_remaining": digest.FREE_TIER["active_ats"]["reserve_jobs"] - 1})
        self.assertEqual(jobs, [])
        get.assert_not_called()

    def test_the_request_asks_the_api_to_do_the_filtering(self):
        """Every row the server filters out is allowance not spent."""
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([])) as get:
            digest.fetch_active_ats("key")
        url = get.call_args[0][0]
        self.assertIn("date_posted_gte=", url)
        self.assertIn("description_format=text", url)
        self.assertIn("title_advanced=", url)
        self.assertIn("Saudi", requests_unquote(url))

    def test_fresh_feeds_keep_weekly_index_window_for_30_day_fallback(self):
        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        for fetch in (digest.fetch_active_ats, digest.fetch_linkedin):
            with self.subTest(feed=fetch.__name__):
                quota = {}
                with mock.patch.object(digest.requests, "get",
                                       return_value=self._response([])) as get:
                    self.assertEqual(fetch("key", quota=quota, max_age_days=30), [])
                get.assert_called_once()
                query = parse_qs(urlparse(get.call_args.args[0]).query)
                self.assertEqual(query["time_frame"], ["7d"])
                self.assertEqual(query["date_posted_gte"], [cutoff + "T00:00:00"])
                self.assertEqual(query["limit"], ["10"])
                self.assertEqual(quota["jobs_remaining"], 220)
                self.assertEqual(quota["requests_remaining"], 22)


class TestApiQuotasSurviveTheRun(unittest.TestCase):
    """The monthly allowance has to be known BEFORE the request that exceeds it.

    Both caps are hard, both reset monthly, and the headers only arrive on a
    response -- so what one run learned has to reach the next. Keyed by feed
    because RapidAPI meters PER API: one shared counter would make each feed's
    reserve fire on the other's spending.
    """

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="digest_quota_")
        self.old = os.getcwd()
        os.chdir(self.work)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.work, ignore_errors=True)

    def test_a_missing_file_reads_as_no_knowledge_rather_than_no_allowance(self):
        """First run of all, and the first run after the file is lost. Assuming
        zero would disable the only fresh sources permanently."""
        self.assertEqual(digest.load_api_quotas(), {})

    def test_what_one_run_learned_reaches_the_next(self):
        digest.save_api_quotas({"active_ats": {"jobs_remaining": 90},
                                "linkedin_api": {"jobs_remaining": 200}})
        self.assertEqual(digest.load_api_quotas(),
                         {"active_ats": {"jobs_remaining": 90},
                          "linkedin_api": {"jobs_remaining": 200}})

    def test_jsearch_quota_state_round_trips_with_its_observation_time(self):
        state = {"jsearch": {"requests_remaining": 27,
                              "reset_at": 1790000000.0,
                              "observed_at": 1789000000.0}}
        digest.save_api_quotas(state)
        self.assertEqual(digest.load_api_quotas(), state)

    def test_a_corrupt_file_does_not_stop_the_digest(self):
        with open(digest.ACTIVE_ATS_STATE_FILE, "w") as handle:
            handle.write("{not json")
        self.assertEqual(digest.load_api_quotas(), {})

    def test_a_file_written_before_there_were_two_feeds_still_counts(self):
        """The file started as one feed's counters at the top level. Reading it
        as an empty mapping would silently hand that feed a full allowance it
        does not have."""
        with open(digest.ACTIVE_ATS_STATE_FILE, "w") as handle:
            json.dump({"jobs_remaining": 40, "requests_remaining": 4}, handle)
        self.assertEqual(digest.load_api_quotas(),
                         {"active_ats": {"jobs_remaining": 40,
                                         "requests_remaining": 4}})

    def test_the_reserve_is_honoured_across_runs(self):
        digest.save_api_quotas(
            {"active_ats": {"jobs_remaining": digest.FREE_TIER["active_ats"]["reserve_jobs"] - 1}})
        quotas = digest.load_api_quotas()
        with mock.patch.object(digest.requests, "get") as get:
            self.assertEqual(
                digest.fetch_active_ats("key", quotas["active_ats"]), [])
        get.assert_not_called()

    def test_one_feed_running_low_does_not_stop_the_other(self):
        """The failure a shared counter would cause: the ATS feed near its cap
        silencing LinkedIn, which has its own untouched allowance."""
        digest.save_api_quotas(
            {"active_ats": {"jobs_remaining": digest.FREE_TIER["active_ats"]["reserve_jobs"] - 1},
             "linkedin_api": {"jobs_remaining": 240}})
        quotas = digest.load_api_quotas()
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: [TestFetchLinkedin.ROW]
        response.text = ""
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response):
            jobs = digest.fetch_linkedin("key", quotas["linkedin_api"])
        self.assertEqual(len(jobs), 1)


class TestPostingAge(unittest.TestCase):
    """Every source uses the configured posting-age cutoff.

    Missing, invalid and future dates are excluded for every source.
    """

    def _job(self, first_published, **kw):
        return dict({"company": "Acme", "title": "Backend Engineer",
                     "source": "active_ats", "url": "https://acme.test/1",
                     "first_published": first_published}, **kw)

    def test_a_posting_inside_the_window_is_kept(self):
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("2026-09-05")], 7, today)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(stale, [])

    def test_a_posting_older_than_the_window_is_dropped(self):
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("2026-08-07")], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

    def test_the_boundary_day_is_inside_the_window(self):
        today = datetime.date(2026, 9, 7)
        fresh, _ = digest.split_by_age([self._job("2026-08-31")], 7, today)
        self.assertEqual(len(fresh), 1)

    def test_an_undated_posting_is_excluded_and_marked(self):
        """Pinpoint publishes no date. Dropping would remove Tabby entirely."""
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("", source="pinpoint")], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)
        self.assertIsNone(stale[0]["age_days"])

    def test_an_unparseable_date_is_excluded(self):
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("not-a-date")], 7, today)
        self.assertEqual(fresh, [])
        self.assertIsNone(stale[0]["age_days"])

    def test_the_age_is_recorded_so_the_card_can_show_it(self):
        today = datetime.date(2026, 9, 7)
        fresh, _ = digest.split_by_age([self._job("2026-09-04")], 7, today)
        self.assertEqual(fresh[0]["age_days"], 3)

    def test_a_future_date_is_excluded(self):
        """Timezones and ingestion delay can put a posting a few hours ahead."""
        today = datetime.date(2026, 9, 7)
        fresh, rejected = digest.split_by_age([self._job("2026-09-08")], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(rejected[0]["age_days"], -1)


class TestPostingAgeLabel(unittest.TestCase):
    def test_the_label_says_what_is_known(self):
        self.assertEqual(digest.age_label(0), "posted today")
        self.assertEqual(digest.age_label(1), "posted yesterday")
        self.assertEqual(digest.age_label(4), "posted 4 days ago")
        self.assertEqual(digest.age_label(None), "posting date unknown")


class TestTheCardStatesThePostingAge(unittest.TestCase):
    def test_a_delivered_card_says_how_old_the_posting_is(self):
        ranking = {"jsearch_a": {"score": 88, "why": "Senior IC role, requirements covered"}}
        posts, work = run_main(TestEndToEnd.ENV, ranking,
                               jobs=[dict(REAL_JOBS[0], title="Senior AI Engineer")],
                               link_statuses=[digest.LINK_OPEN])
        shutil.rmtree(work, ignore_errors=True)
        self.assertIn("posted yesterday", "\n".join(posts))

    def test_an_undated_card_is_excluded(self):
        """Pinpoint publishes no date. The card must not imply the cutoff was
        enforced on a posting whose age nobody knows."""
        job = dict(REAL_JOBS[0], title="Senior AI Engineer", first_published="")
        ranking = {"jsearch_a": {"score": 88, "why": "Senior IC role, requirements covered"}}
        posts, work = run_main(TestEndToEnd.ENV, ranking, jobs=[job],
                               link_statuses=[digest.LINK_OPEN])
        shutil.rmtree(work, ignore_errors=True)
        self.assertIn("Unverified posting dates: 1 (0 of 10).", "\n".join(posts))


class TestActiveAtsRealRowShape(unittest.TestCase):
    """One row exactly as the API returned it, 7 Sep 2026.

    The field names in fetch_active_ats were guesses read off a key list, and a
    wrong one fails silently: description in the wrong field empties the JD of
    every job from the source, and the card degrades to "title-only assessment"
    without anything erroring. Pinned verbatim so a rename fails loudly.
    """

    ROW = {
        "id": 2350650747,                              # an int, not a string
        "title": "Software Engineer - Full Stack",
        "organization": "Moyasar Financial Company",
        "url": ("https://jobs.workable.com/view/d2LmHHqAKSA2Vm6bHemoGr/"
                "software-engineer---full-stack-in-riyadh-at-moyasar-financial-company"),
        "source": "workable",
        "source_type": "ats",
        "date_posted": "2026-09-07T10:16:12.629",      # sub-second, no timezone
        "date_created": "2026-09-07T10:33:29.40768",
        "date_valid_through": None,
        "salary": None,
        "locations_derived": ["Riyadh, Riyadh Region, Saudi Arabia"],
        "description_text": "We are looking for a Software Engineer - Full Stack.",
    }

    def test_the_real_row_shapes_into_a_complete_job(self):
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: [self.ROW]
        response.text = ""
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response):
            job = digest.fetch_active_ats("key")[0]
        self.assertEqual(job["id"], "2350650747")
        self.assertEqual(job["first_published"], "2026-09-07")
        self.assertEqual(job["location"], "Riyadh, Riyadh Region, Saudi Arabia")
        self.assertEqual(job["description"], "We are looking for a Software Engineer - Full Stack.")
        self.assertEqual(job["company"], "Moyasar Financial Company")

    def test_the_shaped_job_passes_the_pipeline_filters(self):
        """Shaping it correctly is not enough; LOCATION_INCLUDE and
        TITLE_INCLUDE have to accept what the shaping produced."""
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: [self.ROW]
        response.text = ""
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response):
            job = digest.fetch_active_ats("key")[0]
        self.assertTrue(digest.matches_filters(job))


class TestTheAgeGateAppliesToEmployerBoards(unittest.TestCase):
    """Direct ATS listings still honor the digest's seven-day freshness rule."""

    def _job(self, source, first_published):
        return {"company": "Careem", "title": "Backend Engineer", "source": source,
                "url": "https://boards.greenhouse.io/careem/jobs/1",
                "first_published": first_published}

    def test_an_old_posting_on_an_employers_own_board_is_dropped(self):
        today = datetime.date(2026, 9, 7)
        for source in ("greenhouse", "ashby", "workable", "lever", "pinpoint"):
            with self.subTest(source=source):
                fresh, stale = digest.split_by_age(
                    [self._job(source, "2026-06-01")], 7, today)
                self.assertEqual(fresh, [])
                self.assertEqual(len(stale), 1, f"{source} should honor the age cutoff")

    def test_an_old_aggregator_posting_is_still_dropped(self):
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("jsearch", "2026-06-01")], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

    def test_the_ats_feed_is_gated_because_it_is_an_index_not_a_board(self):
        """active_ats rows ARE employer postings, but they are read from a
        third-party index of them, so the index decides what it still shows."""
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("active_ats", "2026-06-01")], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

class TestActiveAtsExpiry(unittest.TestCase):
    """date_valid_through is the index saying the posting has expired.

    Free -- it arrives on a row already paid for -- and liveness is what
    actually failed on 7 Sep. Often None (it was on the row sampled that day),
    so this only ever adds a drop, never keeps something verify_link would cut.
    """

    def _fetch(self, row):
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: [row]
        response.text = ""
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response):
            return digest.fetch_active_ats("key")

    BASE = dict(TestActiveAtsRealRowShape.ROW)

    def test_an_expired_posting_is_refused(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self._fetch(dict(self.BASE, date_valid_through=yesterday)), [])

    def test_a_posting_valid_in_the_future_is_kept(self):
        later = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        self.assertEqual(len(self._fetch(dict(self.BASE, date_valid_through=later))), 1)

    def test_no_expiry_is_not_an_expiry(self):
        self.assertEqual(len(self._fetch(dict(self.BASE, date_valid_through=None))), 1)

    def test_an_unparseable_expiry_is_ignored_rather_than_trusted(self):
        self.assertEqual(len(self._fetch(dict(self.BASE, date_valid_through="soon"))), 1)


class TestFetchLinkedin(unittest.TestCase):
    """LinkedIn as its own feed rather than relayed through Google for Jobs.

    Probed 7 Sep 2026: ten rows, every one posted that day, nine of the ten
    genuinely technical -- Tableau Developer at TCS, ROS2 and Embedded Firmware
    at Atwad, Data Science at stc, Backend Golang. JSearch, asked for the same
    country the same day, returned zero rows inside a week.

    Same vendor and row shape as Active Jobs DB, so the shaping is shared. Its
    allowance is separate: RapidAPI meters per API.
    """

    ROW = {
        "id": 2351033857,
        "title": "Tableau Developer",
        "organization": "Tata Consultancy Services",
        "url": "https://sa.linkedin.com/jobs/view/tableau-developer-at-tata-consultancy-services-4462958816",
        "source": "linkedin",
        "date_posted": "2026-09-07T16:40:23",
        "date_valid_through": None,
        "locations_derived": ["Riyadh, Riyadh Region, Saudi Arabia"],
        "description_text": "Tata Consultancy Services is hiring a Tableau Developer.",
    }

    def _response(self, rows, status=200, headers=None):
        response = mock.Mock()
        response.status_code = status
        response.json = lambda: rows
        response.text = ""
        response.headers = headers or {}
        return response

    def test_a_row_shapes_the_same_way_the_ats_feed_does(self):
        with mock.patch.object(digest.requests, "get", return_value=self._response([self.ROW])):
            job = digest.fetch_linkedin("key")[0]
        self.assertEqual(job["company"], "Tata Consultancy Services")
        self.assertEqual(job["title"], "Tableau Developer")
        self.assertEqual(job["source"], "linkedin_api")
        self.assertEqual(job["first_published"], "2026-09-07")
        self.assertIn("Saudi Arabia", job["location"])
        self.assertTrue(job["description"])

    def test_it_is_not_treated_as_the_employers_own_posting(self):
        """An ATS row IS the employer's posting; a LinkedIn row is a relay of
        it. prefer_employer_postings must keep favouring the real board."""
        self.assertNotIn("linkedin_api", digest.EMPLOYER_SOURCES)
        self.assertNotIn("linkedin_api", digest.FETCHERS)

    def test_it_is_age_gated(self):
        today = datetime.date(2026, 9, 7)
        job = {"source": "linkedin_api", "first_published": "2026-06-01"}
        fresh, stale = digest.split_by_age([job], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

    def test_the_relister_exclusion_is_shared_with_the_ats_feed(self):
        """Jobgether appeared twice in the 7 Sep LinkedIn probe, same as in the
        ATS probe. One list, not two that drift apart."""
        row = dict(self.ROW, organization="Jobgether", id=99)
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([row, self.ROW])):
            jobs = digest.fetch_linkedin("key")
        self.assertEqual([j["company"] for j in jobs], ["Tata Consultancy Services"])

    def test_it_keeps_its_own_allowance(self):
        """Separate API, separate meter. Sharing one counter would make each
        feed's reserve fire on the other's spending."""
        quota = {}
        headers = {"x-ratelimit-jobs-remaining": "240",
                   "x-ratelimit-requests-remaining": "24"}
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([self.ROW], headers=headers)):
            digest.fetch_linkedin("key", quota=quota)
        self.assertEqual(quota["jobs_remaining"], 240)

    def test_no_key_and_a_failed_request_are_both_survivable(self):
        self.assertEqual(digest.fetch_linkedin(""), [])
        with mock.patch.object(digest.requests, "get",
                               side_effect=digest.requests.Timeout("slow")):
            self.assertEqual(digest.fetch_linkedin("key"), [])
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([], status=429)):
            self.assertEqual(digest.fetch_linkedin("key"), [])

    def test_the_request_narrows_on_saudi_arabia_and_a_date(self):
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([])) as get:
            digest.fetch_linkedin("key")
        url = get.call_args[0][0]
        self.assertIn("date_posted_gte=", url)
        self.assertIn("description_format=text", url)
        self.assertIn("Saudi", requests_unquote(url))


class TestNonSoftwareEngineeringIsRefused(unittest.TestCase):
    """"Engineer" and "architect" are not software words in Saudi Arabia.

    Bayt exposed this on 7 Sep 2026. Of 177 Saudi postings inside a week, 33
    passed these filters -- and they were Structural Design Engineer,
    Architectural Engineer, Firefighting Engineer, Civil & Transportation
    Engineer, HSE Engineer, Fire Protection Supervisor, Bridge Inspector. The
    Saudi market runs on construction and industry, so a title filter built
    around the bare words matches that market far more often than software.

    It had gone unnoticed because LOCATION_INCLUDE and the ranker sit
    downstream and quietly absorbed it, at the cost of a model call per role.
    """

    CONSTRUCTION = [
        "Structural Design Engineer", "Architectural Engineer", "Architect",
        "Civil Engineer", "Sr Civil & Transportation Engineer",
        "Firefighting Engineer", "Fire Protection Supervisor / Electrical Engineer",
        "Mechanical Design Engineer - SolidWorks", "HSE Engineer | MEP",
        "Electrical Engineer", "Maintenance Engineer", "Bridge Inspector",
        "Steel Structure Estimation Engineer", "Pricing & Estimation Engineer (Fit-Out)",
        "Senior Planning Engineer", "Biomedical Engineer",
        "Architectural Designer", "Project Sales Engineer", "Sales Engineer",
    ]

    SOFTWARE = [
        "Senior Backend Engineer", "Software Engineer - Full Stack",
        "Machine Learning Engineer", "Data Engineer", "Senior Data Scientist",
        "DevOps Consultant", "Site Reliability Engineer", "MLOps Engineer",
        "Cloud Solutions Architect", "Solutions Architect - Data and AI",
        "Software Architect", "Platform Engineer", "Senior AI Engineer",
        "Tableau Developer", "ROS / ROS2 Developer (Software Engineer)",
        "Embedded Firmware Engineer", "Backend Developer (Golang)",
        "Fortinet Network Engineer", "QA Engineer", "Automation Engineer",
    ]

    def _job(self, title):
        return {"company": "Acme", "title": title,
                "location": "Riyadh, Saudi Arabia",
                "url": "https://example.com/1", "source": "bayt"}

    def test_construction_and_industrial_titles_are_refused(self):
        for title in self.CONSTRUCTION:
            with self.subTest(title=title):
                self.assertFalse(digest.matches_filters(self._job(title)),
                                 f"{title!r} should not reach a software digest")

    def test_software_titles_still_pass(self):
        for title in self.SOFTWARE:
            with self.subTest(title=title):
                self.assertTrue(digest.matches_filters(self._job(title)),
                                f"{title!r} must still reach the digest")


class TestSourceYield(unittest.TestCase):
    def test_fourteen_run_comparison_waits_for_real_data(self):
        from scripts.compare_job_coverage import compare_coverage
        baseline = [{"date": f"2026-09-{day:02d}", "fetched": 20, "new": 3, "delivered": 1}
                    for day in range(1, 15)]
        changed = [{"date": f"2026-10-{day:02d}", "fetched": 30, "new": 5, "delivered": 2,
                    "source_yield": {"active_ats": {"fetched": 10, "new": 3, "delivered": 1,
                                                    "quota_paused": False}}}
                   for day in range(1, 15)]
        self.assertEqual(compare_coverage(baseline + changed[:13])["status"], "pending")
        report = compare_coverage(baseline + changed)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["baseline"]["delivered_per_run"], 1)
        self.assertEqual(report["after"]["delivered_per_run"], 2)
        self.assertEqual(report["sources"]["active_ats"]["delivered"], 14)
        self.assertEqual(compare_coverage(changed)["status"], "unavailable")

    def test_source_counts_include_pauses_and_only_accepted_deliveries(self):
        fetched = [{"source": "jsearch"}, {"source": "jsearch"}, {"source": "greenhouse"}]
        self.assertEqual(digest.source_yield(
            fetched, fetched[:1], [], ["active_ats"]), {
                "jsearch": {"fetched": 2, "new": 1, "delivered": 0, "quota_paused": False},
                "greenhouse": {"fetched": 1, "new": 0, "delivered": 0, "quota_paused": False},
                "active_ats": {"fetched": 0, "new": 0, "delivered": 0, "quota_paused": True},
            })

    def test_rotation_covers_all_target_queries_without_more_requests(self):
        queries = digest.AGGREGATOR_QUERIES
        covered = set()
        for day in range(len(queries)):
            selection = digest.jsearch_query_plan(queries, 2, day)
            self.assertEqual(len(selection), 2)
            covered.update(selection)
        self.assertEqual(covered, set(queries))
        self.assertIn("LLM application engineer Saudi Arabia", covered)
        self.assertIn("AI product engineer Saudi Arabia", covered)

    def test_fresh_feed_switches_at_riyadh_midnight(self):
        before = datetime.datetime(2026, 10, 5, 20, 59, tzinfo=datetime.timezone.utc)
        after = before + datetime.timedelta(minutes=1)
        self.assertNotEqual(digest.fresh_api_feeds(digest.riyadh_ordinal(before)),
                            digest.fresh_api_feeds(digest.riyadh_ordinal(after)))


class TestFreeTierBudget(unittest.TestCase):
    """Every paid feed here runs on a free plan with hard monthly caps.

    Active Jobs DB and the LinkedIn feed allow 250 jobs and 25 requests a
    month each; the Indeed feed allows 50 requests. All of them are metered
    separately, all of them are smaller than a daily run over a 30-day month,
    and exhausting one takes that source out until the plan resets.
    """

    def test_a_feed_with_room_is_spent(self):
        self.assertFalse(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 20, "jobs_remaining": 200}))

    def test_fresh_rapidapi_feeds_are_staggered_on_alternate_days(self):
        self.assertEqual(digest.fresh_api_feeds(day=0),
                         ("active_ats",))
        self.assertEqual(digest.fresh_api_feeds(day=1), ("linkedin_api",))

    def test_rotation_day_is_riyadh_not_the_runner_utc_date(self):
        utc_evening = datetime.datetime(2026, 9, 20, 21, 30,
                                         tzinfo=datetime.timezone.utc)
        self.assertEqual(digest.riyadh_ordinal(utc_evening),
                         datetime.date(2026, 9, 21).toordinal())

    def test_scheduled_runs_default_to_rapidapi_but_manual_false_disables_it(self):
        self.assertTrue(digest.rapidapi_enabled(None))
        self.assertFalse(digest.rapidapi_enabled("false"))

    def test_manual_probe_rejects_unknown_quota_state(self):
        self.assertFalse(digest.can_spend("jsearch", {}, cost=1))

    def test_manual_probe_rejects_a_cost_that_would_cross_the_reserve(self):
        quota = {"requests_remaining": 42, "reset_at": digest.time.time() + 3600}
        self.assertFalse(digest.can_spend("jsearch", quota, cost=3))
        self.assertTrue(digest.can_spend("jsearch", quota, cost=2))

    def test_an_unconfirmed_probe_makes_no_paid_request(self):
        probe = mock.Mock(return_value=0)
        with mock.patch.dict(digest.os.environ, {}, clear=True):
            self.assertEqual(digest.run_paid_probe("jsearch", 1, probe, "key"), 1)
        probe.assert_not_called()

    def test_a_confirmed_probe_persists_only_its_quota_observation(self):
        quotas = {"jsearch": {"requests_remaining": 42,
                               "reset_at": digest.time.time() + 3600}}
        probe = mock.Mock(return_value=0)
        with mock.patch.dict(digest.os.environ, {"CONFIRM_RAPIDAPI_SPEND": "true"},
                             clear=True), \
             mock.patch.object(digest, "load_api_quotas", return_value=quotas), \
             mock.patch.object(digest, "save_api_quotas") as save:
            self.assertEqual(digest.run_paid_probe("jsearch", 1, probe, "key"), 0)
        probe.assert_called_once_with("key", quota=quotas["jsearch"])
        save.assert_called_once_with(quotas)

    def test_a_feed_near_its_request_cap_is_held_back(self):
        self.assertTrue(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 1, "jobs_remaining": 200}))

    def test_a_paused_feed_is_recorded_once_for_the_heartbeat(self):
        paused = []
        quota = {"requests_remaining": 1, "jobs_remaining": 200}
        for _ in range(2):
            self.assertTrue(digest.out_of_allowance(
                "active_ats", quota, paused_feeds=paused))
        self.assertEqual(paused, ["active_ats"])

    def test_the_exact_reserve_is_held_back_before_spending_it(self):
        self.assertTrue(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 5, "jobs_remaining": 200,
                           "reset_at": digest.time.time() + 3600}))

    def test_a_feed_near_its_job_cap_is_held_back(self):
        """Two separate hard limits; either one can end the month."""
        self.assertTrue(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 20, "jobs_remaining": 5}))

    def test_job_credit_cost_cannot_cross_the_reserve(self):
        quota = {"requests_remaining": 20, "jobs_remaining": 39,
                 "reset_at": digest.time.time() + 3600}
        self.assertTrue(digest.out_of_allowance(
            "active_ats", quota, job_cost=digest.ACTIVE_ATS_PAGE))
        self.assertFalse(digest.can_spend(
            "active_ats", quota, cost=1, job_cost=digest.ACTIVE_ATS_PAGE))

    def test_an_unknown_allowance_is_spent_rather_than_assumed_empty(self):
        """First run, or a lost state file. Assuming empty would disable the
        only sources that can answer "posted this week"."""
        self.assertFalse(digest.out_of_allowance("active_ats", {}))

    def test_a_held_back_feed_wakes_up_after_the_plan_resets(self):
        """The bug this exists to prevent: a feed stops once it is under its
        reserve, so it never calls again, so it never learns the plan reset --
        and stays off permanently. The reset instant is recorded when the
        headers report it, and a quota past that instant counts as unknown.
        """
        past = digest.time.time() - 60
        self.assertFalse(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 0, "reset_at": past}))

    def test_it_stays_held_back_before_the_reset(self):
        future = digest.time.time() + 3600
        self.assertTrue(digest.out_of_allowance(
            "active_ats", {"requests_remaining": 0, "reset_at": future}))

    def test_each_feed_is_measured_against_its_own_plan(self):
        """The Indeed feed allows 50 requests where the others allow 25, so a
        single shared reserve would either waste one plan or overrun another."""
        self.assertIn("indeed", digest.FREE_TIER)
        self.assertIn("jsearch", digest.FREE_TIER)
        self.assertIn("active_ats", digest.FREE_TIER)
        self.assertIn("linkedin_api", digest.FREE_TIER)
        # 25 a month does not cover a 30-day month at one call a day; 50 does.
        # That difference is exactly why the reserve is per feed rather than one
        # shared number.
        self.assertEqual(digest.FREE_TIER["active_ats"]["requests"], 25)
        self.assertEqual(digest.FREE_TIER["linkedin_api"]["requests"], 25)
        self.assertEqual(digest.FREE_TIER["indeed"]["requests"], 50)
        self.assertEqual(digest.FREE_TIER["jsearch"]["reserve_requests"], 40)
        for feed, plan in digest.FREE_TIER.items():
            with self.subTest(feed=feed):
                self.assertLess(plan["reserve_requests"], plan["requests"],
                                f"{feed} reserves its whole plan")

    def test_an_unknown_feed_is_never_silently_unlimited(self):
        self.assertTrue(digest.out_of_allowance("not_a_feed", {}))


class TestQuotaResetIsRecorded(unittest.TestCase):
    """RapidAPI reports seconds-until-reset, which is only meaningful at the
    moment it is read. Stored as an absolute instant so a later run can tell
    whether the plan has since rolled over."""

    def _response(self, headers):
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: []
        response.text = ""
        response.headers = headers
        return response

    def test_seconds_until_reset_becomes_an_instant(self):
        quota = {}
        before = digest.time.time()
        headers = {"x-ratelimit-requests-remaining": "24",
                   "x-ratelimit-requests-reset": "86400"}
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response(headers)):
            digest.fetch_active_ats("key", quota=quota)
        self.assertGreaterEqual(quota["reset_at"], before + 86400)

    def test_observation_time_is_recorded_with_the_quota(self):
        quota = {}
        before = digest.time.time()
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response({
                                   "x-ratelimit-requests-remaining": "199"})):
            digest.fetch_active_ats("key", quota=quota)
        self.assertGreaterEqual(quota["observed_at"], before)

    def test_a_missing_reset_header_is_not_fatal(self):
        quota = {}
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response({})):
            digest.fetch_active_ats("key", quota=quota)
        self.assertNotIn("reset_at", quota)


class TestFetchIndeed(unittest.TestCase):
    """Indeed, which no other feed here reaches.

    One row exactly as the API returned it on 7 Sep 2026. Probed the same day:
    20 rows, 8 passing these filters, 7 of those 8 published within a week --
    and Saudi employers the ATS feeds do not carry, Rasan and Consulum among
    them.

    Two things this endpoint does NOT have, both confirmed live: any working
    server-side date filter (it answered "Parameter 'datePosted' is unknown"),
    and a single date field. It returns two clocks that disagreed by 76 days on
    the first row sampled.
    """

    ROW = {
        "id": "aWQ9MTQyZjUxMjJiYWI2MjBmMDtjYz1zYQ==",
        "title": "Software Engineer - Full Stack",
        "company": {"name": "rasan", "image": "", "addresses": []},
        "applyUrl": "https://careers.tameeni.com/jobs/7957785-engineer",
        "location": {"country": "المملكة العربية السعودية",
                     "countryCode": "SA", "location": "Riyadh"},
        # Epoch MILLISECONDS. Published 2026-09-06, indexed 2026-09-07.
        "datePublishedTimestamp": 1788652800000,
        "dateOnIndeedTimestamp": 1788801159024,
        "description": "Build payment integrations.",
    }

    def _response(self, rows, status=200, headers=None):
        response = mock.Mock()
        response.status_code = status
        response.json = lambda: {"data": rows}
        response.text = ""
        response.headers = headers or {}
        return response

    def test_a_row_becomes_a_job_the_pipeline_understands(self):
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([self.ROW])):
            job = digest.fetch_indeed("key")[0]
        self.assertEqual(job["company"], "rasan")
        self.assertEqual(job["title"], "Software Engineer - Full Stack")
        self.assertEqual(job["source"], "indeed")
        self.assertIn("Riyadh", job["location"])
        self.assertEqual(job["url"], "https://careers.tameeni.com/jobs/7957785-engineer")
        self.assertEqual(job["description"], "Build payment integrations.")

    def test_the_date_is_when_the_employer_published_not_when_indeed_noticed(self):
        """The two clocks disagreed by 76 days on the first row probed. An
        index date only says a scraper saw an old posting today, so reading it
        would let a months-old role pass a seven-day cutoff as same-day."""
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([self.ROW])):
            job = digest.fetch_indeed("key")[0]
        self.assertEqual(job["first_published"], "2026-09-06")

    def test_a_row_with_no_publish_date_is_undated_rather_than_todays(self):
        """Falling back to the index date would silently relabel it as fresh."""
        row = dict(self.ROW)
        del row["datePublishedTimestamp"]
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([row])):
            job = digest.fetch_indeed("key")[0]
        self.assertEqual(job["first_published"], "")

    def test_it_is_age_gated_and_is_not_an_employer_source(self):
        self.assertNotIn("indeed", digest.EMPLOYER_SOURCES)
        self.assertNotIn("indeed", digest.FETCHERS)
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age(
            [{"source": "indeed", "first_published": "2026-06-01"}], 7, today)
        self.assertEqual(fresh, [])
        self.assertEqual(len(stale), 1)

    def test_it_respects_its_own_free_plan(self):
        with mock.patch.object(digest.requests, "get") as get:
            jobs = digest.fetch_indeed(
                "key",
                quota={"requests_remaining":
                       digest.FREE_TIER["indeed"]["reserve_requests"] - 1,
                       "reset_at": digest.time.time() + 3600})
        self.assertEqual(jobs, [])
        get.assert_not_called()

    def test_no_key_and_failures_are_survivable(self):
        self.assertEqual(digest.fetch_indeed(""), [])
        with mock.patch.object(digest.requests, "get",
                               side_effect=digest.requests.Timeout("slow")):
            self.assertEqual(digest.fetch_indeed("key"), [])
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([], status=429)):
            self.assertEqual(digest.fetch_indeed("key"), [])

    def test_the_request_names_the_country_the_api_demanded(self):
        """It answered 400 MISSING_OR_PARAMETER without countryCode."""
        with mock.patch.object(digest.requests, "get",
                               return_value=self._response([])) as get:
            digest.fetch_indeed("key")
        self.assertIn("countryCode=sa", get.call_args[0][0])


class TestJSearchCountryIsAlwaysTheQuerysCountry(unittest.TestCase):
    """job_country is the parameter this digest sent, not the employer's place.

    Measured live 8 Sep 2026 on `software engineer Saudi Arabia`, sent with
    country=sa. Every row came back job_country='SA' -- including plainly
    American ones, and NOT only remote ones:

        Quest Diagnostics  city='Los Angeles, CA'  country='SA'  remote=False
        Liquid XR          city='Los Angeles, CA'  country='SA'  remote=False
        YO AI Labs         city='Anywhere, CA'     country='SA'  remote=True
        Govcio LLC         city=None               country='SA'  remote=True

    Expanding that 'SA' to "Saudi Arabia" put the word "saudi" into every one
    of those location strings, so a Los Angeles role read as local. Nineteen
    reached the 8 Sep 07:00 run and nine more the run after; both times the age
    gate caught them by luck rather than the location filter by design.

    Real Saudi rows carry a Saudi region code where the US rows carry a state
    code -- 'Riyadh, RIY' and 'Al Khubar, EAS' against 'Los Angeles, CA'. The
    country is trusted only when the city itself, or that region code, says
    Saudi Arabia.
    """

    SEP = "\u060c"  # Arabic comma; JSearch separates city and region with it

    def _job(self, city, remote=False, country="SA"):
        response = mock.Mock()
        response.status_code = 200
        response.json = lambda: {"data": {"jobs": [{
            "job_id": "x1", "job_title": "Software Engineer",
            "employer_name": "Acme", "job_apply_link": "https://acme.test/1",
            "job_city": city, "job_state": None, "job_country": country,
            "job_is_remote": remote,
            "job_posted_at_datetime_utc": "2026-09-08T00:00:00Z",
        }]}}
        response.text = ""
        response.headers = {}
        with mock.patch.object(digest.requests, "get", return_value=response):
            return digest.fetch_jsearch("key")[0]

    def _assert_local(self, city, **kw):
        job = self._job(city, **kw)
        self.assertTrue(digest.matches_filters(job),
                        f"location {job['location']!r} should read as Saudi")

    def _assert_foreign(self, city, **kw):
        job = self._job(city, **kw)
        self.assertFalse(digest.matches_filters(job),
                         f"location {job['location']!r} must not read as Saudi")

    def test_an_american_city_is_not_saudi_even_when_onsite(self):
        """The case that leaked: not remote, real US city, country echoed."""
        self._assert_foreign(f"Los Angeles{self.SEP} CA")
        self._assert_foreign(f"Culver City{self.SEP} CA")

    def test_an_american_remote_row_is_not_saudi(self):
        self._assert_foreign(f"Anywhere{self.SEP} CA", remote=True)
        self._assert_foreign(f"Anywhere{self.SEP} OR", remote=True)

    def test_a_row_with_no_city_at_all_is_not_saudi(self):
        """Nothing but the echoed country, which is no evidence."""
        self._assert_foreign(None, remote=True)

    def test_a_saudi_city_named_in_english_is_kept(self):
        self._assert_local(f"Riyadh{self.SEP} RIY")

    def test_a_saudi_city_named_in_arabic_is_kept(self):
        """Why the country was expanded in the first place."""
        self._assert_local("\u0627\u0644\u0631\u064a\u0627\u0636")

    def test_a_saudi_city_the_keyword_list_does_not_know_is_kept(self):
        """'Al Khubar' is Khobar and "al-'Aqrabiyyah" is nowhere in
        LOCATION_INCLUDE, so the region code is what saves these real rows."""
        self._assert_local(f"Al Khubar{self.SEP} EAS")
        self._assert_local(f"al-'Aqrabiyyah{self.SEP} EAS")
        self._assert_local(f"Najd{self.SEP} RIY")

    def test_remote_work_from_riyadh_still_counts(self):
        self._assert_local(f"Riyadh{self.SEP} RIY", remote=True)

    def test_every_saudi_region_code_is_recognised(self):
        for code in digest.SAUDI_REGION_CODES:
            with self.subTest(code=code):
                self._assert_local(f"Somewhere{self.SEP} {code}")

    def test_no_us_state_code_is_mistaken_for_a_saudi_region(self):
        for code in ("CA", "OR", "DC", "NY", "TX", "WA", "UT", "MA", "IL"):
            with self.subTest(code=code):
                self._assert_foreign(f"Somewhere{self.SEP} {code}")


class TestTheRunBudgetLosesWorkNotTheDigest(unittest.TestCase):
    """The execute job's timeout is not graceful: it cancels the job, so the
    state artifact is never packaged and seen.json never records cards Discord
    already holds. RUN_BUDGET_SECONDS exists so digest.py finishes on its own,
    and every check it adds must drop optional work rather than the delivery."""

    ENV = dict(DISCORD_WEBHOOK_URL="https://discord.test/hook",
               OPENROUTER_API_KEY="k")

    def test_the_phases_sum_under_the_workflow_wall(self):
        """The whole point of the constant. If these three plus delivery can
        exceed the execute job's timeout, the budget buys nothing."""
        wall = 20 * 60
        self.assertLess(
            digest.RUN_BUDGET_SECONDS + digest.RANK_BUDGET_SECONDS
            + fit_check_module.CHECK_BUDGET_SECONDS, wall)

    def _run_with_no_budget(self, **kwargs):
        with mock.patch.object(digest, "RUN_BUDGET_SECONDS", -1):
            return run_main(self.ENV, {"jsearch_b": {"score": 90, "why": "Supported role"}}, **kwargs)

    def test_a_spent_budget_still_delivers_and_still_records_what_it_sent(self):
        """The one thing the budget must never cost. A run that delivers and
        then fails to write seen.json re-delivers the same cards tomorrow,
        which is the exact failure the timeout used to cause."""
        posts, work = self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])],
                                               link_statuses=[])
        self.assertTrue(posts, "a starved run still has to deliver")
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            self.assertTrue(json.load(handle), "delivery was not remembered")

    def test_an_unchecked_link_is_inconclusive_rather_than_missing(self):
        """settle_link is the only writer of link_status and three readers index
        it rather than .get() it, so a skipped check must set the key. The card
        already renders LINK_UNKNOWN as 'link check inconclusive'."""
        with mock.patch.object(digest, "settle_link") as settle:
            posts, _ = self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])],
                                                link_statuses=[])
        settle.assert_not_called()
        self.assertIn("link check inconclusive", "\n".join(posts))

    def test_a_spent_budget_skips_enrichment_rather_than_the_digest(self):
        """Enrichment is dropped last and degrades to whatever text the fetcher
        supplied, which jd_grade then labels honestly."""
        with mock.patch.object(digest, "enrich_descriptions") as enrich:
            posts, _ = self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])],
                                                link_statuses=[])
        enrich.assert_not_called()
        self.assertTrue(posts)

    def test_a_spent_budget_skips_board_discovery(self):
        """Pure investment in tomorrow's run, so the first thing worth dropping:
        five ATS probes per seed employer buy nothing for this morning."""
        with mock.patch.object(digest, "discover_boards") as discover:
            self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])], link_statuses=[])
        discover.assert_not_called()

    CURATED = [{"name": "Hala", "source": "ashby", "token": "hala"},
               {"name": "Salla", "source": "lever", "token": "salla"}]

    def test_a_spent_budget_still_reads_every_hand_curated_company(self):
        """COMPANIES is curated and is never rationed; only discovered boards
        are droppable. Getting this backwards would let a slow morning silently
        stop reading the employers this digest was built around."""
        with mock.patch.object(digest, "fetch_company", return_value=[]) as fetch:
            self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])], link_statuses=[],
                                     companies=self.CURATED)
        read = [call.args[0]["token"] for call in fetch.call_args_list]
        self.assertEqual(read, [c["token"] for c in self.CURATED])

    def test_a_spent_budget_drops_the_discovered_boards_after_them(self):
        """The other half of the same guard: curated first and always, then
        discovered boards only while there is budget left."""
        work = tempfile.mkdtemp(prefix="digest_budget_")
        for name in ("profile.json", "resume.md"):
            source = os.path.join(REPO, name)
            if os.path.exists(source):
                shutil.copy(source, work)
        with open(os.path.join(work, "boards.json"), "w", encoding="utf-8") as handle:
            json.dump({"nabla": {"company": "Nabla", "source": "ashby",
                                 "token": "nabla", "found": "2026-09-03"}}, handle)
        with mock.patch.object(digest, "fetch_company", return_value=[]) as fetch:
            self._run_with_no_budget(jobs=[dict(REAL_JOBS[1])], link_statuses=[],
                                     work=work, companies=self.CURATED)
        shutil.rmtree(work)
        read = [call.args[0]["token"] for call in fetch.call_args_list]
        self.assertEqual(read, [c["token"] for c in self.CURATED])
        self.assertNotIn("nabla", read)


class TestConfirmedBoardsIsBounded(unittest.TestCase):
    """Board discovery adds entries every week with no ceiling of its own, and
    the company loop spends a request and half a second on each one."""

    @staticmethod
    def _boards(count, last_read=None):
        return {f"b{i}": {"company": f"B{i}", "source": "ashby",
                          "token": f"b{i}",
                          **({"last_read": last_read(i)} if last_read else {})}
                for i in range(count)}

    def test_the_cap_bounds_what_one_run_reads(self):
        self.assertEqual(len(digest.confirmed_boards(self._boards(80), limit=5)), 5)

    def test_overflow_rotates_so_every_board_is_served(self):
        boards = self._boards(digest.MAX_BOARDS_PER_RUN + 1)
        first = digest.confirmed_boards(boards)
        for board in first:
            boards[board["token"]]["last_read"] = "2026-09-21"
        second = digest.confirmed_boards(boards)
        self.assertEqual(len(first), digest.MAX_BOARDS_PER_RUN)
        self.assertEqual({board["token"] for board in first + second}, set(boards))

    def test_generated_board_state_is_served_by_the_bounded_rotation(self):
        boards = digest.load_boards() if os.path.exists(digest.BOARDS_FILE) else {}
        expected = min(len(digest.confirmed_boards(boards, limit=None)),
                       digest.MAX_BOARDS_PER_RUN)
        self.assertEqual(len(digest.confirmed_boards(boards)), expected)

    def test_the_least_recently_read_board_is_read_first(self):
        """Newest-first would starve exactly the boards discovered earliest and
        validated longest, so a board that misses one run leads the next."""
        boards = self._boards(3, last_read=lambda i: f"2026-09-0{3 - i}")
        order = [b["token"] for b in digest.confirmed_boards(boards)]
        self.assertEqual(order, ["b2", "b1", "b0"])

    def test_a_board_never_read_comes_before_every_board_that_has_been(self):
        boards = self._boards(2, last_read=lambda i: "2026-01-01")
        boards["fresh"] = {"company": "Fresh", "source": "ashby", "token": "fresh"}
        self.assertEqual(digest.confirmed_boards(boards)[0]["token"], "fresh")

    def test_a_curated_company_is_never_returned_as_a_discovered_board(self):
        curated = digest.COMPANIES[0]
        boards = {"dupe": {"company": curated["name"], "source": curated["source"],
                           "token": curated["token"]}}
        self.assertEqual(digest.confirmed_boards(boards), [])


# The posting that produced this change: 5187 characters, responsibilities at
# 217, Minimum Qualifications at 3958, Skills at 4529. Trimmed to the shape that
# matters, with the same ordering and the same distance between the two ends.
MAADEN_SHAPED_JD = (
    "Why This Role Matters\n"
    "The role transforms data into actionable intelligence.\n\n"
    "What You Will Deliver\n"
    # Repeated until it clears JD_RANK_CHARS, because the point of the
    # fixture is a posting the window cannot cover. The real one is 5187.
    + " * Develop and deploy machine learning and Generative AI solutions.\n" * 60 +
    "\nMinimum Qualifications\n"
    " * Bachelor's degree in Data Science, Artificial Intelligence, Computer "
    "Science, or a related quantitative discipline.\n\n"
    "Experience\n"
    " * 4-6 years of experience in data science, machine learning, artificial "
    "intelligence, or advanced analytics roles.\n\n"
    "Skills That Matter\n"
    " * Model Fine-Tuning & Evaluation\n"
    " * TensorFlow, PyTorch & Scikit-Learn\n"
)


class TestWhatTheScorerIsAllowedToRead(unittest.TestCase):
    """The whole defect, in one place.

    A Ma'aden posting was delivered on 2026-09-08 as a Strong match at fit 91.
    Watheq scored the same posting 67 and named two gaps. The digest's ranker
    had been shown characters 217-2217 of 5187: every responsibility, no degree,
    no year count, no fine-tuning.
    """

    def test_the_signal_window_alone_walks_past_the_qualifications(self):
        """Not a regression test -- a record of why a window is not enough. No
        single contiguous span reaches both ends of this posting, and anchoring
        on the LAST heading lands on Skills and skips the qualifications above
        it."""
        window = job_archive.jd_signal(MAADEN_SHAPED_JD, 2000)

        self.assertIn("Generative AI", window)
        self.assertNotIn("Bachelor's degree", window)
        self.assertNotIn("4-6 years", window)

    def test_the_requirement_lines_are_found_wherever_they_sit(self):
        found = " ".join(job_archive.jd_requirements(MAADEN_SHAPED_JD))

        self.assertIn("Bachelor's degree", found)
        self.assertIn("4-6 years", found)

    def test_a_posting_that_states_no_requirement_yields_nothing(self):
        """Silence must stay silence. Inventing a requirement here would put a
        gap on a card that the posting never asked for."""
        self.assertEqual(job_archive.jd_requirements(
            "We are a friendly team building great products together."), [])

    def test_a_bullet_marker_does_not_survive_into_the_prompt(self):
        for line in job_archive.jd_requirements(MAADEN_SHAPED_JD):
            self.assertFalse(line.startswith(("*", "-", "•")), line)

    def test_the_budget_bounds_what_one_job_carries(self):
        crowded = "\n".join(
            f"* Requirement number {n}: {n} years of experience with tooling."
            for n in range(200))
        found = job_archive.jd_requirements(crowded, limit=300)

        self.assertTrue(found)
        self.assertLessEqual(sum(len(line) + 2 for line in found), 300)

    def test_a_qualifications_block_written_as_prose_is_still_found(self):
        """Sentence splitting is what recovers these. Without it a paragraph
        that happens to contain the word 'required' is one over-long chunk and
        is dropped whole: 14 archived postings lost their requirement that way,
        11 with the split."""
        prose = ("About us. " * 40 + "We build things. The successful candidate "
                 "holds a Bachelor's degree in Computer Science. They will join "
                 "a growing team. " + "More about us. " * 40)
        found = " ".join(job_archive.jd_requirements(prose))

        self.assertIn("Bachelor's degree in Computer Science", found)

    def test_the_ranker_is_told_when_it_is_reading_a_window(self):
        """description_kind is computed on the STORED text, so it reported
        'full' while the model was handed a third of a posting -- on 48 of the
        49 archived postings whose requirements the window cut."""
        listing = self._listing(MAADEN_SHAPED_JD)

        self.assertEqual(listing["coverage"], "windowed")
        self.assertEqual(listing["description_kind"], job_archive.GRADE_FULL)
        self.assertTrue(listing["hard_requirements"])

    def test_a_posting_that_fits_is_reported_as_complete(self):
        listing = self._listing("Requirements. " + "Short posting. " * 20)
        self.assertEqual(listing["coverage"], "complete")

    @staticmethod
    def _listing(description):
        job = {"id": "j", "title": "T", "company": "C", "location": "Riyadh",
               "description": description}
        captured = {}

        def fake_chat(prompt, *args, **kwargs):
            captured["prompt"] = prompt
            raise RuntimeError("stop here; the prompt is what is under test")

        with mock.patch.object(digest, "chat_json", side_effect=fake_chat):
            digest.rank_jobs([job], "key", evidence="resume")
        listing = json.loads(captured["prompt"].split("Jobs:\n", 1)[1])
        return listing[0]


class TestTheRankingContract(unittest.TestCase):
    """Prompt pins, in the style of Watheq's own ai-contracts tests. The rubric
    is the only thing standing between an unanchored 0-100 and a digest of 90s.
    """

    def _prompt(self):
        captured = {}

        def fake_chat(prompt, schema, *args, **kwargs):
            captured.update(prompt=prompt, schema=schema)
            raise RuntimeError("stop here")

        job = {"id": "j", "title": "Senior AI Engineer", "company": "C",
               "location": "Riyadh", "description": MAADEN_SHAPED_JD}
        with mock.patch.object(digest, "chat_json", side_effect=fake_chat):
            digest.rank_jobs([job], "key", evidence="resume")
        return captured

    def test_the_band_anchors_are_watheqs(self):
        prompt = self._prompt()["prompt"]

        self.assertIn("80+ means strong evidence-backed fit", prompt)
        self.assertIn("60-79 means competitive with gaps", prompt)
        self.assertIn("below 60 means significant gaps", prompt)

    def test_the_ninety_ceiling_demands_every_requirement(self):
        """The old rubric asked only that hard requirements be 'clearly
        covered', which is what let a posting with an unmet minimum
        qualification reach 91."""
        self.assertIn("Never score above 90 unless every requirement",
                      self._prompt()["prompt"])

    def test_the_rubric_names_education_as_a_category_of_its_own(self):
        prompt = self._prompt()["prompt"]

        self.assertIn("education 15", prompt)
        for name in digest.RANK_CATEGORIES:
            self.assertIn(name, self._prompt()["schema"]["properties"]["scores"]
                          ["items"]["properties"]["categories"]["properties"])

    def test_the_categories_are_not_required_to_sum_to_the_score(self):
        """Watheq documents that the models' own sums run 30+ points above
        their stated overall on partial matches, so forcing agreement is what
        pushes a weak resume into the hireable band."""
        self.assertIn("do NOT have to sum to the overall score",
                      self._prompt()["prompt"])

    def test_the_requirements_block_is_named_as_the_authority(self):
        prompt = self._prompt()["prompt"]

        self.assertIn("hard_requirements", prompt)
        self.assertIn("authority on what the role requires", prompt)

    def test_an_unstated_requirement_is_still_never_a_gap(self):
        """Kept unconditional on purpose. Relaxing it on a windowed posting
        would remove the anti-invention guard on exactly the postings whose
        requirement line the extractor missed."""
        self.assertIn("unknown, not a gap", self._prompt()["prompt"])

    def test_every_track_reaches_the_prompt_with_its_pointer(self):
        prompt = self._prompt()["prompt"]

        for track in digest.PROFILE["tracks"]:
            self.assertIn(track["name"], prompt)
            self.assertIn(track["titles"][0], prompt)
            self.assertIn(track["evidence"][:40], prompt)

    def test_the_track_block_points_at_the_resume_rather_than_asserting(self):
        """The tracks land inside <candidate_preferences>, which the prompt
        tells the model proves nothing about capability. Plan 018 pulled
        self-authored capability claims out of that block on purpose, so a
        track that restated what the candidate has built would either be
        discounted and do nothing, or be believed and put 018's defect back."""
        prompt = self._prompt()["prompt"]

        self.assertIn("These lines say where to look; they prove nothing", prompt)
        for track in digest.PROFILE["tracks"]:
            self.assertNotIn("Evidenced by", track["evidence"])
            self.assertTrue(
                track["evidence"].startswith("Judge against"),
                f"{track['name']} asserts capability instead of naming where to look")

    def test_every_thing_the_resume_leads_with_has_a_track_to_land_on(self):
        """Lenovo's "Web Developer" routed to the AI and LLM track on the run of
        2026-09-09. Not a routing bug: no track named frontend, React or web
        existed, so the model picked the least wrong option from a menu with no
        right answer, on a posting that says Agentic AI six times. The resume
        leads with 28,000 lines of React and TypeScript."""
        titles = " ".join(t.lower() for track in digest.PROFILE["tracks"]
                          for t in track["titles"] + [track["name"]])

        for area in ("frontend", "backend", "data", "ai", "web"):
            self.assertIn(area, titles, f"no track a {area} posting can land on")

    def test_the_prompt_states_what_the_candidate_does_not_do(self):
        """He builds WITH models, he does not build models. Without this the
        rubric has no way to tell an LLM application role from a Data Scientist
        posting demanding training and fine-tuning: they share most of their
        vocabulary, so the second reads as a near miss instead of a wrong
        function. The Ma'aden posting asked for Model Fine-Tuning, TensorFlow
        and PyTorch and scored 91."""
        prompt = self._prompt()["prompt"]

        self.assertIn("he BUILDS WITH models, he does not build models", prompt)
        for absent in ("TensorFlow", "PyTorch", "fine-tuning"):
            self.assertIn(absent, prompt)

    def test_no_tracks_means_no_track_section_rather_than_an_empty_one(self):
        self.assertEqual(digest.describe_tracks([]), "")
        self.assertEqual(digest.describe_tracks(None), "")


class TestTheReportedCategories(unittest.TestCase):
    def _result(self, categories):
        jobs = [{"id": "j", "title": "Senior AI Engineer", "description": "Python"}]
        digest.apply_ranking(jobs, {"j": {"score": 80, "why": "fits",
                                          "track": "AI and LLM product engineering",
                                          "categories": categories}})
        return jobs[0]

    @staticmethod
    def _sane():
        return {name: {"score": maximum, "max": maximum, "note": "n"}
                for name, maximum in digest.RANK_CATEGORIES.items()}

    def test_a_sane_breakdown_is_kept_with_the_track(self):
        job = self._result(self._sane())

        self.assertEqual(set(job["categories"]), set(digest.RANK_CATEGORIES))
        self.assertEqual(job["track"], "AI and LLM product engineering")

    def test_a_category_scored_above_its_own_max_is_rejected(self):
        broken = self._sane()
        broken["education"] = {"score": 100, "max": 15, "note": "n"}

        self.assertEqual(self._result(broken)["categories"], {})

    def test_a_category_on_its_own_hundred_point_scale_is_accepted(self):
        """Watheq's evaluation harness records the model emitting each category
        on a 0-100 scale about as often as on the stated weight, and only the
        ratio is ever displayed. Pinning the max would reject honest answers."""
        hundreds = {name: {"score": 70, "max": 100, "note": "n"}
                    for name in digest.RANK_CATEGORIES}

        self.assertEqual(len(self._result(hundreds)["categories"]),
                         len(digest.RANK_CATEGORIES))

    def test_categories_that_overshoot_the_total_are_still_kept(self):
        """The decoupling, asserted rather than commented. Reconciling them is
        what pushes a partial match into the hireable band."""
        job = self._result(self._sane())

        self.assertEqual(job["score"], 80)
        self.assertGreater(sum(c["score"] for c in job["categories"].values()),
                           job["score"])

    def test_a_malformed_breakdown_costs_the_breakdown_and_not_the_score(self):
        job = self._result("not an object at all")

        self.assertEqual(job["categories"], {})
        self.assertEqual(job["score"], 80)
        self.assertTrue(job["ranking_verified"])


class TestTheBelowFloorCooldown(unittest.TestCase):
    """A below-floor drop used to go through remember(), which writes the job
    under the 120-day state TTL. The archives hold one description scored 40,
    40, 40, 45, 50, 50 and 75 unchanged, so one bad morning retired a real job
    until January."""

    JOB = {"id": "x", "url": "https://example.com/x", "source": "greenhouse"}

    DESCRIBED = dict(JOB, company="C", title="Senior Data Engineer",
                     description="Build pipelines. " * 40)

    def test_a_dropped_job_is_deferred_rather_than_remembered(self):
        seen = {}
        digest.defer_below_floor(seen, self.JOB, "2026-09-10")

        self.assertEqual(list(seen), [digest.FLOOR_SEEN_PREFIX
                                      + digest.job_identity(self.JOB)])
        self.assertNotIn(digest.job_identity(self.JOB), seen)

    def test_the_description_is_deferred_too(self):
        """remember() writes both keys and so must this. An aggregator relists
        the same posting under a fresh URL, which gives it a fresh identity --
        65 of the first 122 aggregator cards were repeats, and one role arrived
        under 11 ids in 9 days. Identity alone would let a below-floor posting
        come back tomorrow as a stranger and cost a full enrich-and-rank."""
        seen = {}
        digest.defer_below_floor(seen, self.DESCRIBED, "2026-09-10")

        self.assertIn(digest.FLOOR_SEEN_PREFIX + digest.description_key(self.DESCRIBED),
                      seen)

    def test_a_relisted_copy_under_a_new_url_is_still_cooling(self):
        seen = {}
        digest.defer_below_floor(seen, self.DESCRIBED, "2026-09-10")
        relisted = dict(self.DESCRIBED, id="y", url="https://elsewhere.example/y")

        self.assertNotEqual(digest.job_identity(relisted),
                            digest.job_identity(self.DESCRIBED))
        self.assertTrue(digest.still_cooling(seen, relisted, "2026-09-12"))

    def test_a_different_role_at_the_same_employer_is_not_cooling(self):
        """description_key is scoped to employer AND title, so the cooldown
        must not spread from one rejected role to the next one they post."""
        seen = {}
        digest.defer_below_floor(seen, self.DESCRIBED, "2026-09-10")
        other = dict(self.DESCRIBED, id="z", url="https://example.com/z",
                     title="Senior AI Engineer")

        self.assertFalse(digest.still_cooling(seen, other, "2026-09-12"))

    def test_a_job_with_no_description_still_gets_a_cooldown(self):
        """description_key is empty without text, and an empty key must not be
        written -- it would collide with every other undescribed job."""
        seen = {}
        digest.defer_below_floor(seen, self.JOB, "2026-09-10")

        # One key, and not the bare prefix. An empty description_key would
        # write "floor:" itself, which every undescribed job would then collide
        # on -- one rejected posting would put the whole feed in cooldown.
        self.assertEqual(len(seen), 1)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX, seen)
        self.assertTrue(digest.still_cooling(seen, self.JOB, "2026-09-10"))

    def test_a_job_inside_the_cooldown_is_skipped(self):
        seen = {}
        digest.defer_below_floor(seen, self.JOB, "2026-09-10")

        self.assertTrue(digest.still_cooling(seen, self.JOB, "2026-09-12"))

    def test_a_job_past_the_cooldown_comes_back(self):
        seen = {}
        digest.defer_below_floor(seen, self.JOB, "2026-09-10")
        later = (datetime.date(2026, 9, 10)
                 + datetime.timedelta(days=digest.FLOOR_COOLDOWN_DAYS + 1)).isoformat()

        self.assertFalse(digest.still_cooling(seen, self.JOB, later))

    def test_a_job_that_was_never_deferred_is_not_cooling(self):
        self.assertFalse(digest.still_cooling({}, self.JOB, "2026-09-10"))

    def test_each_kind_of_key_is_pruned_on_its_own_clock(self):
        """A single cutoff was right while every key meant 'delivered'. A floor:
        key means 'scored badly once', which has to expire much sooner or the
        cooldown is a 120-day retirement under a new name."""
        today = datetime.date.today()
        recent = (today - datetime.timedelta(days=1)).isoformat()
        middling = (today - datetime.timedelta(
            days=digest.FLOOR_COOLDOWN_DAYS + 1)).isoformat()
        seen = {"delivered-old": middling, "delivered-new": recent,
                "floor:legacy-stale": middling,
                digest.FLOOR_SEEN_PREFIX + "stale": middling,
                digest.FLOOR_SEEN_PREFIX + "fresh": recent}

        work = tempfile.mkdtemp(prefix="digest_seen_")
        cwd = os.getcwd()
        try:
            os.chdir(work)
            digest.save_seen(seen)
            with open(digest.STATE_FILE, encoding="utf-8") as handle:
                pruned = json.load(handle)
        finally:
            os.chdir(cwd)
            shutil.rmtree(work, ignore_errors=True)

        self.assertIn("delivered-old", pruned, "a delivery keeps the long TTL")
        self.assertIn(digest.FLOOR_SEEN_PREFIX + "fresh", pruned)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX + "stale", pruned,
                         "a cooldown must expire on its own clock")
        self.assertNotIn("floor:legacy-stale", pruned)
