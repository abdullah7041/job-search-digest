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

    def test_requested_role_families_are_not_discarded_before_ranking(self):
        titles = (
            "Sales Operations Analyst", "Sales Performance Analyst", "Commercial Analyst", "Commercial Business Analyst",
            "Retail Business Analyst", "Data Analyst", "Business Data Analyst",
            "Business Intelligence Analyst", "Reporting Analyst", "AI Automation Engineer",
            "AI Automation Specialist", "AI Implementation Specialist", "AI Solutions Specialist",
            "AI Product Engineer", "LLM Application Engineer", "Applied AI Engineer",
            "Full-Stack Developer", "Product Engineer", "Customer Insights Analyst", "CRM Analyst",
            "Senior Sales Specialist", "Senior Sales Advisor", "AI Solutions Engineer",
            "Technical Solutions Consultant",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(digest.matches_filters(self.job(title=title)))
        for title in ("Sales Manager", "Commercial Director", "Civil Engineer", "CRM Analyst - Dubai based"):
            with self.subTest(title=title):
                self.assertFalse(digest.matches_filters(self.job(title=title)))

    def test_one_strong_match_is_filled_to_ten_with_review_candidates(self):
        jobs = [self.job(str(n), company=f"Employer {n}") for n in range(12)]
        scores = {str(n): 90 if n == 0 else 80 - n for n in range(12)}
        body, row, seen, work = self.run_jobs(jobs, scores, {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["delivered"], 10)
        self.assertEqual(row["strong"], 1)
        self.assertEqual(row["fallback_delivered"], 9)
        self.assertIn("10 of 10", body)
        self.assertIn("Apply first", body)
        self.assertIn("Worth reviewing", body)
        for name in ("10", "11"):
            self.assertNotIn(f"url:https://example.test/{name}", seen)
            self.assertNotIn(digest.FLOOR_SEEN_PREFIX + f"url:https://example.test/{name}", seen)
        with open(os.path.join(work, "jobs", digest.riyadh_today().isoformat() + ".json"), encoding="utf-8") as handle:
            cards = json.load(handle)["jobs"]
        self.assertEqual(cards[0]["source_id"], "0")
        for card, action in ((cards[0], "Apply first"), (cards[1], "Worth reviewing")):
            embed = fixtures.job_archive.job_card_payload(card)["embeds"][0]
            self.assertIn(action, json.dumps(embed))
            self.assertTrue(card["match_evidence"])

    def test_review_candidates_follow_requested_role_priority(self):
        jobs = [self.job("data", title="Data Analyst", company="Data Employer"),
                self.job("commercial", title="Commercial Analyst", company="Commercial Employer"),
                self.job("operations", title="Sales Operations Analyst", company="Sales Employer")]
        body, row, _, work = self.run_jobs(jobs, {"data": 79, "commercial": 65, "operations": 60},
                                         {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["delivered"], 3)
        with open(os.path.join(work, "jobs", digest.riyadh_today().isoformat() + ".json"), encoding="utf-8") as handle:
            cards = json.load(handle)["jobs"]
        self.assertEqual([card["source_id"] for card in cards], ["operations", "commercial", "data"])

    def test_ten_strong_jobs_fill_the_shortlist_before_review_candidates(self):
        jobs = [self.job(str(n), company=f"Employer {n}") for n in range(12)]
        jobs.append(self.job("review", title="Sales Operations Analyst", company="Sales Employer"))
        scores = {str(n): 95 - n for n in range(12)}
        scores["review"] = 79
        _, row, seen, _ = self.run_jobs(jobs, scores, {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["delivered"], 10)
        self.assertEqual(row["fallback_delivered"], 0)
        for name in ("10", "11", "review"):
            self.assertNotIn(f"url:https://example.test/{name}", seen)
            self.assertNotIn(digest.FLOOR_SEEN_PREFIX + f"url:https://example.test/{name}", seen)

    def test_webhook_keeps_role_priority_when_an_employer_has_multiple_roles(self):
        jobs = [self.job("operations", title="Sales Operations Analyst", company="Shared Employer"),
                self.job("commercial", title="Commercial Analyst", company="Other Employer"),
                self.job("data", title="Data Analyst", company="Shared Employer")]
        body, _, _, _ = self.run_jobs(jobs, {"operations": 60, "commercial": 65, "data": 79},
                                       {j["title"]: VIABLE for j in jobs})
        self.assertLess(body.index("**Sales Operations Analyst**"), body.index("**Commercial Analyst**"))
        self.assertLess(body.index("**Commercial Analyst**"), body.index("**Data Analyst**"))

    def test_mixed_shortlist_counts_only_review_cards_accepted_by_delivery(self):
        jobs = [self.job(name, company=f"Employer {name}") for name in ("strong", "review", "retry")]
        received = []
        def deliver(_url, _secret, snapshots, **kwargs):
            received.extend(snapshots)
            return {"posted": [snapshots[0]["job_key"], snapshots[1]["job_key"], kwargs["heartbeat"]["key"]],
                    "existing": [], "in_progress": [], "retryable": [snapshots[2]["job_key"]], "failed": []}
        env = dict(fixtures.TestEndToEnd.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="test-secret")
        with mock.patch.object(digest, "post_interactive_cards", side_effect=deliver):
            _, row, seen, _ = self.run_jobs(jobs, {"strong": 90, "review": 79, "retry": 70},
                                           {j["title"]: VIABLE for j in jobs}, env)
        self.assertEqual(len(received), 3)
        self.assertEqual(row["delivered"], 2)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertNotIn("url:https://example.test/retry", seen)

    def test_empty_digest_explains_incomplete_descriptions(self):
        body, row, seen, _ = self.run_jobs([self.job(description="Build Python apps.")], {"match": 90})
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(seen, {})
        self.assertIn("complete job descriptions", body)
        self.assertIn("0 of 10", body)

    def test_empty_digest_reports_failed_sources_and_clears_failures_next_run(self):
        response = mock.Mock(status_code=503, text="Service unavailable")
        company = {"name": "Example", "source": "greenhouse", "token": "example"}
        with mock.patch.object(digest, "_get", return_value=response):
            posts, work = fixtures.run_main(fixtures.TestEndToEnd.ENV, {}, jobs=[], companies=[company])
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        self.assertIn("Search coverage incomplete", "\n".join(posts))
        posts, _ = fixtures.run_main(fixtures.TestEndToEnd.ENV, {}, jobs=[], work=work)
        self.assertNotIn("Search coverage incomplete", "\n".join(posts))

    def test_lower_configured_floor_cannot_relax_strong_match_gate(self):
        with mock.patch.object(digest, "MIN_DELIVER_SCORE", 40):
            _, row, _, _ = self.run_jobs([self.job()], {"match": 79})
        self.assertEqual(row["delivered"], 0)

    def test_no_strong_matches_delivers_best_ten_verified_viable_jobs(self):
        scores = (42, 50, 60, 65, 70, 71, 72, 73, 74, 75, 76, 77, 79)
        jobs = [self.job(str(n), company=f"Employer {n}") for n in scores]
        checks = {j["title"]: VIABLE for j in jobs}
        body, row, seen, _ = self.run_jobs(jobs, {str(n): n for n in scores}, checks)
        self.assertEqual(row["delivered"], 10)
        self.assertEqual(row["fallback_delivered"], 10)
        self.assertIn("Closest matches to review", body)
        self.assertNotIn("**Software Engineer 50**", body)
        self.assertIn("**Software Engineer 79**", body)
        self.assertNotIn("floor:url:https://example.test/50", seen)
        self.assertNotIn("url:https://example.test/50", seen)

    def test_review_shortlist_includes_30_day_roles_after_recent_matches(self):
        ages = {"recent": 7, "older_best": 8, "older_next": 14, "older_third": 21,
                "oldest": 30, "deferred": 29, "stale": 31, "undated": None,
                "future": -1}
        jobs = [self.job(name, company=f"Employer {name}",
                         first_published=(digest.riyadh_today() - datetime.timedelta(days=age)).isoformat()
                         if age is not None else "") for name, age in ages.items()]
        scores = dict(recent=45, older_best=95, older_next=90, older_third=79,
                      oldest=70, deferred=60, stale=99, undated=99, future=99)
        body, row, seen, work = self.run_jobs(jobs, scores, {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["fallback_delivered"], 6)
        self.assertEqual(row["strong"], 0)
        self.assertIn("**Software Engineer oldest**", body)
        self.assertIn("**Software Engineer deferred**", body)
        for name in ("stale", "undated", "future"):
            self.assertNotIn(f"url:https://example.test/{name}", seen)
        with open(os.path.join(work, "jobs", digest.riyadh_today().isoformat() + ".json"), encoding="utf-8") as handle:
            cards = json.load(handle)["jobs"]
        self.assertEqual(cards[0]["source_id"], "recent")
        self.assertEqual(cards[1]["score"], 95)
        self.assertTrue(all(card["tier"] == "Closest matches to review" for card in cards))
        self.assertIn("posted 30 days ago", body)

    def test_recent_strong_match_precedes_older_review_candidate(self):
        jobs = [self.job("recent"), self.job("older", first_published=(
            digest.riyadh_today() - datetime.timedelta(days=8)).isoformat())]
        body, row, _, _ = self.run_jobs(jobs, {"recent": 80, "older": 95},
                                       {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["delivered"], 2)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertLess(body.index("**Software Engineer recent**"), body.index("**Software Engineer older**"))

    def test_recent_fallback_wins_employer_slots_before_older_higher_scores(self):
        jobs = [self.job(f"recent{n}") for n in range(3)] + [self.job(
            f"older{n}", first_published=(digest.riyadh_today() - datetime.timedelta(days=8)).isoformat())
            for n in range(3)]
        body, row, _, _ = self.run_jobs(jobs,
            {j["id"]: 60 if j["id"].startswith("recent") else 90 for j in jobs},
            {j["title"]: VIABLE for j in jobs})
        self.assertEqual(row["fallback_delivered"], 3)
        self.assertIn("**Software Engineer recent0**", body)
        self.assertNotIn("**Software Engineer older0**", body)

    def test_review_shortlist_keeps_disclosed_gaps_and_real_scores(self):
        scores = dict(degree=95, skill=85, experience=75, unclear=78, clean=65,
                      lower=50, wrong=39, unchecked=99, incomplete=99)
        jobs = [self.job(name, company=f"Employer {name}") for name in scores]
        jobs[-1]["description"] = "Build TypeScript applications."
        checks = {j["title"]: VIABLE for j in jobs}
        for name, kind, severity, title in (
            ("degree", "education_certification_gap", "medium", "Required degree absent"),
            ("skill", "missing_required_skill", "high", "Vector search not demonstrated"),
            ("experience", "experience_gap", "critical", "Required testing experience absent"),
        ):
            checks[f"Software Engineer {name}"] = dict(VIABLE, ceiling="mid", blockers=[{
                "type": kind, "severity": severity, "title": title}])
        checks["Software Engineer unclear"] = dict(VIABLE, unclear=[{
            "topic": "Frontend team experience", "reason": "Only solo projects evidenced",
            "evidence_needed": "Examples of delivery with a frontend team"}])
        checks["Software Engineer unchecked"] = fit_check.neutral_result("not checked: budget exhausted")
        body, row, seen, work = self.run_jobs(jobs, scores, checks)
        self.assertEqual(row["fallback_delivered"], 6)
        self.assertEqual(row["strong"], 0)
        self.assertEqual(row["unverified_fit"], 2)
        with open(os.path.join(work, "jobs", digest.riyadh_today().isoformat() + ".json"), encoding="utf-8") as handle:
            cards = json.load(handle)["jobs"]
        self.assertEqual([(card["source_id"], card["score"]) for card in cards],
                         [("unclear", 78), ("degree", 69), ("skill", 69), ("clean", 65), ("experience", 55), ("lower", 50)])
        for card in cards:
            self.assertIn(f"url:https://example.test/{card['source_id']}", seen)
            self.assertNotIn(digest.FLOOR_SEEN_PREFIX + f"url:https://example.test/{card['source_id']}", seen)
        for name in ("unchecked", "incomplete"):
            self.assertNotIn(f"url:https://example.test/{name}", seen)
            self.assertNotIn(digest.FLOOR_SEEN_PREFIX + f"url:https://example.test/{name}", seen)
        self.assertIn("Required degree absent", body)
        self.assertIn("Frontend team experience", body)
        self.assertIn("Only solo projects evidenced", body)
        embed = fixtures.job_archive.job_card_payload(cards[0])["embeds"][0]
        self.assertIn("Frontend team experience", json.dumps(embed))
        self.assertIn("Only solo projects evidenced", json.dumps(embed))
        degree_embed = fixtures.job_archive.job_card_payload(cards[1])["embeds"][0]
        self.assertIn("Required degree absent", json.dumps(degree_embed))

    def test_fewer_than_ten_review_jobs_are_delivered_without_padding(self):
        jobs = [self.job("viable"), self.job("wrong"), self.job("blocked"), self.job("unclear")]
        checks = {j["title"]: VIABLE for j in jobs}
        checks[jobs[2]["title"]] = dict(VIABLE, ceiling="mid", blockers=[{
            "type": "education_certification_gap", "severity": "medium", "title": "Required degree absent"}])
        checks[jobs[3]["title"]] = dict(VIABLE, unclear=[{"topic": "Minimum experience", "reason": "Unproven"}])
        body, row, seen, _ = self.run_jobs(jobs,
            {"viable": 55, "wrong": 39, "blocked": 79, "unclear": 78}, checks)
        self.assertEqual(row["delivered"], 3)
        self.assertEqual(row["fallback_delivered"], 3)
        self.assertIn("**Software Engineer viable**", body)
        self.assertIn("**Software Engineer blocked**", body)
        self.assertIn("3 of 10", body)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX + "url:https://example.test/unclear", seen)

    def test_old_gap_cooldowns_do_not_hide_new_review_candidates(self):
        job = self.job()
        work = tempfile.mkdtemp(prefix="review_policy_")
        for filename in ("resume.md", "profile.json"):
            shutil.copy(os.path.join(fixtures.REPO, filename), work)
        today = digest.riyadh_today().isoformat()
        legacy = {"floor:url:https://example.test/match": today,
                  "floor:" + digest.description_key(job): today,
                  "url:https://example.test/already-delivered": today}
        with open(os.path.join(work, "seen.json"), "w", encoding="utf-8") as handle:
            json.dump(legacy, handle)
        _, row, seen, _ = self.run_jobs([job], {"match": 69},
                                      {job["title"]: VIABLE}, work=work)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertIn("url:https://example.test/already-delivered", seen)

    def test_fallback_requires_resume_evidence_not_just_a_clean_check(self):
        _, row, seen, _ = self.run_jobs([self.job()], {"match": 79})
        self.assertEqual(row["delivered"], 0)
        self.assertEqual(seen, {})

    def test_review_shortlist_excludes_confirmed_location_and_role_mismatches(self):
        for kind in ("location_work_authorization", "role_scope_mismatch"):
            with self.subTest(kind=kind):
                job = self.job()
                check = dict(VIABLE, ceiling="low", blockers=[{
                    "type": kind, "severity": "critical", "title": "Required role or location mismatch"}])
                _, row, seen, _ = self.run_jobs([job], {"match": 79}, {job["title"]: check})
                self.assertEqual(row["delivered"], 0)
                self.assertIn(digest.FLOOR_SEEN_PREFIX + "url:https://example.test/match", seen)

    def test_strong_and_review_candidates_can_share_the_shortlist(self):
        jobs = [self.job("strong"), self.job("viable")]
        checks = {j["title"]: VIABLE for j in jobs}
        body, row, seen, _ = self.run_jobs(jobs, {"strong": 80, "viable": 79}, checks)
        self.assertEqual(row["delivered"], 2)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertIn("**Software Engineer viable**", body)
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
        jobs = [self.job("best", company="Employer A"), self.job("next", company="Employer B",
                first_published=(digest.riyadh_today() - datetime.timedelta(days=8)).isoformat())]
        received = []
        summaries = []
        def deliver(_url, _secret, snapshots, **kwargs):
            received.extend(snapshots)
            summaries.append(kwargs.get("heartbeat"))
            return {"posted": [snapshots[0]["job_key"], kwargs["heartbeat"]["key"]], "existing": [],
                    "in_progress": [], "retryable": [snapshots[1]["job_key"]], "failed": []}
        env = dict(fixtures.TestEndToEnd.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="test-secret")
        with mock.patch.object(digest, "post_interactive_cards", side_effect=deliver):
            _, row, seen, _ = self.run_jobs(jobs, {"best": 79, "next": 90},
                                           {j["title"]: VIABLE for j in jobs}, env)
        self.assertEqual(row["fallback_delivered"], 1)
        self.assertIsNotNone(summaries[0])
        self.assertIn("2 of 10", summaries[0]["content"])
        self.assertEqual(received[0]["tier"], "Closest matches to review")
        self.assertEqual(received[1]["first_published"], jobs[1]["first_published"])
        footer = fixtures.job_archive.job_card_payload(received[1])["embeds"][0]["footer"]["text"]
        self.assertIn("Posted 8d ago", footer)
        self.assertEqual(received[0]["match_evidence"][0]["resume_quote"],
                         "Built and deployed the product solo with React 19, TypeScript")
        self.assertNotIn("url:https://example.test/next", seen)
        self.assertNotIn("floor:url:https://example.test/next", seen)

    def test_summary_delivery_cannot_hide_total_job_card_failure(self):
        env = dict(fixtures.TestEndToEnd.ENV, INTERACTIVE_WORKER_URL="https://worker.test",
                   WORKER_CALLBACK_SECRET="test-secret")
        for summary_state in ("posted", "existing", "in_progress", "retryable"):
            with self.subTest(summary_state=summary_state):
                job = self.job()
                work = tempfile.mkdtemp(prefix="review_delivery_")
                self.addCleanup(shutil.rmtree, work, ignore_errors=True)
                for filename in ("resume.md", "profile.json"):
                    shutil.copy(os.path.join(fixtures.REPO, filename), work)
                def deliver(_url, _secret, snapshots, **kwargs):
                    result = {"posted": [], "existing": [], "in_progress": [], "retryable": [],
                              "failed": [{"job_key": snapshots[0]["job_key"], "error": "Discord HTTP 503"}]}
                    result[summary_state] = [kwargs["heartbeat"]["key"]]
                    return result
                with mock.patch.object(digest, "post_interactive_cards", side_effect=deliver), \
                     mock.patch.object(digest, "post_discord") as webhook:
                    with self.assertRaises(SystemExit) as raised:
                        self.run_jobs([job], {"match": 69}, {job["title"]: VIABLE}, env, work)
                self.assertEqual(raised.exception.code, 1)
                webhook.assert_not_called()
                with open(os.path.join(work, "seen.json"), encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle), {})
                with open(os.path.join(work, "stats.json"), encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)[-1]["delivered"], 0)

    def test_junior_software_allowed_but_junior_analytics_and_internships_excluded(self):
        for title in ("Junior Software Engineer", "Associate AI Engineer", "Entry Level Backend Developer"):
            self.assertTrue(digest.matches_filters(self.job(title=title)), title)
        for title in ("Junior Data Analyst", "Software Engineering Intern", "AI Graduate Trainee"):
            self.assertFalse(digest.matches_filters(self.job(title=title)), title)

    def test_qualification_cap_is_applied_before_delivery(self):
        job = self.job()
        blocked = dict(VIABLE, ceiling="mid", blockers=[{
            "type": "education_certification_gap", "severity": "medium", "title": "Required degree absent"}])
        body, row, seen, _ = self.run_jobs([job], {"match": 95}, {job["title"]: blocked})
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["below_floor"], 0)
        self.assertIn("**Software Engineer match**", body)
        self.assertIn("fit 69", body)
        self.assertIn("Required degree absent", body)
        self.assertNotIn(digest.FLOOR_SEEN_PREFIX + "url:https://example.test/match", seen)
        self.assertIn("url:https://example.test/match", seen)

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

    def test_limited_fresh_feeds_target_resume_tracks_before_pagination(self):
        for title in ("'ai engineer'", "'llm'", "software", "'data analyst'", "'power bi'"):
            self.assertIn(title, digest.ACTIVE_ATS_TITLE)
        for title in ("'machine learning'", "'data scien'", "'data engineer'", "mlops",
                      "devops", "sre", "'cloud engineer'", "'data architect'", "'qa engineer'"):
            self.assertNotIn(title, digest.ACTIVE_ATS_TITLE)

    def test_scheduled_feed_requests_fallback_window_without_extra_calls(self):
        with mock.patch.object(digest, "fresh_api_feeds", return_value=("active_ats",)), \
             mock.patch.object(digest, "fetch_active_ats", return_value=[]) as fetch:
            self.run_jobs([], {})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[2], 30)

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
        self.assertIn("Unverified posting dates: 2 (0 of 10).", body)


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
