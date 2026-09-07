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
import fit_check as fit_check_module  # noqa: E402
import jd_fetch  # noqa: E402
import job_archive  # noqa: E402


# Postings carry a date RELATIVE TO TODAY, not the literal 2026-07-30 the
# original digest recorded. MAX_JOB_AGE_DAYS drops anything older than a week,
# so a hardcoded date turns every end-to-end test into a time bomb that passes
# until the fixture ages past the cutoff and then fails for a reason that has
# nothing to do with the behaviour under test.
RECENT = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


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

    def _job(self, ceiling, blockers, score=88, why="Strong senior IC match in Riyadh."):
        return {"title": "T", "company": "C", "score": score, "why": why,
                "ranking_verified": True, "description": FULL_JD,
                "fit_check": {"blockers": [{"title": f"b{n}"} for n in range(blockers)],
                              "unclear": [], "strengths": [], "ceiling": ceiling, "note": ""}}

    def test_a_low_ceiling_caps_the_score(self):
        job = self._job("low", 2)
        capped = digest.cap_blocked_scores([job])
        self.assertEqual(capped, [job])
        self.assertEqual(job["score"], digest.BLOCKED_SCORE_CAP)
        self.assertIn("2 verified requirement gaps", job["why"])

    def test_a_mid_ceiling_does_not_cap(self):
        job = self._job("mid", 1)
        self.assertEqual(digest.cap_blocked_scores([job]), [])
        self.assertEqual(job["score"], 88)

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
        strong, review = digest.split_tiers(jobs)
        self.assertEqual(strong, [j for j in jobs if digest.is_strong(j)])
        self.assertEqual(review, [j for j in jobs if not digest.is_strong(j)])

    def test_duplicate_jobs_are_preserved_in_both_tiers(self):
        """split_tiers built the review tier with `job not in strong` -- a dict
        equality compare, O(n^2) with deep comparison. plans/README.md:296
        flagged it and correctly judged the collapse unreachable, since
        strength is a pure function of the dict: two equal dicts are either
        both strong or both not. The predicate refactor makes it a `not
        is_strong(job)` anyway, so this pins that neither tier loses a
        duplicate."""
        weak = {"ranking_verified": True, "score": 40, "description": FULL_JD}
        strong_job = {"ranking_verified": True, "score": 95, "description": FULL_JD}

        strong, review = digest.split_tiers([dict(weak), dict(weak)])
        self.assertEqual((len(strong), len(review)), (0, 2))

        strong, review = digest.split_tiers([dict(strong_job), dict(strong_job)])
        self.assertEqual((len(strong), len(review)), (2, 0))


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


# What the last run_main() passed to check_jobs. See fake_check below.
LAST_CHECK_CALL = {}


def _board_probe_stub(probe_boards):
    """No-network stand-in for the board probe, unless a test asked for the real one."""
    if probe_boards:
        return contextlib.nullcontext()
    return mock.patch.object(digest, "_probe_employer_board", return_value=None)


