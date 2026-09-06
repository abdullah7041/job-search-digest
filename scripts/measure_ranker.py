"""Measure what plan 018A's prompt change does to a real archived job.

This is the gate between 018A and 018B. The premise of plan 018 is that the
ranker inflates because it scores `PROFILE["blurb"]` -- a paragraph the
candidate wrote about himself -- instead of `resume.md`. This script tests that
premise on a job the digest actually delivered, by scoring it both ways.

It is a measurement tool, not production code. Nothing imports it.

Usage:

    OPENROUTER_API_KEY=sk-... python scripts/measure_ranker.py
    OPENROUTER_API_KEY=sk-... python scripts/measure_ranker.py \
        --company OmniOps --runs 3

Why it re-implements the old prompt instead of checking out the old file: both
prompts have to run in one process against one snapshot, or the comparison
picks up differences in the archive rather than in the prompt.

Reading the result -- the two things that matter, in order:

1. Reasons. Does the new `why` name an evidence gap (years, a credential, a
   required skill the resume does not demonstrate), or does it restate the
   title match? This is what shows the model is reading the evidence block.
2. Direction. Does the new median drop, and does the drop clear the spread of
   the runs? `chat_json` sends seed=7 and deliberately no temperature
   (openrouter.py:94-99), so this is reproducible-ish, NOT deterministic. A
   move inside run-to-run noise is not a result.

Do not tune toward Watheq's 58. Watheq scores a different rubric over a
different input window; agreement to the point is not the goal.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digest  # noqa: E402
from job_archive import jd_signal  # noqa: E402
from openrouter import chat_json  # noqa: E402


# The default grounding case: the most recent exact-85 in the archive that
# carries a complete posting. See plans/018.
DEFAULT_COMPANY = "OmniOps"
DEFAULT_TITLE = "Senior Solution Engineer"


def load_jobs(path: str) -> list[dict]:
    """Snapshots are a bare list in older archives and {"jobs": [...]} in newer."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data["jobs"] if isinstance(data, dict) else data


def as_live_job(snapshot: dict) -> dict:
    """Map an archived snapshot back to the shape `rank_jobs` reads.

    `rank_jobs` reads `job["description"]`; snapshots store the JD under
    `jd.text`. Archives written before that block exists carry a bare
    `description` instead, the same way `snapshot_grade` (job_archive.py:126)
    recovers a grade from archives that predate its field.
    """
    jd = snapshot.get("jd") or {}
    return {
        "id": snapshot.get("source_id") or snapshot.get("job_key") or "measured",
        "title": snapshot.get("title", ""),
        "company": snapshot.get("company", ""),
        "location": snapshot.get("location", ""),
        "description": jd.get("text") or snapshot.get("description") or "",
    }


def find_case(company: str, title: str) -> tuple[dict, str]:
    """The newest archived snapshot matching company (and title, if given)."""
    for path in sorted(glob.glob("jobs/*.json"), reverse=True):
        for snapshot in load_jobs(path):
            if company.lower() not in (snapshot.get("company") or "").lower():
                continue
            if title and title.lower() not in (snapshot.get("title") or "").lower():
                continue
            return snapshot, path
    raise SystemExit(f"No archived job matched company={company!r} title={title!r}")


