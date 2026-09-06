"""Deterministic scorer for the ranker gold set.

Given a fixture's expected constraints and what the ranker actually returned,
produce a 0-1 score per category plus an overall, and a list of hard failures.
Pure functions, no network, no model: `test_eval.py` exercises this offline, so
the thing that judges the ranker is itself under test.

Why this exists at all: plan 018A changed what the ranker reads and therefore
what every delivered score means. Nothing else in this repository would notice
if a later prompt edit walked that back. `check_drift` measures the daily
average against its own recent history, so a slow inflation drift moves the
baseline with it and never fires -- it catches a cliff, not a slope.

WHAT THIS CAN AND CANNOT GRADE TODAY
------------------------------------
Watheq grades `strongMatches` / `missingKeywords` arrays (eval/match-score.mjs).
This ranker returns `{score, why}` and nothing else, and `why` is capped at 15
words, so those arrays do not exist to grade. The categories degrade instead of
pretending:

  score_band  (w3)  ALWAYS gradeable. The anti-inflation core.
  reason_gap  (w1)  Weak but real: does `why` name at least one expected gap?
                    15 words cannot name six, so one is the bar.
  blockers    (w2)  SKIPPED until plan 018B emits typed blockers. When a result
                    carries them, they are graded and the weight switches on.

`must_not_credit` is a HARD failure, not a weighted category -- the same choice
Watheq makes for keyword-stuffing fixtures. A run that presents an unproven
skill as a strength has failed regardless of how well it scored elsewhere.
"""

from __future__ import annotations

import re

# score_band carries three times the weight of the reason text because it is the
# number that reaches the card, gets sorted on, and decides the tier.
WEIGHTS = {"score_band": 3, "reason_gap": 1, "blockers": 2}

# How far outside its band a score may sit before the category scores zero.
# 20 points is two full rubric bands: beyond that the model is not near-missing,
# it is reading the job differently than the fixture says it should.
BAND_DECAY_POINTS = 20

# Same idea as Watheq's BAND_CROSS_TOLERANCE. Two fixtures in one invariance
# group state identical requirements, so their scores should agree -- but the
# ranker sends seed=7 and no temperature, which is reproducible, not
# deterministic. Only flag a spread wide enough that noise cannot explain it.
INVARIANCE_TOLERANCE = 8


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def term_present(term: str, text: str) -> bool:
    """Is `term` in `text`, matched at word starts and tolerant of separators?

    "modern data stack" must match "the modern-data-stack tooling", and "dbt"
    must not match "adbtool". Mirrors digest.keyword_hits: anchor the start,
    leave the suffix free, because English builds meaning by suffix and noise by
    prefix (see plans/017).
    """
    haystack = _normalise(text)
    needle = _normalise(term)
    if not needle:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(re.escape(w) for w in needle.split())
    return re.search(pattern, haystack) is not None


def band_score(score: int, band: list[int]) -> float:
    """1.0 inside the band, decaying linearly to 0 across BAND_DECAY_POINTS."""
    low, high = band
    if low <= score <= high:
        return 1.0
    distance = low - score if score < low else score - high
    return max(0.0, 1.0 - distance / BAND_DECAY_POINTS)


def grade(fixture: dict, result: dict) -> dict:
    """Grade one ranker result against one fixture.

    `result` is `{"score": int, "why": str}` as `rank_jobs` returns it, plus an
    optional `"blockers"` list once 018B exists.
    """
    expected = fixture["expected"]
    why = result.get("why") or ""
    score = result.get("score")
    categories: dict[str, float] = {}
    hard_failures: list[str] = []
    notes: list[str] = []

    if not isinstance(score, int):
        return {"id": fixture["id"], "overall": 0.0, "categories": {},
                "hard_failures": ["no_score_returned"], "notes": [], "score": None}

    categories["score_band"] = band_score(score, expected["score_band"])

    must_flag = expected.get("must_flag") or []
    if must_flag:
        found = [t for t in must_flag if term_present(t, why)]
        categories["reason_gap"] = 1.0 if found else 0.0
        notes.append(f"reason names {found or 'no expected gap'}")
    else:
        notes.append("no gaps expected; reason_gap not graded")

    # Hard gate. An unproven skill presented as a strength is a failure whatever
    # the number said -- this is the regression that put an 85 on a card.
    for term in expected.get("must_not_credit") or []:
        if term_present(term, why):
            hard_failures.append(f"forbidden_strength_credited:{term}")

    blockers = result.get("blockers")
    if blockers is None:
        notes.append("blockers not graded (018B not shipped)")
    else:
        titles = " ".join(b.get("title", "") for b in blockers)
        hit = [t for t in must_flag if term_present(t, titles)]
        categories["blockers"] = (len(hit) / len(must_flag)) if must_flag else 1.0
        notes.append(f"blockers name {len(hit)}/{len(must_flag)} expected gaps")

    total = sum(WEIGHTS[k] * v for k, v in categories.items())
    weight = sum(WEIGHTS[k] for k in categories)
    overall = (total / weight) if weight else 0.0
    if hard_failures:
        overall = 0.0
    return {"id": fixture["id"], "score": score, "overall": round(overall, 3),
            "categories": {k: round(v, 3) for k, v in categories.items()},
            "hard_failures": hard_failures, "notes": notes, "why": why}


def invariance_failures(fixtures: list[dict], results: dict[str, dict]) -> list[str]:
    """Fixture groups stating identical requirements that scored too far apart."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for fixture in fixtures:
        group = (fixture["expected"].get("invariance_group") or "").strip()
        result = results.get(fixture["id"]) or {}
        if group and isinstance(result.get("score"), int):
            groups.setdefault(group, []).append((fixture["id"], result["score"]))

    failures = []
    for group, members in groups.items():
        if len(members) < 2:
            continue
        scores = [s for _, s in members]
        spread = max(scores) - min(scores)
        if spread > INVARIANCE_TOLERANCE:
            detail = ", ".join(f"{name}={score}" for name, score in members)
            failures.append(
                f"{group}: spread {spread} > {INVARIANCE_TOLERANCE} ({detail})")
    return failures
