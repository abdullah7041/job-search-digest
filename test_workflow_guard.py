import datetime as dt
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflow_guard import (
    apply_state_artifact,
    build_failure_alert,
    freshness_report,
    package_state_artifact,
    verify_state_artifact,
)


class WorkflowGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "jobs").mkdir()
        (self.root / "seen.json").write_text('{"job-1":"2026-08-11"}', encoding="utf-8")
        (self.root / "stats.json").write_text('[{"date":"2026-08-11"}]', encoding="utf-8")
        (self.root / "jobs" / "2026-08-11.json").write_text('[{"job_key":"job-1"}]', encoding="utf-8")
        self.artifact = Path(self.temp.name) / "artifact"

    def tearDown(self):
        self.temp.cleanup()

    def test_packages_only_allowed_state_with_verifiable_hashes(self):
        package_state_artifact(
            self.root,
            self.artifact,
            ["seen.json", "stats.json", "jobs/2026-08-11.json"],
        )

        manifest = json.loads((self.artifact / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["files"],
            [
                {"path": "jobs/2026-08-11.json", "sha256": hashlib.sha256(b'[{"job_key":"job-1"}]').hexdigest()},
                {"path": "seen.json", "sha256": hashlib.sha256(b'{"job-1":"2026-08-11"}').hexdigest()},
                {"path": "stats.json", "sha256": hashlib.sha256(b'[{"date":"2026-08-11"}]').hexdigest()},
            ],
        )
        self.assertEqual(verify_state_artifact(self.artifact), manifest["files"])

    def test_package_state_command_dispatches_and_writes_an_artifact(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "seen.json", "stats.json", "jobs"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "initial state"], cwd=self.root, check=True, capture_output=True, text=True)
        (self.root / "seen.json").write_text('{"job-1":"2026-08-12"}', encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("workflow_guard.py")),
                "package-state",
                "--root",
                str(self.root),
                "--output",
                str(self.artifact),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.artifact / "manifest.json").is_file())

    def test_packages_the_discovered_board_state(self):
        """boards.json remembers which employers have their own ATS board. It is
        written by a run and read by the next one, so a run that discovers a
        board and cannot persist it discovers the same board again tomorrow, for
        the same five requests, forever."""
        (self.root / "boards.json").write_text('{"nabla": {"source": "ashby"}}',
                                               encoding="utf-8")
        package_state_artifact(self.root, self.artifact, ["boards.json"])
        self.assertTrue((self.artifact / "boards.json").exists())

    def test_rejects_unexpected_or_unsafe_state_paths(self):
        for path in ["resume.md", "jobs/not-a-date.json", "../seen.json", "/seen.json"]:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    package_state_artifact(self.root, self.artifact, [path])

    def test_rejects_tampered_or_symlinked_artifacts(self):
        package_state_artifact(self.root, self.artifact, ["seen.json"])
        (self.artifact / "seen.json").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_state_artifact(self.artifact)

        clean_artifact = Path(self.temp.name) / "clean-artifact"
        package_state_artifact(self.root, clean_artifact, ["seen.json"])
        if hasattr(os, "symlink"):
            (clean_artifact / "seen.json").unlink()
            try:
                os.symlink(self.root / "seen.json", clean_artifact / "seen.json")
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(ValueError):
                verify_state_artifact(clean_artifact)

    def test_applies_verified_artifact_without_touching_other_files(self):
        package_state_artifact(self.root, self.artifact, ["seen.json", "jobs/2026-08-11.json"])
        target = Path(self.temp.name) / "target"
        target.mkdir()
        (target / "jobs").mkdir()
        (target / "README.md").write_text("keep", encoding="utf-8")

        apply_state_artifact(self.artifact, target)

        self.assertEqual((target / "seen.json").read_text(encoding="utf-8"), '{"job-1":"2026-08-11"}')
        self.assertEqual((target / "jobs" / "2026-08-11.json").read_text(encoding="utf-8"), '[{"job_key":"job-1"}]')
        self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "keep")

    def test_failure_alert_contains_only_operational_metadata(self):
        payload = build_failure_alert(
            workflow="job-digest",
            stage="persist-state",
            run_url="https://github.com/example/repo/actions/runs/123",
            timestamp="2026-08-11T08:00:00+03:00",
        )

        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("job-digest", payload["content"])
        self.assertIn("persist-state", payload["content"])
        self.assertIn("https://github.com/example/repo/actions/runs/123", payload["content"])
        self.assertNotIn("secret", payload["content"].lower())
        self.assertNotIn("\n\n", payload["content"])

    def test_freshness_requires_successful_riyadh_day_run_and_stats_row(self):
        fresh_run = {"conclusion": "success", "completedAt": "2026-08-11T05:01:21Z"}
        self.assertEqual(
            freshness_report(self.root / "stats.json", fresh_run, dt.date(2026, 8, 11)),
            [],
        )
        self.assertEqual(
            freshness_report(
                self.root / "stats.json",
                {"conclusion": "success", "updatedAt": "2026-08-11T05:01:21Z"},
                dt.date(2026, 8, 11),
            ),
            [],
        )
        self.assertEqual(
            freshness_report(self.root / "stats.json", {"conclusion": "failure", "completedAt": "2026-08-11T05:01:21Z"}, dt.date(2026, 8, 11)),
            ["latest digest run was not successful"],
        )
        self.assertEqual(
            freshness_report(self.root / "stats.json", {"conclusion": "success", "completedAt": "2026-08-10T20:59:59Z"}, dt.date(2026, 8, 11)),
            ["latest digest run did not complete on the Riyadh day"],
        )
        (self.root / "stats.json").write_text('[{"date":"2026-08-10"}]', encoding="utf-8")
        self.assertEqual(
            freshness_report(self.root / "stats.json", fresh_run, dt.date(2026, 8, 11)),
            ["stats.json has no row for the Riyadh day"],
        )


