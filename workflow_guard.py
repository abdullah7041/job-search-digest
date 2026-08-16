"""Small, dependency-free guardrails for stateful GitHub Actions workflows."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo


RIYADH = ZoneInfo("Asia/Riyadh")
_JOB_SNAPSHOT = __import__("re").compile(r"jobs/\d{4}-\d{2}-\d{2}\.json\Z")


def _safe_state_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    normalized = path.as_posix()
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe state path: {value}")
    if normalized in {"seen.json", "stats.json"} or _JOB_SNAPSHOT.fullmatch(normalized):
        return normalized
    raise ValueError(f"unexpected state path: {value}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_state_artifact(root: Path, artifact: Path, paths: list[str]) -> None:
    """Copy validated generated state into a new artifact directory."""
    root = root.resolve()
    if artifact.exists():
        raise ValueError(f"artifact already exists: {artifact}")
    entries = []
    normalized_paths = sorted({_safe_state_path(path) for path in paths})
    artifact.mkdir(parents=True)
    for relative in normalized_paths:
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"state file is missing or unsafe: {relative}")
        destination = artifact / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entries.append({"path": relative, "sha256": _sha256(destination)})
    (artifact / "manifest.json").write_text(
        json.dumps({"files": entries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_state_artifact(artifact: Path) -> list[dict[str, str]]:
    """Return validated manifest entries or raise ValueError without copying data."""
    manifest_path = artifact / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("state artifact has no safe manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("state artifact manifest is invalid") from error
    if not isinstance(entries, list) or not entries:
        raise ValueError("state artifact manifest has no files")
    paths = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("state artifact manifest entry is invalid")
        relative = _safe_state_path(entry["path"])
        if not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64:
            raise ValueError("state artifact hash is invalid")
        source = artifact / relative
        if source.is_symlink() or not source.is_file() or _sha256(source) != entry["sha256"]:
            raise ValueError(f"state artifact file failed verification: {relative}")
        paths.append(relative)
    if paths != sorted(set(paths)):
        raise ValueError("state artifact paths are not unique and sorted")
    return entries


def apply_state_artifact(artifact: Path, target: Path) -> list[str]:
    """Copy a verified artifact into a checkout without altering any other paths."""
    entries = verify_state_artifact(artifact)
    applied = []
    for entry in entries:
        relative = entry["path"]
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact / relative, destination)
        applied.append(relative)
    return applied


def build_failure_alert(*, workflow: str, stage: str, run_url: str, timestamp: str) -> dict[str, object]:
    """Build a deliberately minimal Discord payload with mentions disabled."""
    fields = [" ".join(value.split())[:120] for value in (workflow, stage, timestamp)]
    if not run_url.startswith("https://github.com/"):
        raise ValueError("run URL must be a GitHub HTTPS URL")
    return {
        "content": f"⚠️ {fields[0]} failed at {fields[1]} ({fields[2]}). Run: {run_url}",
        "allowed_mentions": {"parse": []},
    }


def freshness_report(stats_path: Path, latest_run: dict[str, object], day: dt.date) -> list[str]:
    """Report independently observable missing-digest signals for one Riyadh day."""
    problems = []
    if latest_run.get("conclusion") != "success":
        problems.append("latest digest run was not successful")
    else:
        try:
            completed_at = latest_run.get("completedAt") or latest_run.get("updatedAt")
            completed = dt.datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
            if completed.astimezone(RIYADH).date() != day:
                problems.append("latest digest run did not complete on the Riyadh day")
        except (KeyError, TypeError, ValueError):
            problems.append("latest digest run has no valid completion time")
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stats = []
    if not isinstance(stats, list) or not any(isinstance(row, dict) and row.get("date") == day.isoformat() for row in stats):
        problems.append("stats.json has no row for the Riyadh day")
    return problems


def _changed_state_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "seen.json", "stats.json", "jobs"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if len(line) < 4 or line[:2].strip() in {"R", "C"}:
            raise ValueError(f"unsupported git state entry: {line}")
        paths.append(line[3:])
    return paths


def _command_package(args: argparse.Namespace) -> int:
    package_state_artifact(Path(args.root), Path(args.output), _changed_state_paths(Path(args.root)))
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    print(json.dumps(verify_state_artifact(Path(args.artifact)), sort_keys=True))
    return 0


def _command_apply(args: argparse.Namespace) -> int:
    print(json.dumps(apply_state_artifact(Path(args.artifact), Path(args.target))))
    return 0


def _command_freshness(args: argparse.Namespace) -> int:
    day = dt.date.fromisoformat(args.day) if args.day else dt.datetime.now(RIYADH).date()
    latest_run = json.loads(Path(args.run_json).read_text(encoding="utf-8"))
    problems = freshness_report(Path(args.stats), latest_run, day)
    print(json.dumps({"day": day.isoformat(), "problems": problems}, sort_keys=True))
    return 1 if problems else 0


def _command_notify(args: argparse.Namespace) -> int:
    payload = build_failure_alert(
        workflow=args.workflow,
        stage=args.stage,
        run_url=args.run_url,
        timestamp=args.timestamp or dt.datetime.now(RIYADH).isoformat(),
    )
    request = urllib.request.Request(
        args.webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Discord webhook returned HTTP {response.status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package-state")
    package.add_argument("--root", default=".")
    package.add_argument("--output", required=True)
    package.set_defaults(func=_command_package)
    verify = commands.add_parser("verify-state")
    verify.add_argument("--artifact", required=True)
    verify.set_defaults(func=_command_verify)
    apply = commands.add_parser("apply-state")
    apply.add_argument("--artifact", required=True)
    apply.add_argument("--target", default=".")
    apply.set_defaults(func=_command_apply)
    freshness = commands.add_parser("freshness")
    freshness.add_argument("--stats", required=True)
    freshness.add_argument("--run-json", required=True)
    freshness.add_argument("--day")
    freshness.set_defaults(func=_command_freshness)
    notify = commands.add_parser("notify")
    notify.add_argument("--webhook", required=True)
    notify.add_argument("--workflow", required=True)
    notify.add_argument("--stage", required=True)
    notify.add_argument("--run-url", required=True)
    notify.add_argument("--timestamp")
    notify.set_defaults(func=_command_notify)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