def legacy_rank(jobs: list[dict], api_key: str) -> dict | None:
    """The pre-018A prompt, verbatim: the blurb alone, and no evidence rules.

    Kept in this throwaway script rather than left behind a flag in digest.py,
    so production carries exactly one prompt.
    """
    profile = digest.PROFILE["blurb"]
    targets = digest.PROFILE.get("target_titles")
    if targets:
        profile += " Currently targeting: " + ", ".join(targets) + "."
    listing = [
        {"id": j["id"], "title": j["title"], "company": j["company"],
         "location": j["location"],
         "description_kind": digest.jd_grade(j.get("description") or ""),
         "jd_excerpt": jd_signal(j.get("description") or "", digest.JD_RANK_CHARS)}
        for j in sorted(jobs, key=lambda j: j["id"])
    ]
    prompt = (
        f"{profile}\n\n"
        "Score each job 0-100 for fit to this candidate. Anchor every score to this rubric:\n"
        "- 90-100: senior-level INDIVIDUAL CONTRIBUTOR role in a core area (AI/LLM apps, "
        "software/backend, full-stack, data) in Saudi Arabia, and the JD's hard requirements "
        "are clearly covered by the profile. Reserve 95+ for near-perfect matches.\n"
        "- 70-89: senior IC role in a core area with one or two real gaps against the JD.\n"
        "- 40-69: adjacent function, seniority mismatch, or several gaps.\n"
        "- 0-39: leadership/staff/principal/management track, wrong location (including a title "
        "that says based elsewhere or offers relocation), or wrong function.\n"
        "jd_excerpt starts where the posting begins describing the job, so it is the "
        "requirements and responsibilities, not the company's recruiting copy. Score against "
        "what it states. A requirement the excerpt does not mention is unknown, not a gap: "
        "never lower a score for something the posting simply did not say.\n"
        "description_kind tells you what you are reading. 'full' is a complete posting. "
        "'teaser' is a short aggregator summary and 'incomplete' is a fragment — for those two, "
        "judge on title and function alone and say so in your reason. Never score above 85 when "
        "jd_excerpt is empty: a title alone cannot prove fit.\n"
        "Give a one-line reason of at most 15 words naming the deciding factor.\n"
        f"Jobs:\n{json.dumps(listing, ensure_ascii=False)}"
    )
    try:
        answer = chat_json(prompt, digest.RANK_SCHEMA, "job_fit_scores", api_key,
                           digest.RANK_MODEL, digest.RANK_FALLBACKS,
                           time.monotonic() + digest.RANK_BUDGET_SECONDS,
                           provider=digest.RANK_PROVIDER)
        return {str(it["id"]): {"score": int(it["score"]), "why": str(it["why"])[:140]}
                for it in answer["scores"]}
    except Exception as exc:  # noqa: BLE001 - a measurement run reports and continues
        print(f"  legacy call failed: {exc}", file=sys.stderr)
        return None


def run(label: str, ranker, job: dict, api_key: str, runs: int) -> list[int]:
    scores: list[int] = []
    print(f"\n--- {label} ---")
    for attempt in range(1, runs + 1):
        ranking = ranker([dict(job)], api_key)
        result = (ranking or {}).get(job["id"])
        if not result:
            print(f"  run {attempt}: no score returned")
            continue
        scores.append(result["score"])
        print(f"  run {attempt}: {result['score']:3d}  {result['why']}")
    return scores


def summarise(label: str, scores: list[int]) -> None:
    if not scores:
        print(f"{label:>8}: no scores")
        return
    spread = max(scores) - min(scores)
    print(f"{label:>8}: median {statistics.median(scores):.0f}  "
          f"range {min(scores)}-{max(scores)}  spread {spread}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set. This script makes live calls.")

    snapshot, path = find_case(args.company, args.title)
    job = as_live_job(snapshot)
    print(f"Case: {job['company']} — {job['title']}")
    print(f"  archive     : {path}")
    print(f"  archived fit: {snapshot.get('score')} "
          f"(ranking_verified={snapshot.get('ranking_verified')})")
    print(f"  jd          : {len(job['description'])} chars, "
          f"grade={digest.jd_grade(job['description'])}")
    print(f"  resume      : {len(digest.load_resume_evidence())} chars of evidence")
    print(f"  model       : {digest.RANK_MODEL}")

    before = run("BEFORE — blurb only (pre-018A)", legacy_rank, job, api_key, args.runs)
    after = run("AFTER — resume as evidence (018A)", digest.rank_jobs, job, api_key, args.runs)

    print("\n=== summary ===")
    summarise("before", before)
    summarise("after", after)
    if before and after:
        shift = statistics.median(after) - statistics.median(before)
        noise = max(max(before) - min(before), max(after) - min(after))
        print(f"\nmedian shift: {shift:+.0f}   worst within-arm spread: {noise}")
        if abs(shift) <= noise:
            print("VERDICT: the shift does not clear run-to-run noise. Not a result.")
        else:
            print("VERDICT: the shift clears run-to-run noise.")
        print("Now read the reasons above: do the AFTER reasons name an evidence gap?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
