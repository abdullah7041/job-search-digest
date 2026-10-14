"""Offline regression coverage for the approved strong-fit delivery policy."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import test_digest as fixtures
import digest
import fit_check


CLEAN = {"blockers": [], "unclear": [], "strengths": [], "ceiling": "high", "note": ""}
VIABLE = dict(CLEAN, strengths=[{
    "title": "Shipped TypeScript applications",
    "why_it_matters": "The role builds applications",
    "evidence": [{"source": "resume", "snippet": "Built and deployed the product solo with React 19, TypeScript"}],
}])


class TestStrictAlerts(unittest.TestCase):
    def job(self, name="match", **updates):
        job = dict(fixtures.REAL_JOBS[1], id=name, company="Example Labs",
                   title="Software Engineer " + name, url="https://example.test/" + name,
                   description=fixtures.FULL_JD)
        job.update(updates)
        return job

    def run_jobs(self, jobs, scores, checks=None, env=None, work=None):
        ranking = None if scores is None else {
            job["id"]: {"score": scores[job["id"]], "why": "Resume-backed application engineering",
                        "track": "Software engineering, front and back"}
            for job in jobs if job["id"] in scores}
        checks = checks if checks is not None else {j["title"]: CLEAN for j in jobs}
        blocked = digest.requests.Response()
        blocked.status_code = 404
        blocked._content = b""
        blocked._content_consumed = True
        blocked.url = "https://example.test"
        with mock.patch.object(digest.requests, "get", return_value=blocked):
            posts, work = fixtures.run_main(env or fixtures.TestEndToEnd.ENV, ranking,
                                           jobs=jobs, fit_checks=checks, work=work)
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
            row = json.load(handle)[-1]
        seen_path = os.path.join(work, "seen.json")
        seen = {}
        if os.path.exists(seen_path):
            with open(seen_path, encoding="utf-8") as handle:
                seen = json.load(handle)
        return "\n".join(posts), row, seen, work

    def test_only_final_scores_80_and_81_are_delivered(self):
        jobs = [self.job(str(n)) for n in (79, 80, 81)]
        body, row, seen, _ = self.run_jobs(jobs, {str(n): n for n in (79, 80, 81)})
        self.assertEqual(row["delivered"], 2)
        self.assertNotIn("**Software Engineer 79**", body)
        self.assertIn("**Software Engineer 80**", body)
        self.assertIn("**Software Engineer 81**", body)
        self.assertNotIn("url:https://example.test/79", seen)
        self.assertNotIn("floor:url:https://example.test/79", seen)

    def test_keyword_fallback_is_not_delivered_or_cooled(self):
        body, row, seen, _ = self.run_jobs([self.job()], None)
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["unverified_fit"], 1)
        self.assertNotIn("**Software Engineer match**", body)
        self.assertEqual(seen, {})

    def test_malformed_ranking_is_unverified(self):
        for score, why in ((True, "fit"), (80.5, "fit"), ("95", "fit"), (95, None)):
            with self.subTest(score=score, why=why):
                job = self.job()
                digest.apply_ranking([job], {job["id"]: {"score": score, "why": why}})
                self.assertFalse(job["ranking_verified"])

    def test_lower_configured_floor_cannot_relax_strong_match_gate(self):
        with mock.patch.object(digest, "MIN_DELIVER_SCORE", 40):
            _, row, _, _ = self.run_jobs([self.job()], {"match": 79})
        self.assertEqual(row["delivered"], 0)

    def test_no_strong_matches_delivers_best_five_verified_viable_jobs(self):
        jobs = [self.job(str(n), company=f"Employer {n}") for n in (42, 50, 60, 65, 70, 75, 79)]
        checks = {j["title"]: VIABLE for j in jobs}
        body, row, seen, _ = self.run_jobs(jobs, {str(n): n for n in (42, 50, 60, 65, 70, 75, 79)}, checks)
        self.assertEqual(row["delivered"], 5)
        self.assertEqual(row["fallback_delivered"], 5)
        self.assertIn("Closest viable matches", body)
        self.assertNotIn("**Software Engineer 50**", body)
        self.assertIn("**Software Engineer 79**", body)
        self.assertNotIn("floor:url:https://example.test/50", seen)
        self.assertNotIn("url:https://example.test/50", seen)

    def test_fewer_than_five_viable_jobs_are_delivered_without_padding(self):
        jobs = [self.job("viable"), self.job("wrong"), self.job("blocked"), self.job("unclear")]
        checks = {j["title"]: VIABLE for j in jobs}
        checks[jobs[2]["title"]] = dict(CLEAN, ceiling="mid", blockers=[{
            "type": "education_certification_gap", "severity": "medium", "title": "Required degree absent"}])
        checks[jobs[3]["title"]] = dict(VIABLE, unclear=[{"topic": "Minimum experience", "reason": "Unproven"}])
        body, row, seen, _ = self.run_jobs(jobs,
            {"viable": 55, "wrong": 39, "blocked": 79, "unclear": 78}, checks)
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertIn("**Software Engineer viable**", body)
        self.assertNotIn("**Software Engineer blocked**", body)
        self.assertNotIn("floor:url:https://example.test/unclear", seen)

    def test_fallback_requires_resume_evidence_not_just_a_clean_check(self):
        _, row, seen, _ = self.run_jobs([self.job()], {"match": 79})
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(seen, {})

    def test_any_strong_match_suppresses_fallback_without_cooling_viable_jobs(self):
        jobs = [self.job("strong"), self.job("viable")]
        checks = {j["title"]: VIABLE for j in jobs}
        body, row, seen, _ = self.run_jobs(jobs, {"strong": 80, "viable": 79}, checks)
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["fallback_delivered"], 0)
        self.assertNotIn("**Software Engineer viable**", body)
        self.assertNotIn("floor:url:https://example.test/viable", seen)

    def test_fallback_employer_limit_does_not_consume_other_employer_slots(self):
        jobs = [self.job(str(n), company="One Employer" if n > 74 else f"Employer {n}")
                for n in (79, 78, 77, 76, 75, 74, 73)]
        body, row, _, _ = self.run_jobs(jobs, {j["id"]: int(j["id"]) for j in jobs},
                                      {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["delivered"], 5)
        self.assertNotIn("**Software Engineer 76**", body)
        self.assertIn("**Software Engineer 73**", body)

    def test_interactive_fallback_preserves_evidence_and_marks_only_accepted_jobs(self):
        jobs = [self.job("best", company="Employer A"), self.job("next", company="Employer B")]
        received = []
        def deliver(_url, _secret, snapshots, **kwargs):
            received.extend(snapshots)
            return {"posted": [snapshots[0]["job_key"]], "existing": [],
                    "in_progress": [], "retryable": [snapshots[1]["job_key"]], "failed": []}
        env = dict(fixtures.TestEndToEnd.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="test-secret")
        with mock.patch.object(digest, "post_interactive_cards", side_effect=deliver):
            _, row, seen, _ = self.run_jobs(jobs, {"best": 79, "next": 70},
                                           {j["title"]: VIABLE for j in jobs}, env)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertEqual(received[0]["tier"], "Closest viable matches")
        self.assertEqual(received[0]["match_evidence"][0]["resume_quote"],
                         "Built and deployed the product solo with React 19, TypeScript")
        self.assertNotIn("url:https://example.test/next", seen)
        self.assertNotIn("floor:url:https://example.test/next", seen)

    def test_junior_software_allowed_but_junior_analytics_and_internships_excluded(self):
        for title in ("Junior Software Engineer", "Associate AI Engineer", "Entry Level Backend Developer"):
            self.assertTrue(digest.matches_filters(self.job(title=title)), title)
        for title in ("Junior Data Analyst", "Software Engineering Intern", "AI Graduate Trainee"):
            self.assertFalse(digest.matches_filters(self.job(title=title)), title)

    def test_qualification_cap_is_applied_before_delivery(self):
        job = self.job()
        blocked = dict(CLEAN, ceiling="mid", blockers=[{
            "type": "education_certification_gap", "severity": "medium", "title": "Required degree absent"}])
        body, row, seen, _ = self.run_jobs([job], {"match": 95}, {job["title"]: blocked})
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["below_floor"], 1)
        self.assertNotIn("**Software Engineer match**", body)
        self.assertIn("floor:url:https://example.test/match", seen)
        self.assertNotIn("url:https://example.test/match", seen)

    def test_incomplete_requirements_check_stays_retryable(self):
        for reason in ("check unavailable", "not checked: budget exhausted"):
            with self.subTest(reason=reason):
                job = self.job()
                body, row, seen, _ = self.run_jobs(
                    [job], {"match": 95}, {job["title"]: fit_check.neutral_result(reason)})
                self.assertEqual(row["delivered"], 0)
                self.assertEqual(row["unverified_fit"], 1)
                self.assertEqual(seen, {})
                self.assertNotIn("**Software Engineer match**", body)

    def test_retry_after_incomplete_check_can_deliver_once(self):
        job = self.job()
        _, row, seen, work = self.run_jobs([job], {"match": 95},
            {job["title"]: fit_check.neutral_result("check unavailable")})
        self.assertEqual(seen, {})
        _, row, seen, _ = self.run_jobs([job], {"match": 95}, work=work)
        self.assertEqual(row["delivered"], 1)
        self.assertIn("url:https://example.test/match", seen)
        _, row, _, _ = self.run_jobs([job], {"match": 95}, work=work)
        self.assertEqual(row["delivered"], 0)

    def test_target_analytics_titles_pass_and_management_is_excluded(self):
        for title in ("Data Analyst", "Senior Data Analyst", "Business Intelligence Analyst", "Power BI Developer"):
            with self.subTest(title=title):
                self.assertTrue(digest.matches_filters(self.job(title=title)))
        for title in ("Data Analytics Manager", "Product Manager", "Software Development Manager"):
            with self.subTest(title=title):
                self.assertFalse(digest.matches_filters(self.job(title=title)))

    def test_missing_partial_and_teaser_descriptions_stay_retryable(self):
        for description in ("", "Build Python apps.", "We are a growing AI company. " * 20):
            with self.subTest(description=description[:20]):
                _, row, seen, _ = self.run_jobs([self.job(description=description)], {"match": 95})
                self.assertEqual(row["delivered"], 0)
                self.assertEqual(row["unverified_fit"], 1)
                self.assertEqual(seen, {})

    def test_rejected_jobs_do_not_consume_employer_slots(self):
        jobs = [self.job(str(n)) for n in range(4)]
        blocked = dict(CLEAN, ceiling="low", blockers=[{"severity": "critical", "title": "Required skill absent"}])
        checks = {j["title"]: blocked if n < 3 else CLEAN for n, j in enumerate(jobs)}
        body, row, _, _ = self.run_jobs(jobs, {"0": 95, "1": 94, "2": 93, "3": 80}, checks)
        self.assertEqual(row["delivered"], 1)
        self.assertIn("**Software Engineer 3**", body)
        self.assertNotIn("**Software Engineer 0**", body)

    def test_interactive_cards_receive_only_eligible_jobs(self):
        jobs = [self.job(str(n)) for n in (79, 80, 95)]
        checks = {j["title"]: CLEAN for j in jobs}
        checks[jobs[2]["title"]] = fit_check.neutral_result("budget exhausted")
        received = []
        def deliver(_url, _secret, snapshots, **kwargs):
            received.extend(snapshots)
            return {"posted": [s["job_key"] for s in snapshots], "existing": [],
                    "in_progress": [], "retryable": [], "failed": []}
        env = dict(fixtures.TestEndToEnd.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="test-secret")
        with mock.patch.object(digest, "post_interactive_cards", side_effect=deliver):
            _, row, seen, _ = self.run_jobs(jobs, {"79": 79, "80": 80, "95": 95}, checks, env)
        self.assertEqual([s["title"] for s in received], ["Software Engineer 80"])
        self.assertEqual(row["delivered"], 1)
        self.assertNotIn("url:https://example.test/95", seen)

    def test_missing_resume_or_invalid_profile_suppresses_cards(self):
        for filename, contents in (("resume.md", ""), ("profile.json", "null"),
                                   ("profile.json", '{"blurb": true, "skill_keywords": "ai"}')):
            with self.subTest(filename=filename, contents=contents):
                work = tempfile.mkdtemp(prefix="strict_evidence_")
                for source in ("resume.md", "profile.json"):
                    shutil.copy(os.path.join(fixtures.REPO, source), work)
                with open(os.path.join(work, filename), "w", encoding="utf-8") as handle:
                    handle.write(contents)
                body, row, seen, _ = self.run_jobs([self.job()], {"match": 95}, work=work)
                self.assertEqual(row["delivered"], 0)
                self.assertEqual(row["unverified_fit"], 1)
                self.assertEqual(seen, {})
                self.assertIn("evidence", body.lower())

    def test_unverified_dates_are_counted_without_delivery(self):
        jobs = [self.job(str(n), first_published=date) for n, date in enumerate(("", "bad-date"))]
        body, row, seen, _ = self.run_jobs(jobs, {"0": 95, "1": 95})
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(row["unverified_date"], 2)
        self.assertEqual(seen, {})
        self.assertIn("2 with unverified posting dates", body)


class TestStrictPostingDates(unittest.TestCase):
    def test_configuration_cannot_expand_seven_day_window(self):
        self.assertEqual(digest._max_job_age("30"), 7)

    def test_epoch_publication_uses_riyadh_calendar(self):
        stamp = datetime.datetime(2026, 10, 2, 22, 30, tzinfo=datetime.timezone.utc).timestamp() * 1000
        self.assertEqual(digest._epoch_ms_date(stamp), "2026-10-03")

    def test_timestamps_use_riyadh_and_malformed_dates_are_rejected(self):
        jobs = [{"first_published": value} for value in (
            "2026-10-02T22:30:00Z", "2026-10-10garbage", "2026-10-10Tbad")]
        kept, rejected = digest.split_by_age(jobs, 7, datetime.date(2026, 10, 10))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["age_days"], 7)
        self.assertEqual(len(rejected), 2)

    def test_publication_boundaries_across_sources(self):
        today = datetime.date(2026, 10, 10)
        for source in ("greenhouse", "ashby", "workable", "lever", "pinpoint", "jsearch", "indeed", "active_ats", "linkedin_api"):
            jobs = [{"id": str(n), "source": source, "first_published": date}
                    for n, date in enumerate(("2026-10-10", "2026-10-03", "2026-10-02", "", "invalid", "2026-10-11"))]
            with self.subTest(source=source):
                kept, rejected = digest.split_by_age(jobs, 7, today)
                self.assertEqual([j["id"] for j in kept], ["0", "1"])
                self.assertEqual([j["id"] for j in rejected], ["2", "3", "4", "5"])

    def test_default_calendar_day_is_riyadh_after_utc_midnight_boundary(self):
        utc = datetime.datetime(2026, 10, 9, 22, tzinfo=datetime.timezone.utc)
        with mock.patch.object(digest.datetime, "datetime", wraps=datetime.datetime) as clock:
            clock.now.side_effect = lambda tz=None: utc.astimezone(tz) if tz else utc.replace(tzinfo=None)
            kept, _ = digest.split_by_age([{"first_published": "2026-10-02"}], 7)
        self.assertEqual(kept, [], "eight days old in Riyadh even though UTC still reads Oct 9")


class TestStrictCheckResponses(unittest.TestCase):
    def test_malformed_response_is_unknown_rather_than_clean(self):
        for raw in ({}, {"blockers": "none", "unclear": [], "strengths": []},
                    {"blockers": [None], "unclear": [], "strengths": []},
                    {"blockers": [], "unclear": [], "strengths": [{"title": "Python"}]}):
            with self.subTest(raw=raw):
                result = fit_check.verify(raw, {"description": fixtures.FULL_JD}, "Python")
                self.assertEqual(result["ceiling"], "unknown")
                self.assertTrue(result["note"])


class TestRefreshedResumeExport(unittest.TestCase):
    def test_strict_policy_suite_is_run_before_both_release_paths(self):
        from pathlib import Path
        for workflow in ("ci.yml", "digest.yml"):
            with self.subTest(workflow=workflow):
                source = Path(fixtures.REPO, ".github", "workflows", workflow).read_text(encoding="utf-8")
                self.assertIn("test_strict_alerts.py", source)

    def test_profile_and_master_share_truthful_new_evidence(self):
        from pathlib import Path
        root = Path(fixtures.REPO)
        profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
        master = (root / "resume.md").read_text(encoding="utf-8")
        for fact in ("Moyasar", "credit-based", "Hijri", "March 2020", "August 2025", "RAG concepts", "n8n"):
            self.assertIn(fact, profile["blurb"])
            self.assertIn(fact, master)
        self.assertIn("NO bachelor's degree", profile["blurb"])
        self.assertIn("does not build models", profile["blurb"])
        self.assertIn("2016 – 2018", master)
        self.assertIn("deployed in production", master)
        self.assertNotIn("private beta", master + profile["blurb"])
        for track in profile["tracks"]:
            self.assertTrue(any(not title.startswith("Senior") for title in track["titles"]))

    def test_new_evidence_and_education_survive_the_one_page_export(self):
        from io import BytesIO
        from pathlib import Path
        from pypdf import PdfReader
        from resume_render import parse_resume, render_pdf
        master = Path(fixtures.REPO, "resume.md").read_text(encoding="utf-8")
        document = PdfReader(BytesIO(render_pdf(parse_resume(master))))
        self.assertEqual(len(document.pages), 1)
        text = " ".join(document.pages[0].extract_text().split())
        for fact in ("Moyasar", "Hijri", "2016 – 2018", "Dammam, Saudi Arabia"):
            self.assertIn(fact, text)
        evidence = digest.load_resume_evidence(str(Path(fixtures.REPO, "resume.md")))
        self.assertIn("EDUCATION", evidence)
        self.assertIn("Moyasar", evidence)


if __name__ == "__main__":
    unittest.main()
