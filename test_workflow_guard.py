import datetime as dt
import hashlib
import json
import os
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
