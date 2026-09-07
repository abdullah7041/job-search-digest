"""Tests for the evidence gate behind the ceiling verdict.

The prompt only asks the model to cite evidence. `verify` is what makes the
citation mean anything, so almost everything here exercises `verify` directly
with hand-written model answers -- no network, and each one is a claim a real
model plausibly makes.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import fit_check  # noqa: E402

RESUME = """# ABDULLAH BIN AHMED
**AI Product Engineer**

## SUMMARY
AI Product Engineer who ships production LLM systems end to end.

## EXPERIENCE
### Founder & AI Product Engineer - Watheq | Riyadh | Aug 2025 - Present
- Built and deployed the full platform solo: React 19, TypeScript, Supabase
  (PostgreSQL) with role-based access control, Netlify serverless CI/CD.
- Built an LLM evaluation pipeline that judge-scores prompt, temperature, and
  model variants, so quality changes ship on measured results.

## SKILLS
- **AI & LLM:** prompt engineering, evaluation pipelines, RAG concepts, n8n
- **Languages:** Python, TypeScript, SQL
"""

JD = """Qualifications
- Bachelor's degree in Computer Science or a related field
- 5+ years of experience in Data Engineering
- Hands-on with GCP, especially BigQuery
- Experience with Airflow and dbt
- Strong SQL skills
"""

JOB = {"title": "Senior Data Engineer I", "company": "HungerStation",
       "location": "Riyadh, Saudi Arabia", "description": JD}


def blocker(title, snippet, source="job_description", severity="high",
            kind="education_certification_gap"):
    return {"type": kind, "severity": severity, "title": title,
            "explanation": "The posting states this and the resume does not show it.",
            "evidence": [{"source": source, "snippet": snippet}]}


def strength(title, snippet, source="resume"):
    return {"title": title, "why_it_matters": "Directly relevant to the role.",
            "evidence": [{"source": source, "snippet": snippet}]}


def answer(blockers=(), unclear=(), strengths=()):
    return {"blockers": list(blockers), "unclear": list(unclear),
            "strengths": list(strengths)}


class TestBlockerVerification(unittest.TestCase):
    def test_a_blocker_citing_real_job_text_survives(self):
        result = fit_check.verify(answer(blockers=[
            blocker("No Bachelor's degree in Computer Science",
                    "Bachelor's degree in Computer Science")]), JOB, RESUME)
        self.assertEqual(len(result["blockers"]), 1)
        self.assertEqual(result["unclear"], [])

    def test_a_blocker_whose_quote_appears_nowhere_is_demoted_not_kept(self):
        """Unverifiable pessimism becomes a question, not a verdict."""
        result = fit_check.verify(answer(blockers=[
            blocker("Requires a security clearance",
                    "must hold an active government security clearance")]), JOB, RESUME)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["unclear"]), 1)
        self.assertIn("No quoted evidence", result["unclear"][0]["reason"])

    def test_a_blocker_that_never_quotes_the_posting_is_demoted(self):
        """A blocker asserts the POSTING demands something, so it has to quote
        the posting. Citing only the resume shows what the candidate lacks
        without showing that anyone asked for it."""
        result = fit_check.verify(answer(blockers=[
            blocker("No Kubernetes or Terraform experience",
                    "Built and deployed the full platform solo: React 19, TypeScript",
                    source="resume")]), JOB, RESUME)
        self.assertEqual(result["blockers"], [])
        self.assertIn("requires this", result["unclear"][0]["reason"])

    def test_a_title_case_blocker_is_not_mistaken_for_invented_terms(self):
        """Regression guard, measured. Plan 018 had unsupported_terms check the
        blocker title; it flags capitalised words mid-sentence as proper nouns,
        which is meaningless for a Title Case heading. Against the live
        HungerStation posting it threw away all three real blockers on the words
        "Dedicated", "Required" and "Tools" while all nine of their snippets
        verified."""
        titles = [
            "Lacks 5-8 Years of Dedicated Data Engineering Experience",
            "Lacks Required Bachelor's Degree in Relevant Field",
            "Missing Proven Experience with Modern Data Stack Tools",
        ]
        raw = answer(blockers=[
            blocker(title, "5+ years of experience in Data Engineering")
            for title in titles])
        result = fit_check.verify(raw, JOB, RESUME)
        self.assertEqual(len(result["blockers"]), 3)
        self.assertEqual(result["unclear"], [])

    def test_a_blocker_naming_a_requirement_absent_from_the_resume_is_kept(self):
        """THE regression guard for the direction of the term check.

        A blocker names what the candidate LACKS, so its terms are absent from
        the resume by definition. Checking a blocker against the resume's
        vocabulary -- as plan 018 originally specified -- would reject every
        true blocker. BigQuery is in the posting and in no part of the resume,
        which is exactly what makes it a real blocker.
        """
        self.assertNotIn("bigquery", RESUME.lower())
        result = fit_check.verify(answer(blockers=[
            blocker("No BigQuery or GCP evidence", "Hands-on with GCP, especially BigQuery")]),
            JOB, RESUME)
        self.assertEqual(len(result["blockers"]), 1)
        self.assertIn("BigQuery", result["blockers"][0]["title"])

    def test_blockers_are_capped(self):
        many = [blocker(f"Missing requirement {n} for Data Engineering",
                        "5+ years of experience in Data Engineering") for n in range(6)]
        self.assertEqual(len(fit_check.verify(answer(blockers=many), JOB, RESUME)["blockers"]),
                         fit_check.MAX_BLOCKERS)


class TestStrengthVerification(unittest.TestCase):
    def test_a_strength_citing_the_resume_survives(self):
        result = fit_check.verify(answer(strengths=[
            strength("Strong SQL and PostgreSQL", "Supabase (PostgreSQL) with role-based access control")]),
            JOB, RESUME)
        self.assertEqual(len(result["strengths"]), 1)

    def test_an_unverifiable_strength_is_deleted_not_demoted(self):
        """The asymmetry. A strength that cannot be shown simply goes away --
        it is the claim that put an 85 on a card, so no softer landing."""
        result = fit_check.verify(answer(strengths=[
            strength("Extensive BigQuery and Airflow experience",
                     "led a team of twelve data engineers")]), JOB, RESUME)
        self.assertEqual(result["strengths"], [])
        self.assertEqual(result["unclear"], [], "a bad strength must not become a question")

    def test_a_strength_naming_a_tool_the_resume_lacks_is_deleted(self):
        """The quote is real, but the claim built on it is not."""
        result = fit_check.verify(answer(strengths=[
            strength("Production Airflow and BigQuery pipelines",
                     "Built an LLM evaluation pipeline that judge-scores prompt")]),
            JOB, RESUME)
        self.assertEqual(result["strengths"], [])

    def test_a_strength_citing_the_job_description_is_not_evidence_of_capability(self):
        """Quoting the posting back proves the job wants it, not that the
        candidate has it -- so the term check against the resume still bites."""
        result = fit_check.verify(answer(strengths=[
            strength("Meets the BigQuery requirement", "Hands-on with GCP, especially BigQuery",
                     source="job_description")]), JOB, RESUME)
        self.assertEqual(result["strengths"], [])


class TestBannedClaims(unittest.TestCase):
    def test_an_outcome_prediction_is_replaced(self):
        result = fit_check.verify(answer(blockers=[
            blocker("You will be rejected without a degree",
                    "Bachelor's degree in Computer Science")]), JOB, RESUME)
        title = (result["blockers"] + result["unclear"])[0].get("title") or \
            (result["blockers"] + result["unclear"])[0].get("topic")
        self.assertNotIn("rejected", title.lower())
        self.assertIn("employer", title.lower())

    def test_a_prediction_anywhere_in_the_text_is_caught(self):
        for phrase in ("guaranteed to fail", "you are disqualified",
                       "will not get an interview", "will be screened out"):
            with self.subTest(phrase=phrase):
                self.assertTrue(fit_check.BANNED_CLAIM_RE.search(phrase))

    def test_the_surrounding_claim_survives_when_phrasing_is_fine(self):
        """Substitution, not rejection: only offending text is replaced."""
        result = fit_check.verify(answer(blockers=[
            blocker("No Bachelor's degree in Computer Science",
                    "Bachelor's degree in Computer Science")]), JOB, RESUME)
        self.assertIn("Bachelor", result["blockers"][0]["title"])


class TestCeilingBand(unittest.TestCase):
    def test_no_blockers_is_a_high_ceiling(self):
        self.assertEqual(fit_check.ceiling_band([]), "high")

    def test_one_ordinary_blocker_is_a_mid_ceiling(self):
        self.assertEqual(fit_check.ceiling_band([{"severity": "high"}]), "mid")

    def test_two_blockers_cap_the_ceiling(self):
        self.assertEqual(
            fit_check.ceiling_band([{"severity": "medium"}, {"severity": "medium"}]), "low")

    def test_one_critical_blocker_caps_the_ceiling_on_its_own(self):
        self.assertEqual(fit_check.ceiling_band([{"severity": "critical"}]), "low")


class TestNeutralResult(unittest.TestCase):
    """A check that could not run must never invent a verdict."""

    def test_a_neutral_result_has_no_blockers_and_no_ceiling(self):
        result = fit_check.neutral_result("boom")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["ceiling"], "unknown")

    def test_a_raising_model_call_yields_a_neutral_result(self):
        with mock.patch.object(fit_check, "chat_json", side_effect=RuntimeError("503")):
            result = fit_check.check_job(JOB, RESUME, "key", 0.0, "m", [])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["ceiling"], "unknown")
        self.assertIn("503", result["note"])

    def test_a_garbage_answer_yields_no_blockers(self):
        for junk in ({}, {"blockers": "nope"}, {"blockers": [None, 7]}):
            with self.subTest(junk=junk):
                result = fit_check.verify(junk, JOB, RESUME)
                self.assertEqual(result["blockers"], [])
                self.assertEqual(result["ceiling"], "high")

    def test_without_a_resume_every_job_is_marked_unchecked(self):
        jobs = [dict(JOB, score=90), dict(JOB, score=85)]
        fit_check.check_jobs(jobs, "", "key", "m", [])
        for job in jobs:
            self.assertEqual(job["fit_check"]["ceiling"], "unknown")
            self.assertIn("no resume", job["fit_check"]["note"])

    def test_without_an_api_key_every_job_is_marked_unchecked(self):
        jobs = [dict(JOB, score=90)]
        fit_check.check_jobs(jobs, RESUME, "", "m", [])
        self.assertEqual(jobs[0]["fit_check"]["ceiling"], "unknown")


class TestCheckJobs(unittest.TestCase):
    def test_only_the_highest_scoring_jobs_are_checked(self):
        jobs = [dict(JOB, score=80 + n, id=n) for n in range(fit_check.MAX_CHECKED_JOBS + 3)]
        calls = []

        def fake(job, *args, **kwargs):
            calls.append(job["score"])
            return fit_check.neutral_result("")

        with mock.patch.object(fit_check, "check_job", side_effect=fake):
            fit_check.check_jobs(jobs, RESUME, "key", "m", [])

        self.assertEqual(len(calls), fit_check.MAX_CHECKED_JOBS)
        self.assertEqual(calls, sorted(calls, reverse=True), "highest score first")
        unchecked = [j for j in jobs if "budget exhausted" in j["fit_check"]["note"]]
        self.assertEqual(len(unchecked), 3)

    def test_every_job_carries_a_result_even_when_none_was_checked(self):
        """An unchecked job must look unchecked, not clean."""
        jobs = [dict(JOB, score=90, id=n) for n in range(3)]
        with mock.patch.object(fit_check.time, "monotonic", return_value=1e9):
            fit_check.check_jobs(jobs, RESUME, "key", "m", [])
        for job in jobs:
            self.assertEqual(job["fit_check"]["ceiling"], "unknown")


class TestPrompt(unittest.TestCase):
    def test_the_prompt_carries_both_documents_and_the_evidence_rule(self):
        prompt = fit_check.build_prompt(JOB, RESUME)
        self.assertIn("<resume>", prompt)
        self.assertIn("<job_description>", prompt)
        self.assertIn("COPIED VERBATIM", prompt)
        self.assertIn("unclear", prompt)

    def test_the_prompt_forbids_predicting_the_employers_decision(self):
        self.assertIn("what the employer will decide",
                      fit_check.build_prompt(JOB, RESUME))

    def test_the_prompt_states_the_listed_versus_demonstrated_rule(self):
        prompt = fit_check.build_prompt(JOB, RESUME).lower()
        self.assertIn("only lists", prompt)
        self.assertIn("unproven", prompt)


if __name__ == "__main__":
    unittest.main()