def run_main(monkey_env, ranking, link_statuses=None, jobs=None, work=None,
             fit_checks=None, probe_boards=False):
    """Run digest.main() fully mocked; return (discord_posts, workdir).

    Pass `work` from a previous call to run a second day against the seen.json
    and archives the first one wrote, which is how repeat delivery is tested.

    `fit_checks` maps a job title to the fit_check result the reality pass
    should attach; anything unnamed is left unchecked, which is the default.

    `digest.check_jobs` is patched unconditionally. No network escapes without
    it either -- `digest.requests` and `openrouter.requests` are the same module
    object, so the fake_post below already covers chat_json -- but that fake
    returns a Discord-shaped 204 with an empty body, which chat_json would fail
    to parse into a check on every strong job of every run. Patching keeps that
    noise out and, more usefully, lets a test say exactly what the reality pass
    found.

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
            job["fit_check"] = (fit_checks or {}).get(
                job["title"], fit_check_module.neutral_result("not checked in test"))

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

    Five consecutive days delivered nothing new: three queries, page one only,
    ten results each over a 30-day window, so the same thirty postings
    recirculated and every one was already in seen.json. A live probe on 3 Sep
    2026 found the cursor pages and that all ten of page two were absent from
    page one -- but also that the plan allows 200 requests a MONTH, so the
    second page has to be rationed rather than always read.
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

    def test_the_default_budget_funds_one_page_per_query_plus_two_deep_reads(self):
        """The budget is a slice of a monthly allowance, so it has to follow the
        query list rather than sit at a number that silently stops paying for
        deep reads the day a query is added."""
        self.assertEqual(digest._request_budget(None),
                         len(digest.AGGREGATOR_QUERIES) + 2)

    def test_a_budget_too_small_to_read_every_query_is_refused(self):
        self.assertEqual(digest._request_budget("1"),
                         len(digest.AGGREGATOR_QUERIES) + 2)

    def test_a_budget_that_only_covers_page_one_reads_page_one(self):
        self.assertEqual(digest.jsearch_page_plan(["a", "b", "c"], 3, day=0), [])

    def test_spare_budget_buys_a_second_page(self):
        self.assertEqual(digest.jsearch_page_plan(["a", "b", "c"], 5, day=0), ["a", "b"])

    def test_the_deep_read_rotates_so_every_query_is_covered(self):
        """Two of three queries get a second page. Fixing which two would leave
        the third permanently shallow."""
        self.assertEqual(digest.jsearch_page_plan(["a", "b", "c"], 5, day=1), ["b", "c"])
        self.assertEqual(digest.jsearch_page_plan(["a", "b", "c"], 5, day=2), ["c", "a"])

    def test_the_plan_never_asks_for_more_pages_than_there_are_queries(self):
        self.assertEqual(digest.jsearch_page_plan(["a"], 5, day=0), ["a"])

    def test_the_second_page_is_fetched_with_the_cursor_and_its_jobs_kept(self):
        pages = [self._page(["1", "2"], cursor="CUR"), self._page(["3", "4"])]
        with mock.patch.object(digest.requests, "get", side_effect=pages) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            jobs = digest.fetch_jsearch("key")
        self.assertEqual(len(jobs), 4)
        self.assertIn("cursor=CUR", get.call_args_list[1].args[0])

    def test_a_query_with_no_cursor_is_not_asked_for_a_second_page(self):
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"])]) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key")
        self.assertEqual(get.call_count, 1)

    def test_a_nearly_exhausted_quota_stops_the_second_page(self):
        """The allowance is monthly. Spending the last of it on extra pages
        would leave the digest with no aggregator at all until it resets, so
        the deep read yields before the reserve is touched."""
        with mock.patch.object(digest.requests, "get",
                               side_effect=[self._page(["1"], cursor="CUR", remaining="5")]) as get, \
             mock.patch.object(digest, "AGGREGATOR_QUERIES", ["data engineer"]), \
             mock.patch.object(digest.time, "sleep", lambda *_: None):
            digest.fetch_jsearch("key")
        self.assertEqual(get.call_count, 1)

    def test_the_remaining_allowance_is_logged(self):
        """200 a month is 6.6 a day. Nothing anywhere reported the balance, so
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
        self.assertIn("no new roles today", body)

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
        self.assertIn("no new roles today", body)
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
        self.assertIn("no new roles today", body)

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
            "jsearch_e": {"score": 74, "why": "Senior IC data role, some gaps"},
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
        ranking = {f"jsearch_cap{i}": {"score": 90 - i * 5,
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

    def test_a_blocked_job_is_delivered_flagged_rather_than_dropped(self):
        """The whole point of plan 018B, end to end.

        A job the ranker loved (93) but whose stated hard requirements the
        resume cannot evidence must still reach the reader -- capped, tiered
        down and carrying its blockers. The complaint was "the card lied", not
        "show me fewer jobs", and drop_below_floor has already run by the time
        the cap lands, so a capped score can never remove a job or get it
        remembered.
        """
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
        }
        blocked = {
            "blockers": [
                {"title": "No BigQuery or GCP evidence", "severity": "high"},
                {"title": "No Bachelor's degree in Computer Science", "severity": "high"},
            ],
            "unclear": [], "strengths": [], "ceiling": "low", "note": "",
        }

        posts, work = run_main(
            self.ENV, ranking,
            fit_checks={"Senior Software Engineer (AI Systems)": blocked})

        body = "\n".join(posts)
        self.assertIn("Senior Software Engineer (AI Systems)", body,
                      "a capped job must still be delivered")
        self.assertIn(f"fit {digest.BLOCKED_SCORE_CAP}", body)
        self.assertIn("2 verified requirement gaps", body)

        with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
            seen = json.load(handle)
        self.assertIn("url:https://example.com/b", seen,
                      "delivered jobs are remembered normally")

        # Selected by title, not by index: the jobs the ranking left out fall
        # back to keyword_score, which can exceed a capped 69 and sort above it.
        snapshot = self._snapshot(work, "Senior Software Engineer (AI Systems)")
        self.assertEqual(snapshot["tier"], "Worth reviewing",
                         "a capped job must not wear a Strong matches label")
        self.assertEqual(snapshot["ceiling"], "low")
        self.assertEqual(len(snapshot["blockers"]), 2)

        card = job_archive.job_card_payload(snapshot)
        self.assertIn("Low ceiling, 2 blocker(s)", card["embeds"][0]["footer"]["text"])
        gaps = [f for f in card["embeds"][0]["fields"] if f["name"].startswith("Gaps")]
        self.assertEqual(len(gaps), 1, "the blockers must be readable on the card")
        self.assertIn("No BigQuery or GCP evidence", gaps[0]["value"])

    def _snapshot(self, work, title):
        path = os.path.join(work, "jobs", datetime.date.today().isoformat() + ".json")
        with open(path, encoding="utf-8") as handle:
            snapshots = json.load(handle)["jobs"]
        return next(s for s in snapshots if s["title"] == title)

    def test_only_strong_tier_jobs_get_the_paid_reality_check(self):
        """One model call per checked job, so WHICH jobs are checked is a cost
        control, not a detail. is_strong requires a verified score >= 80 with a
        real JD; everything else must be left alone."""
        ranking = {
            "jsearch_b": {"score": 93, "why": "Senior IC LLM role, requirements covered"},
            "jsearch_a": {"score": 55, "why": "Lead title, seniority above target"},
        }

        run_main(self.ENV, ranking)

        self.assertEqual(LAST_CHECK_CALL["titles"],
                         ["Senior Software Engineer (AI Systems)"],
                         "only the verified 80+ job with a JD should be checked")
        self.assertGreater(LAST_CHECK_CALL["resume_chars"], 0,
                           "the check must receive resume evidence to check against")

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
        self.assertEqual(len(card["embeds"][0]["fields"]), 2, "no gaps field when clean")

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
                "key", quota={"jobs_remaining": digest.ACTIVE_ATS_JOBS_RESERVE - 1})
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
            {"active_ats": {"jobs_remaining": digest.ACTIVE_ATS_JOBS_RESERVE - 1}})
        quotas = digest.load_api_quotas()
        with mock.patch.object(digest.requests, "get") as get:
            self.assertEqual(
                digest.fetch_active_ats("key", quotas["active_ats"]), [])
        get.assert_not_called()

    def test_one_feed_running_low_does_not_stop_the_other(self):
        """The failure a shared counter would cause: the ATS feed near its cap
        silencing LinkedIn, which has its own untouched allowance."""
        digest.save_api_quotas(
            {"active_ats": {"jobs_remaining": digest.ACTIVE_ATS_JOBS_RESERVE - 1},
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
    """The cutoff the operator asked for: nothing older than a week.

    first_published was written by every fetcher and read by NOTHING until now
    (digest.py said so outright), so "freshness" meant only "not delivered
    before". A posting could be four months old and count as new the first time
    it was seen. That is how a closed HudHud Maps role from a month earlier
    reached the 7 Sep digest.

    Undated postings are KEPT and flagged, not dropped: Pinpoint publishes no
    date at all and Greenhouse only sometimes, so dropping would silently
    remove those employers entirely.
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

    def test_an_undated_posting_is_kept_and_marked(self):
        """Pinpoint publishes no date. Dropping would remove Tabby entirely."""
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("", source="pinpoint")], 7, today)
        self.assertEqual(stale, [])
        self.assertEqual(len(fresh), 1)
        self.assertIsNone(fresh[0]["age_days"])

    def test_an_unparseable_date_is_treated_as_undated_not_as_stale(self):
        today = datetime.date(2026, 9, 7)
        fresh, stale = digest.split_by_age([self._job("not-a-date")], 7, today)
        self.assertEqual(stale, [])
        self.assertIsNone(fresh[0]["age_days"])

    def test_the_age_is_recorded_so_the_card_can_show_it(self):
        today = datetime.date(2026, 9, 7)
        fresh, _ = digest.split_by_age([self._job("2026-09-04")], 7, today)
        self.assertEqual(fresh[0]["age_days"], 3)

    def test_a_future_date_is_not_negative_age(self):
        """Timezones and ingestion delay can put a posting a few hours ahead."""
        today = datetime.date(2026, 9, 7)
        fresh, _ = digest.split_by_age([self._job("2026-09-08")], 7, today)
        self.assertEqual(fresh[0]["age_days"], 0)


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

    def test_an_undated_card_says_the_date_is_unknown(self):
        """Pinpoint publishes no date. The card must not imply the cutoff was
        enforced on a posting whose age nobody knows."""
        job = dict(REAL_JOBS[0], title="Senior AI Engineer", first_published="")
        ranking = {"jsearch_a": {"score": 88, "why": "Senior IC role, requirements covered"}}
        posts, work = run_main(TestEndToEnd.ENV, ranking, jobs=[job],
                               link_statuses=[digest.LINK_OPEN])
        shutil.rmtree(work, ignore_errors=True)
        self.assertIn("posting date unknown", "\n".join(posts))


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


class TestTheAgeGateSparesEmployerBoards(unittest.TestCase):
    """Age is a proxy for "probably dead". True of an aggregator, false of a board.

    An employer's own ATS lists only roles it is still hiring for and removes
    them when it stops, so a 60-day-old Greenhouse posting is 60 days of a real
    open vacancy. An aggregator index keeps serving a posting long after the
    employer pulled it -- which is the whole reason a closed HudHud Maps role
    reached the 7 Sep digest.

    Measured live 7 Sep 2026 across the 169 postings on the directly-fetched
    boards: 14 were 7 days old or less, 37 were 8-30 days, 73 were over 30 days
    and 45 (all Tabby, via Pinpoint) carried no date at all. Gating them on age
    would have discarded 73 open vacancies to solve a problem those boards do
    not have. Liveness carries them instead: verify_link already drops a pulled
    posting, and fetch_workable and fetch_ashby skip unpublished ones at fetch.
    """

    def _job(self, source, first_published):
        return {"company": "Careem", "title": "Backend Engineer", "source": source,
                "url": "https://boards.greenhouse.io/careem/jobs/1",
                "first_published": first_published}

    def test_an_old_posting_on_the_employers_own_board_is_kept(self):
        today = datetime.date(2026, 9, 7)
        for source in ("greenhouse", "ashby", "workable", "lever", "pinpoint"):
            with self.subTest(source=source):
                fresh, stale = digest.split_by_age(
                    [self._job(source, "2026-06-01")], 7, today)
                self.assertEqual(stale, [], f"{source} should not be age-gated")
                self.assertEqual(fresh[0]["age_days"], 98)

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

    def test_the_exempt_posting_still_reports_its_real_age(self):
        """Exempt from the cutoff, not from the truth: the card still says how
        old it is, so nothing implies a week-old posting that is not one."""
        today = datetime.date(2026, 9, 7)
        fresh, _ = digest.split_by_age([self._job("greenhouse", "2026-08-01")], 7, today)
        self.assertEqual(digest.age_label(fresh[0]["age_days"]), "posted 37 days ago")


class TestDirectBoardSourcesStaysHonest(unittest.TestCase):
    def test_it_names_exactly_the_per_company_fetchers(self):
        """DIRECT_BOARD_SOURCES is written out by hand because it is defined
        above FETCHERS. Adding a fetcher without adding it here would silently
        age-gate a real employer board."""
        self.assertEqual(digest.DIRECT_BOARD_SOURCES, frozenset(digest.FETCHERS))

    def test_the_ats_index_is_an_employer_source_but_not_a_direct_board(self):
        self.assertIn("active_ats", digest.EMPLOYER_SOURCES)
        self.assertNotIn("active_ats", digest.DIRECT_BOARD_SOURCES)


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
        self.assertNotIn("linkedin_api", digest.DIRECT_BOARD_SOURCES)

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