class TestDigestWorkflowPassesTheConfigItReads(unittest.TestCase):
    """Every tunable digest.py reads has to actually reach the scheduled run.

    JSEARCH_DAILY_REQUESTS was read by digest.py and documented as "override with
    a repository variable" for 100+ commits while digest.yml never passed it, so
    the budget silently stayed at the hardcoded default. That was invisible until
    the JSearch plan came within days of running dry (102 of 200 requests left on
    7 Sep 2026, ~21 days to reset, 6 spent a day). A comment is not a wire.
    """

    def _run_digest_env(self):
        import yaml
        path = Path(__file__).with_name(".github") / "workflows" / "digest.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job.get("steps") or []:
                if step.get("name") == "Run digest":
                    return step.get("env") or {}
        self.fail("digest.yml has no 'Run digest' step")

    def test_the_jsearch_budget_reaches_the_scheduled_run(self):
        env = self._run_digest_env()
        self.assertIn("JSEARCH_DAILY_REQUESTS", env)
        self.assertIn("vars.JSEARCH_DAILY_REQUESTS", env["JSEARCH_DAILY_REQUESTS"])

    def test_every_tunable_digest_reads_is_passed(self):
        """Names read via os.environ.get in digest.py, minus the ones that are
        secrets or set by the runner itself, must all appear in the step's env."""
        source = Path(__file__).with_name("digest.py").read_text(encoding="utf-8")
        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
        env = self._run_digest_env()
        missing = sorted(name for name in read if name not in env)
        self.assertEqual(missing, [], f"digest.py reads {missing}, digest.yml never passes them")


class TestActiveAtsStateIsPersisted(unittest.TestCase):
    """A state file the run writes but the workflow never commits is amnesia.

    The Active Jobs DB allowance is 250 jobs and 25 requests a MONTH, both hard.
    If the file recording what is left never survives the run, every morning
    starts believing it has a full allowance and the reserve protects nothing.
    """

    def test_the_guard_accepts_the_file_into_the_state_artifact(self):
        from workflow_guard import _safe_state_path
        self.assertEqual(_safe_state_path("active_ats.json"), "active_ats.json")

    def test_the_run_that_writes_it_can_hand_it_between_jobs(self):
        """execute writes it, persist-state commits it, and the artifact is the
        only thing that crosses between them."""
        from workflow_guard import _changed_state_paths
        self.assertIn("active_ats.json", inspect.getsource(_changed_state_paths))

    def test_the_workflow_commits_it(self):
        path = Path(__file__).with_name(".github") / "workflows" / "digest.yml"
        text = path.read_text(encoding="utf-8")
        add = [ln for ln in text.splitlines() if "git add --" in ln]
        self.assertTrue(add, "digest.yml no longer has a git add line")
        self.assertIn("active_ats.json", add[0])
