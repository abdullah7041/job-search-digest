"""Offline official-source contracts; no network or paid search calls."""
import importlib.util
import json
import unittest

import requests


class Response:
    def __init__(self, payload=None, text="", status=200, url=""):
        self.payload, self.text, self.status_code, self.url = payload, text, status, url

    def json(self):
        return self.payload


def page(posting):
    return '<script type="application/ld+json">' + json.dumps(posting) + '</script>'


class TestTargetSources(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("target_sources"), "official target reader is missing")
        import target_sources
        self.module = target_sources
        self.failures, self.calls = [], []

    def company(self, token):
        return next(c for c in self.module.TARGET_COMPANIES if c["token"] == token)

    def fetch(self, token, responses):
        def get(url, **kwargs):
            self.calls.append(url)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return self.module.fetch_target(self.company(token), get=get, log=lambda _: None,
                                        source_failure=lambda name, message: self.failures.append((name, message)))

    def test_registry_preserves_ats_and_explicit_official_provenance(self):
        targets = self.module.TARGET_COMPANIES
        self.assertEqual(len(targets), 9)
        self.assertEqual({c["name"] for c in targets if c["priority"] == 1},
                         {"Amazon", "Laverne", "Sanabil Studio"})
        self.assertEqual(self.company("tamara")["source"], "greenhouse")
        self.assertEqual(self.company("tabby")["source"], "pinpoint")
        for target in targets:
            self.assertTrue(target["official_url"].startswith("https://"))
            self.assertEqual(target["verified_at"], "2026-11-07")

    def test_amazon_saudi_entity_qualifications_and_no_invented_date(self):
        job = {"id": "uuid", "id_icims": "123", "title": "AI Engineer", "country_code": "SAU",
               "company_name": "AWS EMEA SARL (Saudi Arabia Branch)", "location": "SA, Riyadh",
               "job_path": "/en/jobs/123/ai-engineer", "description": "Build AI products",
               "basic_qualifications": "Eight years required", "preferred_qualifications": "Python preferred",
               "job_schedule_type": "full-time", "posted_date": "November  6, 2026"}
        jobs = self.fetch("amazon", [Response({"jobs": [job, dict(job, country_code="ARE", id_icims="456")], "hits": 2})])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], job["company_name"])
        self.assertEqual(jobs[0]["target_company"], "Amazon")
        self.assertEqual(jobs[0]["requisition_id"], "123")
        self.assertEqual(jobs[0]["first_published"], "2026-11-06")
        self.assertIn("Eight years required", jobs[0]["description"])
        self.assertIn("Python preferred", jobs[0]["description"])
        self.assertEqual(jobs[0]["employment_type"], "full-time")
        self.assertFalse(self.failures)
        unknown = dict(job, posted_date="yesterday", job_schedule_type=None)
        jobs = self.fetch("amazon", [Response({"jobs": [unknown], "hits": 1})])
        self.assertEqual(jobs[0]["first_published"], "")
        self.assertEqual(jobs[0]["employment_type"], "")

    def test_amazon_rejects_invalid_shape_and_untrusted_job_urls(self):
        for payload in ([], {"jobs": {}}, {"jobs": [None], "hits": 1},
                        {"jobs": [{"title": "Engineer", "country_code": "SAU", "id": "1",
                                   "job_path": "https://evil.example/jobs/1"}], "hits": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.fetch("amazon", [Response(payload)]), [])
                self.assertTrue(self.failures)
                self.failures.clear()

    def test_amazon_pagination_is_bounded_and_reports_partial_coverage(self):
        jobs = self.fetch("amazon", [Response({"jobs": [], "hits": 999}) for _ in range(3)])
        self.assertEqual(jobs, [])
        self.assertLessEqual(len(self.calls), 3)
        self.assertTrue(self.failures)

    def test_amazon_reads_next_page_using_advertised_count(self):
        raw = {"id_icims": "123", "title": "Engineer", "country_code": "SAU",
               "company_name": "Amazon", "job_path": "/en/jobs/123/engineer"}
        first_page = [dict(raw, id_icims=str(n), job_path=f"/en/jobs/{n}/engineer") for n in range(100)]
        jobs = self.fetch("amazon", [Response({"jobs": first_page, "hits": 101}),
                                     Response({"jobs": [dict(raw, id_icims="124", job_path="/en/jobs/124/engineer")], "hits": 101})])
        self.assertEqual(len(jobs), 101)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("offset=100", self.calls[1])
        self.assertFalse(self.failures)

    def test_shared_numeric_ids_are_namespaced_for_storage_and_ranking(self):
        from job_archive import job_key
        from digest import job_identity
        raw = {"id_icims": "123", "title": "Engineer", "country_code": "SAU",
               "company_name": "Amazon", "job_path": "/en/jobs/123/engineer"}
        amazon = self.fetch("amazon", [Response({"jobs": [raw], "hits": 1})])[0]
        feed = '<rss xmlns:l="https://teamtailor.com/locations"><channel><item><title>Engineer</title>' \
               '<link>https://careers.sanabil.studio/jobs/123-engineer</link>' \
               '<l:locations><l:location><l:country>Saudi Arabia</l:country></l:location></l:locations>' \
               '</item></channel></rss>'
        sanabil = self.fetch("sanabil-studio", [Response(text=feed)])[0]
        self.assertEqual(amazon["id"], "amazon:123")
        self.assertEqual(sanabil["id"], "sanabil-studio:123")
        self.assertEqual(amazon["requisition_id"], sanabil["requisition_id"])
        self.assertNotEqual(job_key(amazon), job_key(sanabil))
        self.assertNotEqual(job_identity(amazon), job_identity(sanabil))

    def test_amazon_short_and_duplicate_pages_report_partial_but_retain_jobs(self):
        raw = {"id_icims": "123", "title": "Engineer", "country_code": "SAU",
               "company_name": "Amazon", "job_path": "/en/jobs/123/engineer"}
        first_page = [dict(raw, id_icims=str(n), job_path=f"/en/jobs/{n}/engineer") for n in range(100)]
        for responses, expected in (([Response({"jobs": [], "hits": 21})], 0),
                                    ([Response({"jobs": [raw], "hits": 21})], 1),
                                    ([Response({"jobs": [raw, raw], "hits": 2})], 1),
                                    ([Response({"jobs": first_page, "hits": 150}),
                                      Response({"jobs": first_page[:50], "hits": 150})], 100)):
            with self.subTest(expected=expected):
                jobs = self.fetch("amazon", responses)
                self.assertEqual(len(jobs), expected)
                self.assertTrue(self.failures)
                self.assertIn("coverage incomplete", self.failures[-1][1])
                self.failures.clear()

    def test_amazon_genuine_zero_is_successful_and_three_page_cap_is_partial(self):
        self.assertEqual(self.fetch("amazon", [Response({"jobs": [], "hits": 0})]), [])
        self.assertFalse(self.failures)
        responses = [Response({"jobs": [{"id_icims": str(n), "title": "Engineer", "country_code": "SAU",
                                         "company_name": "Amazon", "job_path": f"/en/jobs/{n}/engineer"}
                                        for n in range(start, start + 100)], "hits": 301})
                     for start in (0, 100, 200)]
        self.calls.clear()
        self.assertEqual(len(self.fetch("amazon", responses)), 300)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.failures)

    def test_sanabil_rss_is_one_read_and_keeps_entity_and_type_pending(self):
        url = "https://careers.sanabil.studio/jobs/123-ai-engineer"
        feed = '<rss xmlns:l="https://teamtailor.com/locations"><channel><item><title>AI Engineer</title>' \
               '<link>' + url + '</link><pubDate>Fri, 06 Nov 2026 10:00:00 +0300</pubDate>' \
               '<l:locations><l:location><l:city>Riyadh</l:city><l:country>Saudi Arabia</l:country>' \
               '</l:location></l:locations></item></channel></rss>'
        jobs = self.fetch("sanabil-studio", [Response(text=feed), Response(text="unused detail")])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["employment_type"], "")
        self.assertFalse(jobs[0]["employer_verified"])
        self.assertEqual(jobs[0]["company"], "Sanabil Studio")
        self.assertEqual(jobs[0]["requisition_id"], "123")
        self.assertEqual(jobs[0]["first_published"], "2026-11-06T10:00:00+03:00")
        self.assertEqual(jobs[0]["url"], url)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.failures)
        self.assertEqual(self.fetch("sanabil-studio", [Response(text=feed.replace("Saudi Arabia", "UAE"))]), [])

    def test_official_shell_and_blocked_pages_are_not_successful_scans(self):
        for response in (Response(text="<h1>Careers</h1>"), Response(status=403)):
            self.assertEqual(self.fetch("laverne", [response]), [])
            self.assertTrue(self.failures)
            self.failures.clear()
        self.assertEqual(len(self.calls), 2)

    def test_network_and_malformed_feed_fail_closed(self):
        for response in (requests.Timeout("offline"), Response(text="<html>not RSS</html>"),
                         Response(text="<rss>broken")):
            self.assertEqual(self.fetch("sanabil-studio", [response]), [])
            self.assertTrue(self.failures)
            self.failures.clear()

    def test_jsonld_requires_official_url_employer_and_explicit_saudi_country(self):
        posting = {"@type": "JobPosting", "title": "Software Engineer", "url": "https://jobs.ceermotors.com/job/123",
                   "description": "Build products", "hiringOrganization": {"name": "CEER"},
                   "jobLocation": {"address": {"addressCountry": "SA", "addressLocality": "Riyadh"}}}
        jobs = self.fetch("ceer", [Response(text=page(posting))])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["employment_type"], "")
        for field, value in (("url", "https://evil.example/jobs/1"), ("hiringOrganization", {}),
                             ("jobLocation", {"address": {"addressLocality": "Riyadh"}})):
            self.assertEqual(self.fetch("ceer", [Response(text=page(dict(posting, **{field: value})))]), [])

    def test_truncated_and_official_host_redirects_are_failures(self):
        response = Response(text="<h1>Careers</h1>")
        response.truncated = True
        self.assertEqual(self.fetch("google", [response]), [])
        self.assertTrue(self.failures)
        self.failures.clear()
        self.assertEqual(self.fetch("google", [Response(url="https://evil.example/careers")]), [])
        self.assertTrue(self.failures)


if __name__ == "__main__":
    unittest.main()
