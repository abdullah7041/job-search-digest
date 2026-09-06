"""Run the ranker against the gold set and grade it.

Makes live model calls, so it needs OPENROUTER_API_KEY and is never part of the
test suites. `test_eval.py` covers the scorer offline; this covers the model.

    OPENROUTER_API_KEY=sk-... python eval/run_eval.py
    OPENROUTER_API_KEY=sk-... python eval/run_eval.py --runs 3
    OPENROUTER_API_KEY=sk-... python eval/run_eval.py --only hungerstation

Every fixture goes into ONE `rank_jobs` call, because that is how production
scores a day's jobs -- one call for the whole listing. Grading them one call at
a time would measure something the digest never does, and `rank_jobs` already
sorts its listing by id so a score does not depend on neighbours.

Reading the output: a hard failure is a hard failure, whatever the overall says.
`chat_json` sends seed=7 and no temperature, so a single run is reproducible-ish
but not deterministic -- use `--runs 3` before concluding anything about a
change, and treat a move inside the spread as noise.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# digest reads profile.json and resume.md at import time, relative to cwd, and
# falls back to DEFAULT_PROFILE with only a warning. Anchor before importing or
# this grades the wrong candidate and still prints a confident verdict.
os.chdir(REPO)

import digest  # noqa: E402
from eval.score import grade, invariance_failures  # noqa: E402

FIXTURE_DIR = os.path.join(REPO, "eval", "fixtures")
PASS_THRESHOLD = 0.7


def load_fixtures(only: str = "") -> list[dict]:
    fixtures = []
    for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json"))):
        with io.open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        if not only or only.lower() in fixture["id"].lower():
            fixtures.append(fixture)
    if not fixtures:
        raise SystemExit(f"No fixtures matched {only!r}")
    return fixtures


def rank_once(fixtures: list[dict], api_key: str) -> dict[str, dict]:
    """One rank_jobs call over every fixture job, keyed back by fixture id."""
    by_job_id = {f["job"]["id"]: f["id"] for f in fixtures}
    ranking = digest.rank_jobs([dict(f["job"]) for f in fixtures], api_key)
    if ranking is None:
        return {}
    return {by_job_id[k]: v for k, v in ranking.items() if k in by_job_id}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", default="", help="substring filter on fixture id")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set. This makes live model calls.")

    fixtures = load_fixtures(args.only)
    print(f"{len(fixtures)} fixtures, {args.runs} run(s), model {digest.RANK_MODEL}")
    print(f"resume evidence: {len(digest.load_resume_evidence())} chars\n")

    per_run: list[dict[str, dict]] = []
    for attempt in range(1, args.runs + 1):
        results = rank_once(fixtures, api_key)
        if not results:
            print(f"run {attempt}: ranker returned nothing")
            continue
        per_run.append(results)

    if not per_run:
        raise SystemExit("No run produced a ranking.")

    report, failed = [], 0
    for fixture in fixtures:
        scores = [r[fixture["id"]]["score"] for r in per_run if fixture["id"] in r]
        graded = [grade(fixture, r[fixture["id"]]) for r in per_run if fixture["id"] in r]
        if not graded:
            print(f"MISSING  {fixture['id']}: no score in any run")
            failed += 1
            continue
        overall = statistics.median(g["overall"] for g in graded)
        hard = sorted({h for g in graded for h in g["hard_failures"]})
        low, high = fixture["expected"]["score_band"]
        ok = overall >= PASS_THRESHOLD and not hard
        failed += 0 if ok else 1
        spread = max(scores) - min(scores) if len(scores) > 1 else 0
        print(f"{'PASS' if ok else 'FAIL'}  {fixture['id']}")
        print(f"      scored {scores}  (spread {spread})  expected {low}-{high}")
        print(f"      overall {overall:.2f}   {graded[-1]['categories']}")
        print(f"      why: {graded[-1]['why']}")
        for note in graded[-1]["notes"]:
            print(f"      - {note}")
        for failure in hard:
            print(f"      HARD FAILURE: {failure}")
        print()
        report.append({"id": fixture["id"], "scores": scores, "overall": overall,
                       "hard_failures": hard, "band": [low, high]})

    invariance = invariance_failures(fixtures, per_run[-1])
    for failure in invariance:
        print(f"INVARIANCE FAIL: {failure}")
    failed += len(invariance)

    print(f"\n=== {len(fixtures) - failed}/{len(fixtures)} fixtures pass "
          f"(threshold {PASS_THRESHOLD}) ===")
    if args.json:
        print(json.dumps({"fixtures": report, "invariance": invariance}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
