import contextlib
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
from unittest import mock
from pathlib import Path

from workflow_guard import (
    apply_state_artifact,
    build_failure_alert,
    freshness_report,
    latest_completed_run,
    package_state_artifact,
    verify_state_artifact,
)


class WorkflowGuardTests(unittest.TestCase):
    def test_freshness_logs_the_selected_run_from_unfiltered_history(self):
        from workflow_guard import main
        import io
        runs = [
            {"databaseId": 1, "status": "completed", "conclusion": "success",
             "createdAt": "2026-08-10T05:00:00Z", "updatedAt": "2026-08-10T05:01:00Z"},
            {"databaseId": 3, "status": "in_progress", "createdAt": "2026-08-11T06:00:00Z"},
            {"databaseId": 2, "status": "completed", "conclusion": "success",
             "createdAt": "2026-08-11T05:00:00Z", "updatedAt": "2026-08-11T05:01:00Z"},
        ]
        run_json = self.root / "runs.json"
        run_json.write_text(json.dumps(runs), encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["workflow_guard.py", "freshness", "--stats",
                                            str(self.root / "stats.json"), "--run-json",
                                            str(run_json), "--day", "2026-08-11"]), \
             mock.patch("sys.stdout", output):
            self.assertEqual(main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["latest_run"], runs[2])
        self.assertEqual(report["problems"], [])
        with contextlib.chdir(self.root):
            workflow = (Path(__file__).with_name(".github") / "workflows" /
                        "digest-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn("gh api", workflow)
        self.assertNotIn("--status completed", workflow)

    def test_run_selection_ignores_order_and_unfinished_runs(self):
        old = {"status": "completed", "createdAt": "2026-10-01T10:00:00Z", "conclusion": "success"}
        today = {"status": "completed", "createdAt": "2026-10-05T11:03:04Z", "conclusion": "success"}
        running = {"status": "in_progress", "createdAt": "2026-10-05T17:00:00Z"}
        self.assertEqual(latest_completed_run([old, running, today]), today)
        self.assertEqual(latest_completed_run([]), {})
        failed = dict(today, conclusion="failure", createdAt="2026-10-05T12:00:00Z")
        self.assertEqual(latest_completed_run([today, failed]), failed)

    def test_notification_identifies_itself_to_discord(self):
        from workflow_guard import main
        response = mock.MagicMock()
        response.status = 204
        response.__enter__.return_value = response
        with mock.patch("workflow_guard.urllib.request.urlopen", return_value=response) as send, \
             mock.patch.object(sys, "argv", ["workflow_guard.py", "notify", "--webhook", "https://discord.test/webhook",
                                            "--workflow", "watchdog", "--stage", "freshness",
                                            "--run-url", "https://github.com/example/run"]):
            self.assertEqual(main(), 0)
        request = send.call_args.args[0]
        self.assertTrue(request.get_header("User-agent", "").startswith("DiscordBot ("))

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

    JSEARCH_DAILY_REQUESTS was read by digest.py and documented as "override
    with a repository variable" for 100+ commits while digest.yml never passed
    it, so the budget silently stayed at the hardcoded default. That was
    invisible until the JSearch plan came within days of running dry. A comment
    is not a wire.

    Read as text rather than parsed as YAML: PyYAML is not in the pinned
    requirements, and adding a dependency to the daily run to assert something
    about a config file is a worse trade than a substring check.
    """

    def _digest_workflow(self):
        path = Path(__file__).with_name(".github") / "workflows" / "digest.yml"
        return path.read_text(encoding="utf-8")

    def test_the_jsearch_budget_reaches_the_scheduled_run(self):
        self.assertIn("JSEARCH_DAILY_REQUESTS: ${{ vars.JSEARCH_DAILY_REQUESTS }}",
                      self._digest_workflow())

    def test_manual_dispatch_is_ats_only_unless_rapidapi_is_opted_in(self):
        workflow = self._digest_workflow()
        self.assertIn("use_rapidapi:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("RAPIDAPI_ENABLED: ${{ github.event_name == 'schedule' || inputs.use_rapidapi }}",
                      workflow)

    def test_daily_and_paid_probe_share_one_quota_lock(self):
        digest_workflow = self._digest_workflow()
        probe = (Path(__file__).with_name(".github") / "workflows" /
                 "probe-jsearch.yml").read_text(encoding="utf-8")
        self.assertIn("group: rapidapi-quota", digest_workflow)
        self.assertIn("group: rapidapi-quota", probe)

    def test_paid_probe_requires_confirmation_and_commits_only_quota_state_on_main(self):
        probe = (Path(__file__).with_name(".github") / "workflows" /
                 "probe-jsearch.yml").read_text(encoding="utf-8")
        self.assertIn("confirm_rapidapi_spend:", probe)
        self.assertIn("default: false", probe)
        self.assertIn("refs/heads/main", probe)
        self.assertIn("CONFIRM_RAPIDAPI_SPEND: ${{ inputs.confirm_rapidapi_spend }}", probe)
        self.assertIn("git add -- active_ats.json", probe)

    def test_probe_displays_the_computed_cost_before_rejecting_unconfirmed_spend(self):
        probe = (Path(__file__).with_name(".github") / "workflows" /
                 "probe-jsearch.yml").read_text(encoding="utf-8")
        self.assertIn("Show predicted RapidAPI cost", probe)
        self.assertIn("overlap_count", probe)
        self.assertLess(probe.index("Show predicted RapidAPI cost"),
                        probe.index("Refuse unconfirmed or non-main paid probes"))
        self.assertNotIn("      bayt:", probe)

    def test_probe_rejects_combined_jsearch_modes_before_any_spend(self):
        probe = (Path(__file__).with_name(".github") / "workflows" /
                 "probe-jsearch.yml").read_text(encoding="utf-8")
        self.assertIn("Choose either freshness_query or overlap_queries", probe)

    def test_the_execute_job_outlasts_the_budgets_digest_holds_itself_to(self):
        """A job timeout cancels the job outright, so the state artifact is never
        packaged and seen.json never records cards Discord already holds --
        tomorrow then re-delivers them. digest.py bounds its own phases, and this
        pins that the workflow leaves room for all of them plus delivery."""
        import digest
        import fit_check
        budgeted = (digest.RUN_BUDGET_SECONDS + digest.RANK_BUDGET_SECONDS
                    + fit_check.CHECK_BUDGET_SECONDS)
        execute = self._digest_workflow().split("  execute:", 1)[1]
        minutes = int(re.search(r"timeout-minutes: (\d+)", execute).group(1))
        self.assertGreater(minutes * 60, budgeted,
                           "the execute job can be cancelled before digest.py "
                           "reaches delivery and state persistence")

    def test_every_tunable_digest_reads_is_passed(self):
        """Names read via os.environ.get in digest.py, which are either secrets
        or repository variables, must all be handed to the run."""
        source = Path(__file__).with_name("digest.py").read_text(encoding="utf-8")
        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
        # Set by the runner itself, never by the workflow author.
        read -= {"GITHUB_REPOSITORY", "CONFIRM_RAPIDAPI_SPEND"}
        workflow = self._digest_workflow()
        missing = sorted(name for name in read if f"{name}: ${{{{" not in workflow)
        self.assertEqual(missing, [],
                         f"digest.py reads {missing}, digest.yml never passes them")


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
