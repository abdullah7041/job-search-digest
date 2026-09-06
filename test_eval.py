"""Tests for the gold-set scorer.

The scorer judges the ranker, so it needs judging itself: a grader that always
returns "pass" would be worse than no gold set at all, because it would look
like coverage.

Every path anchors to __file__ rather than the working directory. `test_digest`
chdirs to a temp directory at import time (test_digest.py:30-40) and CI runs
both suites in one process, so a relative path here breaks depending on which
file imported first. Plan 001's executor hit exactly this; see
plans/README.md:594-600.
"""

from __future__ import annotations

import glob
import json
import io
import os
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(REPO, "eval", "fixtures")

import sys

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from eval.run_eval import PASS_THRESHOLD  # noqa: E402
from eval.score import (  # noqa: E402
    INVARIANCE_TOLERANCE, band_score, grade, invariance_failures, term_present)


def read_fixture(path: str) -> dict:
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def fixture_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))


def load_fixtures() -> list[dict]:
    return [read_fixture(p) for p in fixture_paths()]


class TestTermPresent(unittest.TestCase):
    def test_a_term_matches_at_a_word_start(self):
        self.assertTrue(term_present("dbt", "no dbt experience shown"))

    def test_a_term_inside_a_word_does_not_match(self):
        """Same rule as digest.keyword_hits, for the same reason (plans/017)."""
        self.assertFalse(term_present("dbt", "uses adbtool daily"))

    def test_a_multiword_term_tolerates_separators(self):
        self.assertTrue(term_present("modern data stack", "the modern-data-stack tooling"))

    def test_a_suffixed_word_still_matches(self):
        self.assertTrue(term_present("year", "5 years of experience"))

    def test_an_empty_term_never_matches(self):
        self.assertFalse(term_present("", "anything"))


class TestBandScore(unittest.TestCase):
    def test_inside_the_band_is_full_credit(self):
        self.assertEqual(band_score(50, [35, 68]), 1.0)
        self.assertEqual(band_score(35, [35, 68]), 1.0)
        self.assertEqual(band_score(68, [35, 68]), 1.0)

    def test_credit_decays_with_distance_outside_the_band(self):
        self.assertEqual(band_score(78, [35, 68]), 0.5)   # 10 points over, half decay
        self.assertEqual(band_score(25, [35, 68]), 0.5)

    def test_far_outside_the_band_scores_zero(self):
        self.assertEqual(band_score(95, [35, 68]), 0.0)
        self.assertEqual(band_score(100, [0, 39]), 0.0)


class TestGrade(unittest.TestCase):
    FIXTURE = {
        "id": "t",
        "expected": {"score_band": [35, 68],
                     "must_flag": ["BigQuery", "Terraform"],
                     "must_not_credit": ["data modeling"]},
    }

    def test_a_score_in_band_naming_a_gap_scores_full(self):
        result = grade(self.FIXTURE, {"score": 55, "why": "No BigQuery or GCP evidence."})
        self.assertEqual(result["overall"], 1.0)
        self.assertEqual(result["hard_failures"], [])

    def test_an_inflated_score_loses_the_band_category(self):
        result = grade(self.FIXTURE, {"score": 85, "why": "No BigQuery evidence."})
        self.assertEqual(result["categories"]["score_band"], 0.15)
        self.assertLess(result["overall"], 0.5)

    def test_a_reason_naming_no_expected_gap_loses_that_category(self):
        result = grade(self.FIXTURE, {"score": 55, "why": "Senior IC role in Riyadh."})
        self.assertEqual(result["categories"]["reason_gap"], 0.0)

    def test_a_forbidden_term_in_the_reason_is_not_a_failure(self):
        """`why` is 15 words and must_not_credit is about FRAMING, not presence:
        "lacks data modeling" and "strong data modeling" carry the same term.
        Grading it here punished correct answers, so it does not."""
        result = grade(self.FIXTURE,
                       {"score": 55, "why": "Lacks data modeling and BigQuery evidence."})
        self.assertEqual(result["hard_failures"], [])
        self.assertIn("must_not_credit not graded (needs 018B typed strengths)",
                      result["notes"])

    def test_crediting_a_forbidden_strength_is_a_hard_failure(self):
        """The regression that put an 85 on a card: an unproven skill sold as a
        strength. Zero regardless of how well the number scored. Exact once the
        claim arrives as a typed strength rather than as prose."""
        result = grade(self.FIXTURE, {
            "score": 55, "why": "Good match.",
            "strengths": [{"title": "Strong data modeling experience"}]})
        self.assertEqual(result["overall"], 0.0)
        self.assertIn("forbidden_strength_credited:data modeling", result["hard_failures"])

    def test_a_strength_that_is_not_forbidden_passes_the_gate(self):
        result = grade(self.FIXTURE, {
            "score": 55, "why": "No BigQuery.",
            "strengths": [{"title": "Production Python and CI/CD"}]})
        self.assertEqual(result["hard_failures"], [])

    def test_a_missing_score_fails_rather_than_passing_silently(self):
        result = grade(self.FIXTURE, {"why": "no score"})
        self.assertEqual(result["overall"], 0.0)
        self.assertIn("no_score_returned", result["hard_failures"])

    def test_blockers_are_not_graded_until_018b_emits_them(self):
        result = grade(self.FIXTURE, {"score": 55, "why": "No BigQuery."})
        self.assertNotIn("blockers", result["categories"])
        self.assertIn("blockers not graded (018B not shipped)", result["notes"])

    def test_blockers_are_graded_once_present(self):
        result = grade(self.FIXTURE, {
            "score": 55, "why": "No BigQuery.",
            "blockers": [{"title": "No BigQuery or GCP evidence"},
                         {"title": "No Terraform / IaC evidence"}]})
        self.assertEqual(result["categories"]["blockers"], 1.0)

    def test_partial_blocker_coverage_scores_partially(self):
        result = grade(self.FIXTURE, {
            "score": 55, "why": "No BigQuery.",
            "blockers": [{"title": "No BigQuery evidence"}]})
        self.assertEqual(result["categories"]["blockers"], 0.5)


