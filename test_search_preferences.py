import unittest
from unittest import mock

import test_digest as fixtures
import digest
import job_archive
import jd_fetch
import json
import test_strict_alerts as strict
from search_preferences import employment_status, load_preferences


class TestSearchPreferences(unittest.TestCase):
    def test_full_time_contract_is_excluded_and_unknown_is_not_inferred(self):
        for raw, want in (("FULL_TIME", "full_time"), ("Full-time", "full_time"),
                          (["FULL_TIME", "CONTRACTOR"], "excluded"), ("Permanent", "unknown"),
                          ("Part-time", "excluded"), ("", "unknown")):
            with self.subTest(raw=raw):
                self.assertEqual(employment_status({"employment_type": raw}), want)
        job = dict(fixtures.REAL_JOBS[1], employment_type="CONTRACTOR")
        self.assertFalse(digest.matches_filters(job))

    def test_negated_and_malformed_employment_cannot_become_full_time(self):
        self.assertEqual(employment_status({"description": "Employment type: Not full-time"}), "excluded")
        self.assertEqual(employment_status({"employment_type": {"label": "FULL_TIME"}}), "unknown")

    def test_saudi_city_matching_does_not_match_thailand(self):
        job = dict(fixtures.REAL_JOBS[1], location="Bangkok, Thailand")
        self.assertFalse(digest.matches_filters(job))
        self.assertFalse(digest._names_somewhere_saudi("Bangkok, Thailand"))
        self.assertTrue(digest.matches_filters(dict(job, location="Hail, Saudi Arabia")))

    def test_different_descriptions_without_requisitions_are_not_duplicate_vacancies(self):
        official = dict(fixtures.REAL_JOBS[1], source="workable", description="Develop Python services.")
        other = dict(official, source="linkedin_api", description="Develop Swift mobile apps.")
        self.assertEqual(digest.prefer_employer_postings([official, other]), ([official, other], []))

    def test_preferred_and_negated_qualifications_are_not_labeled_mandatory(self):
        job = dict(fixtures.REAL_JOBS[1], description="Preferred qualifications: a master's degree is a nice to have.\nA bachelor's degree is not required.")
        snapshot = job_archive.make_snapshot(job, "2026-11-07T09:00:00Z")
        self.assertEqual(snapshot["mandatory_requirements"], [])

    def test_known_official_employer_site_is_preferred_over_linkedin(self):
        links = [{"url": "https://linkedin.com/jobs/view/1"}, {"url": "https://www.amazon.jobs/en/jobs/123/software-engineer"}]
        self.assertEqual(digest.best_free_link(links, "Amazon"), links[1]["url"])

    def test_generic_successful_page_is_not_positive_vacancy_verification(self):
        response = fixtures.TestLinkEvidence()._response(200, url="https://www.amazon.jobs/en/jobs/123/engineer",
                                                         text='<html><div id="root"></div></html>')
        with mock.patch.object(digest, "_get", return_value=response):
            self.assertEqual(digest.verify_link(response.url), digest.LINK_UNKNOWN)

    def test_adjacent_title_uses_actual_responsibilities(self):
        job = dict(fixtures.REAL_JOBS[1], title="Product Operations Specialist",
                   description="Build Python automations and SQL reporting for the product team.")
        self.assertTrue(digest.matches_filters(job))
        self.assertFalse(digest.matches_filters(dict(job, description="Maintain generic office records and supplies.")))
        self.assertFalse(digest.matches_filters(dict(job, location="Dubai, UAE")))

    def test_runtime_preferences_are_part_of_rank_input_not_resume_evidence(self):
        captured = []
        def model(prompt, *args, **kwargs):
            captured.append(prompt)
            return {"scores": []}
        with mock.patch.object(digest, "chat_json", side_effect=model):
            digest.rank_jobs([dict(fixtures.REAL_JOBS[1])], "key", evidence="RESUME FACTS")
        self.assertIn("full-time", captured[0])
        self.assertIn("Laverne", captured[0])
        self.assertIn("RESUME FACTS", captured[0])
        self.assertIn("canonical preference guide", load_preferences())

    def test_same_requisition_prefers_employer_over_aggregator(self):
        job = dict(fixtures.REAL_JOBS[1], requisition_id="REQ-12")
        official = dict(job, source="workable", id="official", url="https://apply.workable.com/example/j/A/")
        copy_job = dict(job, title="Product Software Builder", source="linkedin_api")
        kept, repeats = digest.prefer_employer_postings([copy_job, official])
        self.assertEqual(kept, [official])
        self.assertEqual(repeats, [copy_job])

    def test_missing_requisition_can_match_content_but_different_requisitions_do_not(self):
        official = dict(fixtures.REAL_JOBS[1], source="workable", requisition_id="REQ-1")
        aggregator = dict(official, source="linkedin_api", requisition_id="", id="another")
        self.assertEqual(digest.drop_repeat_descriptions([official, aggregator], {}), ([official], [aggregator]))
        different = dict(aggregator, requisition_id="REQ-2")
        self.assertEqual(digest.drop_repeat_descriptions([official, different], {}), ([official, different], []))

    def test_expiry_in_canonical_metadata_is_respected(self):
        job = dict(fixtures.REAL_JOBS[1], link_status="open")
        digest.apply_posting_metadata(job, {"title": job["title"], "identity_verified": True,
                                           "valid_through": "2020-01-01T00:00:00Z"})
        self.assertEqual(job["link_status"], "closed")

    def test_named_target_queries_rotate_within_existing_allowance(self):
        plans = [digest.discovery_query_plan(digest.AGGREGATOR_QUERIES, 2, day) for day in range(20)]
        self.assertTrue(all(len(plan) == 2 for plan in plans))
        for name in ("Laverne", "DHL", "Tesla", "Google", "CEER"):
            self.assertTrue(any(name in query for plan in plans for query in plan))
        self.assertEqual(digest.discovery_query_plan(digest.AGGREGATOR_QUERIES, 0, 0), [])

    def test_saved_verification_timestamp_is_the_actual_live_check_time(self):
        job = dict(fixtures.REAL_JOBS[1])
        with mock.patch.object(digest, "verify_link", return_value="open"):
            digest.settle_link(job)
        self.assertEqual(job["verification_url"], job["url"])
        self.assertRegex(job["verified_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_official_apply_option_is_preferred_over_native_aggregators(self):
        options = [{"url": "https://indeed.com/viewjob?jk=1"},
                   {"url": "https://linkedin.com/jobs/view/1"},
                   {"url": "https://apply.workable.com/acme/j/1/"}]
        self.assertEqual(digest.best_free_link(options, "Acme"), "https://apply.workable.com/acme/j/1/")
        self.assertEqual(digest.best_free_link(options[:2], "Acme"), "https://linkedin.com/jobs/view/1")

    def test_structured_canonical_metadata_preserves_type_and_requisition(self):
        data = {"@type": "JobPosting", "title": "Software Engineer", "description": fixtures.FULL_JD,
                "employmentType": "FULL_TIME", "identifier": {"value": "REQ-42"},
                "hiringOrganization": {"name": "Acme"}}
        response = mock.Mock(status_code=200, text='<script type="application/ld+json">' + json.dumps(data) + '</script>')
        job = dict(fixtures.REAL_JOBS[1], title="Software Engineer", company="Acme", employment_type="", description="")
        with mock.patch.object(digest, "_get", return_value=response):
            digest.fetch_posting_page(job)
        self.assertEqual(job["employment_type"], "FULL_TIME")
        self.assertEqual(job["requisition_id"], "REQ-42")

    def test_unrelated_jobposting_cannot_change_existing_employment_or_requisition(self):
        job = dict(fixtures.REAL_JOBS[1], title="Software Engineer", company="Acme", requisition_id="REQ-1",
                   employment_type="CONTRACTOR", description="")
        for metadata in ({"@type": "JobPosting", "title": "Software Engineer", "description": fixtures.FULL_JD,
                          "hiringOrganization": {"name": "Acme"}, "identifier": {"value": "REQ-2"}, "employmentType": "FULL_TIME"},
                         {"@type": "JobPosting", "description": fixtures.FULL_JD, "employmentType": "FULL_TIME"}):
            with self.subTest(metadata=metadata):
                response = mock.Mock(status_code=200, url=job["url"], text='<script type="application/ld+json">' + json.dumps(metadata) + '</script>')
                with mock.patch.object(digest, "_get", return_value=response):
                    self.assertEqual(digest.fetch_posting_page(job), "")
                self.assertEqual(job["requisition_id"], "REQ-1")
                self.assertEqual(job["employment_type"], "CONTRACTOR")

    def test_official_portal_redirect_cannot_verify_a_different_employer(self):
        job = dict(fixtures.REAL_JOBS[1], company="Sanabil Studio", title="Software Engineer", source="target",
                   token="sanabil-studio", employer_verified=False, employment_type="", description="",
                   url="https://careers.sanabil.studio/jobs/123-software-engineer")
        metadata = {"@type": "JobPosting", "title": job["title"], "description": fixtures.FULL_JD,
                    "hiringOrganization": {"name": "Unrelated Employer"}, "employmentType": "FULL_TIME"}
        response = mock.Mock(status_code=200, url="https://unrelated.test/jobs/1",
                             text='<script type="application/ld+json">' + json.dumps(metadata) + '</script>')
        with mock.patch.object(digest, "_get", return_value=response):
            self.assertEqual(digest.fetch_posting_page(job), "")
        self.assertEqual(job["company"], "Sanabil Studio")
        self.assertFalse(job["employer_verified"])

    def test_snapshot_and_card_explain_employment_source_and_live_check(self):
        job = dict(fixtures.REAL_JOBS[1], employment_type="FULL_TIME", link_status="open",
                   verified_at="2026-11-07T09:00:00Z", verification_url="https://example.com/b",
                   discovery_url="https://linkedin.com/jobs/view/12", discovery_source="LinkedIn",
                   tier="Worth reviewing", track="Software engineering, front and back",
                   description=fixtures.FULL_JD, score=68, why="Shipped TypeScript applications")
        snapshot = job_archive.make_snapshot(job, "2026-11-07T09:01:00Z")
        self.assertEqual(snapshot["employment_status"], "full_time")
        self.assertEqual(snapshot["verified_at"], "2026-11-07T09:00:00Z")
        self.assertEqual(snapshot["verification_url"], "https://example.com/b")
        card = job_archive.job_card_payload(snapshot)
        fields = {field["name"]: field["value"] for field in card["embeds"][0]["fields"]}
        self.assertEqual(fields["Employment"], "Full-time")
        self.assertIn("LinkedIn", fields["Discovery source"])
        self.assertIn("2026-11-07", fields["Verification"])
        self.assertEqual(fields["Role track"], "Software engineering, front and back")

    def test_unknown_employment_or_blocked_live_check_is_a_separate_pending_lead(self):
        harness = strict.TestStrictAlerts()
        self.addCleanup(harness.doCleanups)
        for raw, live in (("", "open"), ("FULL_TIME", "unknown")):
            job = harness.job(employment_type=raw)
            with self.subTest(raw=raw, live=live), \
                 mock.patch.object(digest, "verify_link", return_value=live):
                # run_main supplies link statuses; override the actual settler for this case.
                original = digest.settle_link
                def settle(value):
                    result = original(value)
                    value["link_status"] = live
                    return result
                with mock.patch.object(digest, "settle_link", side_effect=settle):
                    body, row, seen, _ = harness.run_jobs([job], {"match": 90}, {job["title"]: strict.VIABLE})
            self.assertEqual(row["strong"], 0)
            self.assertEqual(row["needs_checking_delivered"], 1)
            self.assertIn("unverified leads", body)
            self.assertTrue(all(key.startswith("pending:") for key in seen))

    def test_confirmed_contract_does_not_reach_ranker_or_delivery(self):
        job = dict(fixtures.REAL_JOBS[1], employment_type=["FULL_TIME", "CONTRACTOR"])
        with mock.patch.object(digest, "settle_link", side_effect=AssertionError("contract must be excluded")):
            _, work = fixtures.run_main(fixtures.TestEndToEnd.ENV, {}, jobs=[job])
        import shutil
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
