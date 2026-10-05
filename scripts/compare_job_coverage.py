"""Compare the first fourteen days with source-yield reporting to prior days."""

import argparse
import json
import sys
from pathlib import Path


def compare_coverage(rows, runs=14):
    # stats.json has one row per date; deduplicate defensively for old history.
    rows = sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])
    changed = [row for row in rows if "source_yield" in row]
    if len(changed) < runs:
        return {"status": "pending", "collected_runs": len(changed), "required_runs": runs}
    after = changed[:runs]
    before = [row for row in rows if row["date"] < after[0]["date"]][-runs:]
    if len(before) < runs:
        return {"status": "unavailable", "reason": "Insufficient pre-change baseline",
                "baseline_runs": len(before), "required_runs": runs}

    def summarize(group):
        return {"runs": len(group), "start": group[0]["date"] if group else None,
                "end": group[-1]["date"] if group else None,
                **{metric + "_per_run": round(sum(row.get(metric) or 0 for row in group) / len(group), 2)
                   if group else None for metric in ("fetched", "new", "delivered")}}

    sources = {}
    for row in after:
        for source, counts in row["source_yield"].items():
            totals = sources.setdefault(source, {"fetched": 0, "new": 0, "delivered": 0, "quota_paused_runs": 0})
            for metric in ("fetched", "new", "delivered"):
                totals[metric] += counts[metric]
            totals["quota_paused_runs"] += bool(counts["quota_paused"])
    return {"status": "complete", "baseline": summarize(before), "after": summarize(after), "sources": sources}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", default="stats.json", help="Stats file, or - to read stdin")
    args = parser.parse_args()
    rows = json.load(sys.stdin) if args.stats == "-" else json.loads(Path(args.stats).read_text(encoding="utf-8"))
    print(json.dumps(compare_coverage(rows), indent=2, sort_keys=True))
