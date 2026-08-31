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
import jd_fetch  # noqa: E402
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

    def test_ranker_denies_provider_data_collection(self):
        captured = {}

        def fake(*args, **kwargs):
            captured.update(kwargs)
            return {"scores": []}

        with mock.patch.object(digest, "chat_json", side_effect=fake):
            self.assertEqual(digest.rank_jobs([REAL_JOBS[0]], "key"), {})

        self.assertEqual(captured["provider"].get("data_collection"), "deny")
        self.assertTrue(captured["provider"]["require_parameters"])


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


def run_main(monkey_env, ranking, link_statuses=None, jobs=None, work=None):
    """Run digest.main() fully mocked; return (discord_posts, workdir).

    Pass `work` from a previous call to run a second day against the seen.json
    and archives the first one wrote, which is how repeat delivery is tested.
    """
    posts = []

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
    old_cwd = os.getcwd()
    os.chdir(work)
    if link_statuses is None:
        link_statuses = [digest.LINK_OPEN] * len(supplied)
    try:
        with mock.patch.dict(os.environ, monkey_env, clear=False), \
             mock.patch.object(digest, "fetch_jsearch", return_value=[dict(j) for j in supplied]), \
             mock.patch.object(digest, "COMPANIES", []), \
             mock.patch.object(digest, "verify_link", side_effect=link_statuses), \
             mock.patch.object(digest, "rank_jobs", return_value=ranking), \
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


class TestDeliveryFloor(unittest.TestCase):
    def _jobs(self):
        return [
            {"id": "good", "title": "Senior AI Engineer", "description": "Python LLM",
             "company": "A", "score": 55, "ranking_verified": True},
            {"id": "weak", "title": "Senior Data Engineer", "description": "SQL",
             "company": "B", "score": 45, "ranking_verified": True},
            {"id": "edge", "title": "Senior Backend Engineer", "description": "Go",
             "company": "C", "score": 50, "ranking_verified": True},
        ]

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
        with mock.patch.object(digest, "MIN_DELIVER_SCORE", 60):
            kept, dropped = digest.drop_below_floor(self._jobs())

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
        "first_published": "2026-08-29", "source": "jsearch", "token": "",
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
        closed. Losing the rescue must never cost the job."""
        posts, work = self._linkedin_run([digest.LINK_OPEN, digest.LINK_CLOSED])
        shutil.rmtree(work)
        body = "\n".join(posts)

        self.assertIn("sa.linkedin.com/jobs/view/senior-ai-engineer-4442", body)
        self.assertNotIn("job-boards.greenhouse.io", body)
        self.assertIn("Senior AI Engineer", body)

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

    def test_a_webhook_fallback_delivery_is_remembered_so_it_is_not_resent(self):
        env = dict(self.ENV, INTERACTIVE_WORKER_URL="https://worker.test", WORKER_CALLBACK_SECRET="secret")
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
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
            seen = json.load(handle)
        # One posting remembered two ways: its identity, and its description.
        self.assertEqual(len([k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)]), 1)

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
        # An aggregator posting is remembered by URL, never by the per-request
        # id it arrived with, plus one key for its description text.
        identities = {k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)}
        self.assertEqual(identities, {"url:https://example.com/b", "url:https://example.com/e"})
        self.assertEqual(len(seen) - len(identities), 2)
        self.assertFalse(identities & {"jsearch_b", "jsearch_e"})

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

    def test_the_same_posting_under_a_new_id_is_not_delivered_twice(self):
        """JSearch mints a fresh opaque id per request, so seen.json keyed on it
        never matched and 65 of the first 122 aggregator cards were repeats. One
        JAL frontend role arrived on 9 days under 11 ids."""
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"}}
        posts, work = run_main(self.ENV, ranking)
        self.assertIn("Senior Software Engineer (AI Systems)", "\n".join(posts))

        # Tomorrow: same postings, same URLs, brand new ids, and a LinkedIn
        # tracking parameter that also changes on every response.
        tomorrow = [dict(job, id=f"jsearch_{job['id']}_regenerated",
                         url=f"{job['url']}?refId=abc%3D%3D&trackingId=xyz")
                    for job in REAL_JOBS]
        posts, _ = run_main(self.ENV, ranking, jobs=tomorrow, work=work)

        body = "\n".join(posts)
        self.assertIn("no new roles today", body)
        self.assertNotIn("Senior Software Engineer (AI Systems)", body)

    def test_a_relisted_posting_with_a_new_url_is_caught_by_its_description(self):
        """Jooble relisted one posting under two of its own ids in the first 11
        archived days. The description is the last thing that does not move."""
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"}}
        _, work = run_main(self.ENV, ranking)

        relisted = [dict(job, id=f"{job['id']}_new", url=f"{job['url']}-relisted-elsewhere")
                    for job in REAL_JOBS]
        posts, _ = run_main(self.ENV, ranking, jobs=relisted, work=work)

        self.assertIn("no new roles today", "\n".join(posts))

    def test_a_genuinely_new_posting_still_gets_through(self):
        ranking = {"jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
                   "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"}}
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
        # The closed posting is not remembered at all, so it stays deliverable
        # if the employer reopens it.
        self.assertEqual({k for k in seen if not k.startswith(digest.JD_SEEN_PREFIX)},
                         {"url:https://example.com/e"})

    def test_gemini_down_keyword_fallback_still_delivers(self):
        posts, work = run_main(self.ENV, None)
        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertIn("fit ", body)
        self.assertNotIn("tailored resume:", body)  # tailor also failed -> no links, no crash

    def test_a_job_below_the_floor_is_neither_delivered_nor_offered_again(self):
        """The Datamatics role scores 41 and stops being a card.

        It is still remembered, so tomorrow's run does not re-fetch it,
        re-score it and reject it again for the next 120 days.
        """
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 41, "why": "Adjacent architecture work, most requirements missing"},
        }

        posts, work = run_main(self.ENV, ranking)

        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body)
        self.assertNotIn("Senior Data Architect", body)
        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertIn("url:https://example.com/e", seen)

    def test_the_drift_average_is_measured_before_the_floor_cuts(self):
        """check_drift compares today's average against the prior window. Taking
        it after a cut that removes exactly the low scores would make a stale
        profile look like a healthy one."""
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_e": {"score": 41, "why": "Adjacent architecture work, most requirements missing"},
        }

        posts, work = run_main(self.ENV, ranking)

        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            row = json.load(handle)[-1]
        self.assertEqual(row["avg_score"], 67.0)
        self.assertEqual(row["new"], 2)
        self.assertEqual(row["below_floor"], 1)
        self.assertEqual(row["delivered"], 1)

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
