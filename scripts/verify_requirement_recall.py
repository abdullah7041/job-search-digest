"""Measure, against every archived posting, how much of what a job DEMANDS the
scorer is actually shown.

This is the evidence behind plan 019. A Ma'aden posting was delivered on
2026-09-08 as a Strong match at fit 91 while Watheq scored the same posting 67
and named two gaps. The posting is 5187 characters; the ranker was sent
characters 217-2217 by `jd_signal(text, 2000)` -- every responsibility, and none
of the Minimum Qualifications block that begins at 3958.

So the question this answers is not "is the model good at scoring" but "was the
model ever shown the half of the posting that could lower a score". It needs no
API key and no network: it replays the extraction over the JSON the daily run
has already committed under jobs/.

It is a measurement tool, not production code. Nothing imports it.

Usage:

    python scripts/verify_requirement_recall.py
    python scripts/verify_requirement_recall.py --misses     # print what is lost
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import digest  # noqa: E402
from job_archive import jd_requirements, jd_signal  # noqa: E402

# Phrases that mean "this posting states a hard requirement". Deliberately
# coarse and deliberately not the extractor's own patterns: a recall measure
# that reused them would only prove the extractor agrees with itself.
MARKERS = ("bachelor", "degree", "years of experience")

# The recall the extraction measured at when plan 019 landed: 109 of the 120
# archived postings that state a qualification. The window alone recovered 71 at
# the old JD_RANK_CHARS of 2000 and 102 at 2600, so both halves of the fix carry
# weight and the run below reports them side by side.
#
# A floor rather than an equality, so adding archives cannot fail the check on
# its own -- but a regression in the patterns will.
MIN_RECALL = 0.88


def archived_descriptions(archive_dir: str) -> list[str]:
    found = []
    for path in sorted(glob.glob(os.path.join(archive_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                archive = json.load(handle)
        except (OSError, ValueError):
            continue
        for snapshot in archive.get("jobs") or []:
            text = (snapshot.get("jd") or {}).get("text") or ""
            if text:
                found.append((snapshot.get("company", ""),
                              snapshot.get("title", ""), text))
    return found


def _ascii(text: str, width: int) -> str:
    """A console-safe, fixed-width cell. Unprintable characters become '?'."""
    safe = (text or "")[:width].encode("ascii", "replace").decode("ascii")
    return f"{safe:<{width}}"


def stated(text: str) -> list[str]:
    lowered = text.lower()
    return [marker for marker in MARKERS if marker in lowered]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", default=os.path.join(REPO, "jobs"))
    parser.add_argument("--misses", action="store_true",
                        help="print the postings whose requirement is still lost")
    args = parser.parse_args()

    postings = archived_descriptions(args.archive_dir)
    if not postings:
        print(f"No archived postings under {args.archive_dir}", file=sys.stderr)
        return 1

    window_lost, extractor_lost, sizes, misses = 0, 0, [], []
    with_requirement = 0
    for company, title, text in postings:
        markers = stated(text)
        if not markers:
            continue
        with_requirement += 1
        window = jd_signal(text, digest.JD_RANK_CHARS).lower()
        extracted = " ".join(jd_requirements(text))
        sizes.append(len(extracted))
        if [m for m in markers if m not in window]:
            window_lost += 1
        lost = [m for m in markers if m not in extracted.lower()]
        if lost:
            extractor_lost += 1
            misses.append((company, title, len(text), lost))

    recovered = with_requirement - extractor_lost
    recall = recovered / with_requirement

    print(f"archived postings carrying JD text:      {len(postings)}")
    print(f"  of those, stating a qualification:     {with_requirement}")
    print()
    for label, lost in ((f"lost to jd_signal({digest.JD_RANK_CHARS}) alone:", window_lost),
                        ("lost to jd_requirements:", extractor_lost)):
        print(f"{label:<34}{lost:>4}   (recovered {with_requirement - lost})")
    print(f"recall: {recall:.0%}   floor: {MIN_RECALL:.0%}")
    print(f"extraction size: mean {sum(sizes) / len(sizes):.0f} chars, "
          f"max {max(sizes)}")

    if args.misses:
        print("\nstill lost:")
        for company, title, length, lost in misses:
            # Titles arrive in Arabic as often as English, and this runs in a
            # Windows console on cp1252 where printing one raises rather than
            # degrading. A measurement tool must not die on its own output.
            print(f"  {_ascii(company, 24)} {_ascii(title, 38)} {length:>5}  {lost}")

    if recall < MIN_RECALL:
        print(f"\nFAIL recall {recall:.0%} is below the {MIN_RECALL:.0%} floor "
              "plan 019 measured; the extraction patterns have regressed",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
