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
    def test_mixed_degree_clauses_follow_the_specific_qualification(self):
        for description, expected in (
            ("Minimum qualifications: Bachelor's degree in Computer Science, master's degree preferred.", 1),
            ("Minimum qualifications: Bachelor's degree in Computer Science,master's degree preferred.", 1),
            ("Bachelor's degree in Computer Science or equivalent practical experience is required.", 0),
            ("Bachelor's degree in Computer Science preferred, must have practical Python experience.", 0),
            ("Bachelor's degree in Computer Science is not required.", 0),
        ):
            with self.subTest(description=description):
                result = fit_check.verify(answer(blockers=[blocker("Degree absent",
                    "Bachelor's degree in Computer Science")]), dict(JOB, description=description), RESUME)
                self.assertEqual(len(result["blockers"]), expected)

    def test_preferred_degree_is_not_a_mandatory_blocker(self):
        job = dict(JOB, description="Bachelor's degree in Computer Science preferred.")
        result = fit_check.verify(answer(blockers=[blocker("Degree absent",
            "Bachelor's degree in Computer Science")]), job, RESUME)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["unclear"], [])

    def test_degree_accepting_equivalent_experience_is_not_a_degree_blocker(self):
        job = dict(JOB, description="Bachelor's degree in Computer Science or equivalent practical experience.")
        result = fit_check.verify(answer(blockers=[blocker("Degree absent",
            "Bachelor's degree in Computer Science")]), job, RESUME)
        self.assertEqual(result["blockers"], [])

    def test_a_required_degree_is_not_optional_because_another_degree_is_preferred(self):
        job = dict(JOB, description="Bachelor's degree in Computer Science required, a master's degree is preferred.")
        result = fit_check.verify(answer(blockers=[blocker("Required degree absent",
            "Bachelor's degree in Computer Science")]), job, RESUME)
        self.assertEqual(len(result["blockers"]), 1)

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
    def test_background_quote_cannot_append_an_unstated_tool(self):
        result = fit_check.verify(answer(strengths=[strength("LLM evaluation pipeline",
            "Built an LLM evaluation pipeline that judge-scores prompt, temperature, and model variants Kubernetes")]),
            JOB, RESUME)
        self.assertEqual(result["strengths"], [])

    def test_unrelated_project_quote_cannot_promote_a_listed_RAG_skill(self):
        result = fit_check.verify(answer(strengths=[strength("Production RAG systems",
            "Built an LLM evaluation pipeline that judge-scores prompt")]), JOB, RESUME)
        self.assertEqual(result["strengths"], [])
    def test_listed_knowledge_is_not_demonstrated_implementation_evidence(self):
        result = fit_check.verify(answer(strengths=[
            strength("Demonstrated RAG and n8n", "AI & LLM: prompt engineering, evaluation pipelines, RAG concepts, n8n")]),
            JOB, RESUME)
        self.assertEqual(result["strengths"], [])

    def test_job_text_does_not_prove_a_tool_even_when_it_is_listed_in_the_resume(self):
        job = dict(JOB, description="Requirements: production RAG concepts and n8n workflows.")
        result = fit_check.verify(answer(strengths=[strength("Production RAG and n8n",
            "production RAG concepts and n8n workflows", source="job_description")]), job, RESUME)
        self.assertEqual(result["strengths"], [])

    def test_a_strength_citing_the_resume_survives(self):
        result = fit_check.verify(answer(strengths=[
            strength("PostgreSQL integration", "Supabase (PostgreSQL) with role-based access control")]),
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
                self.assertEqual(result["ceiling"], "unknown")

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


class TestWhatTheCheckIsAllowedToRead(unittest.TestCase):
    """The defect that let this pass return "high" on a posting whose stated
    minimum qualification the candidate does not hold.

    The Ma'aden posting archived 2026-09-08 is 5187 characters with its Minimum
    Qualifications at 3958, so `description[:4000]` cut mid-word at "Bachelor's
    degre". The check saw the heading, could not quote a verifiable snippet from
    half a word, and reported no blockers at all.
    """

    JD = ("Your Impact\n" + "We build things and ship them often.\n" * 200 +
          "\nMinimum Qualifications\n"
          " * Bachelor's degree in Computer Science or a related field.\n"
          " * 6 years of experience building production systems.\n")

    def test_the_window_alone_still_cuts_the_qualifications(self):
        self.assertNotIn("Bachelor's degree",
                         fit_check.jd_signal(self.JD, fit_check.JD_CHECK_CHARS))

    def test_the_stated_requirements_reach_the_prompt_anyway(self):
        text = fit_check.check_text({"description": self.JD})

        self.assertIn("Stated requirements:", text)
        self.assertIn("Bachelor's degree in Computer Science", text)
        self.assertIn("6 years of experience", text)

    def test_a_posting_stating_nothing_gets_no_requirements_heading(self):
        """An empty heading would invite the model to fill it in."""
        text = fit_check.check_text({"description": "We are a friendly team."})

        self.assertNotIn("Stated requirements", text)

    def test_the_prompt_names_a_missing_degree_as_its_own_kind_of_gap(self):
        prompt = fit_check.build_prompt({"description": self.JD}, "resume")

        self.assertIn("education_certification_gap", prompt)
        self.assertIn("Stated requirements", prompt)

    def test_a_quote_from_the_requirements_verifies_against_the_full_posting(self):
        """`verify` checks a cited snippet against job["description"], not
        against the excerpt. That is what lets a blocker quote a line the
        window never reached and still survive."""
        raw = {"blockers": [{
            "type": "education_certification_gap", "severity": "high",
            "title": "No bachelor's degree", "explanation": "The posting states one.",
            "evidence": [{"source": "job_description",
                          "snippet": "Bachelor's degree in Computer Science or a related field"}],
        }], "unclear": [], "strengths": []}

        result = fit_check.verify(raw, {"description": self.JD, "title": "T",
                                        "company": "C"}, "A resume with no degree.")

        self.assertEqual(len(result["blockers"]), 1, result["unclear"])


class TestTheGapCeiling(unittest.TestCase):
    """Ceilings, not penalties. Stacked subtraction took a measured example
    from 91 to 46, well past the 67 Watheq gave the same posting."""

    def test_no_gaps_leave_the_score_alone(self):
        self.assertIsNone(fit_check.blocked_score_ceiling([]))

    def test_a_stated_degree_gap_caps_whatever_severity_it_carries(self):
        for severity in fit_check.SEVERITIES:
            with self.subTest(severity=severity):
                ceiling = fit_check.blocked_score_ceiling(
                    [{"type": "education_certification_gap", "severity": severity}])
                self.assertLessEqual(ceiling, fit_check.CREDENTIAL_CEILING)

    def test_severity_sets_the_ceiling(self):
        for severity, expected in fit_check.SEVERITY_CEILING.items():
            with self.subTest(severity=severity):
                self.assertEqual(fit_check.blocked_score_ceiling(
                    [{"type": "domain_gap", "severity": severity}]), expected)

    def test_the_lowest_ceiling_wins(self):
        blockers = [{"type": "domain_gap", "severity": "medium"},
                    {"type": "domain_gap", "severity": "critical"},
                    {"type": "domain_gap", "severity": "high"}]

        self.assertEqual(fit_check.blocked_score_ceiling(blockers),
                         fit_check.SEVERITY_CEILING["critical"])

    def test_a_blocker_with_no_severity_still_caps(self):
        """`verify` defaults an unrecognised severity to medium. A blocker that
        arrived without one still describes a real gap, and letting it escape
        the ceiling would make a malformed blocker read as no blocker."""
        self.assertEqual(fit_check.blocked_score_ceiling([{"title": "b"}]),
                         fit_check.SEVERITY_CEILING["medium"])

    def test_every_ceiling_holds_a_job_out_of_the_strong_tier(self):
        """The property the whole table exists for. A "Strong match" label over
        a verified requirement gap is the card lying."""
        import digest
        for ceiling in fit_check.SEVERITY_CEILING.values():
            self.assertLess(ceiling, digest.STRONG_MATCH_MIN_SCORE)
        self.assertLess(fit_check.CREDENTIAL_CEILING, digest.STRONG_MATCH_MIN_SCORE)

    def test_a_capped_job_is_below_the_delivery_floor(self):
        """Verified qualification gaps prevent strong-match delivery."""
        import digest
        for ceiling in list(fit_check.SEVERITY_CEILING.values()) + [
                fit_check.CREDENTIAL_CEILING]:
            self.assertLess(ceiling, digest.MIN_DELIVER_SCORE)