class TestThresholdArithmetic(unittest.TestCase):
    """The pass threshold has to make every live category load-bearing.

    Only two categories are graded until 018B ships blockers, weighted 3 and 1,
    so a fixture can land exactly two ways: 1.0 band + 0.0 reason = 0.75, or
    0.0 band + 1.0 reason = 0.25. Pick 0.7 as the threshold and the first one
    passes -- which makes reason_gap decorative and lets a ranker that is
    inflated but vague sail through inflation-listed-only-skills, the fixture
    built to catch exactly that. This test is here because that bug was live
    and the arithmetic is easy to reintroduce.
    """

    FIXTURE = {"id": "t", "expected": {"score_band": [0, 50],
                                       "must_flag": ["RAG", "n8n"],
                                       "must_not_credit": []}}

    def test_in_band_but_naming_no_gap_does_not_reach_the_threshold(self):
        result = grade(self.FIXTURE, {"score": 45, "why": "Senior IC role in Riyadh."})
        self.assertEqual(result["overall"], 0.75)
        self.assertLess(result["overall"], PASS_THRESHOLD)

    def test_in_band_and_naming_a_gap_passes(self):
        result = grade(self.FIXTURE, {"score": 45, "why": "No RAG evidence in experience."})
        self.assertEqual(result["overall"], 1.0)
        self.assertGreaterEqual(result["overall"], PASS_THRESHOLD)

    def test_a_fixture_expecting_no_gaps_is_judged_on_band_alone(self):
        """The ceiling fixture names no gaps, so reason_gap is not graded and
        must not be counted against it."""
        fixture = {"id": "c", "expected": {"score_band": [75, 100], "must_flag": [],
                                           "must_not_credit": []}}
        result = grade(fixture, {"score": 90, "why": "Near-perfect GenAI fit."})
        self.assertNotIn("reason_gap", result["categories"])
        self.assertGreaterEqual(result["overall"], PASS_THRESHOLD)


class TestInvariance(unittest.TestCase):
    FIXTURES = [
        {"id": "clean", "expected": {"score_band": [35, 68], "invariance_group": "g"}},
        {"id": "noisy", "expected": {"score_band": [35, 68], "invariance_group": "g"}},
    ]

    def test_a_spread_within_tolerance_is_not_flagged(self):
        results = {"clean": {"score": 55}, "noisy": {"score": 55 + INVARIANCE_TOLERANCE}}
        self.assertEqual(invariance_failures(self.FIXTURES, results), [])

    def test_a_spread_beyond_tolerance_is_flagged(self):
        """Boilerplate is not a requirement. If adding a benefits block moves the
        score further than noise explains, the model is scoring page furniture."""
        results = {"clean": {"score": 55}, "noisy": {"score": 20}}
        failures = invariance_failures(self.FIXTURES, results)
        self.assertEqual(len(failures), 1)
        self.assertIn("spread 35", failures[0])

    def test_a_group_with_one_member_is_not_compared(self):
        self.assertEqual(invariance_failures(self.FIXTURES, {"clean": {"score": 55}}), [])


class TestFixtureSet(unittest.TestCase):
    """The gold set is data, and data rots quietly. These assertions are what
    stop a fixture being edited into something that can no longer fail."""

    def test_the_fixtures_load_and_carry_the_required_shape(self):
        fixtures = load_fixtures()
        self.assertGreaterEqual(len(fixtures), 6)
        for fixture in fixtures:
            with self.subTest(fixture=fixture.get("id")):
                for key in ("id", "description", "source", "job", "expected"):
                    self.assertIn(key, fixture)
                for key in ("id", "title", "company", "location", "description"):
                    self.assertIn(key, fixture["job"])
                low, high = fixture["expected"]["score_band"]
                self.assertLessEqual(low, high)
                self.assertGreaterEqual(low, 0)
                self.assertLessEqual(high, 100)
                self.assertTrue(fixture["job"]["description"].strip(),
                                "a fixture with no JD cannot test scoring")

    def test_the_set_covers_a_floor_and_a_ceiling(self):
        """Without a fixture that must stay HIGH, the eval only punishes optimism
        and an over-correction into pessimism would pass clean."""
        bands = [f["expected"]["score_band"] for f in load_fixtures()]
        self.assertTrue(any(high <= 39 for _, high in bands), "no floor fixture")
        self.assertTrue(any(low >= 75 for low, _ in bands), "no ceiling fixture")

    def test_fixture_ids_are_unique_and_match_their_filenames(self):
        for path in fixture_paths():
            fixture = read_fixture(path)
            self.assertEqual(fixture["id"],
                             os.path.splitext(os.path.basename(path))[0])

    def test_every_invariance_group_has_at_least_two_members(self):
        groups: dict[str, int] = {}
        for fixture in load_fixtures():
            group = fixture["expected"].get("invariance_group")
            if group:
                groups[group] = groups.get(group, 0) + 1
        for group, count in groups.items():
            with self.subTest(group=group):
                self.assertGreaterEqual(count, 2, "a lone member is never compared")


if __name__ == "__main__":
    unittest.main()
