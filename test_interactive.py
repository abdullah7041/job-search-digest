#!/usr/bin/env python3
"""Offline contract tests for the interactive tailoring pipeline."""

import hashlib
import hmac
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from job_archive import make_snapshot, job_card_payload, write_snapshots
from resume_source import validate_tailored_resume
from resume_ingest import prepare_resume_preview
from discord_delivery import interactive_cards_payload
from interactive_runner import process_task, run_task


JOB = {
    "id": "provider-42",
    "source": "greenhouse",
    "title": "Senior Machine Learning Engineer",
    "company": "Example Labs",
    "location": "Riyadh, Saudi Arabia",
    "url": "https://jobs.example.test/42",
    "description": (
        "About the role: Example Labs is hiring a senior engineer to build reliable machine learning products. "
        "Responsibilities include designing production Python services, developing LLM retrieval systems, reviewing "
        "architecture, monitoring model quality, documenting decisions, and partnering with product and data teams. "
        "Requirements include five years of software engineering experience, strong Python and SQL, hands-on API "
        "design, cloud deployment, automated testing, observability, and clear written communication. Candidates should "
        "have delivered maintainable systems, handled production incidents, and worked effectively with cross-functional "
        "teams. Experience with containers, CI/CD, evaluation pipelines, and responsible AI practices is preferred."
    ),
    "score": 91,
    "why": "Strong Python and LLM overlap",
    "tier": "Strong matches",
    "link_status": "open",
    "ranking_verified": True,
}


class TestJobArchive(unittest.TestCase):
    def test_snapshot_has_stable_key_and_hashed_full_jd(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")

        self.assertEqual(snapshot["job_key"], "greenhouse:provider-42")
        self.assertEqual(snapshot["jd"]["status"], "captured")
        self.assertEqual(
            snapshot["jd"]["sha256"],
            hashlib.sha256(JOB["description"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(snapshot["jd"]["text"], JOB["description"])

    def test_card_uses_opaque_tailor_button_for_captured_jd(self):
        payload = job_card_payload(make_snapshot(JOB, "2026-08-03T12:00:00Z"))

        self.assertEqual(payload["embeds"][0]["title"], "Senior Machine Learning Engineer")
        button = payload["components"][0]["components"][0]
        self.assertEqual(button["label"], "Tailor CV")
        self.assertEqual(button["custom_id"], "tailor:f765d605515d048a68135fcbf73b66f2")
        self.assertLessEqual(len(button["custom_id"]), 100)
        self.assertFalse(button["disabled"])

    def test_card_keeps_tailor_button_for_missing_jd_to_start_recovery(self):
        job = dict(JOB, description="")
        payload = job_card_payload(make_snapshot(job, "2026-08-03T12:00:00Z"))

        self.assertIn("Job description needed", payload["embeds"][0]["footer"]["text"])
        self.assertFalse(payload["components"][0]["components"][0]["disabled"])

    def test_snapshot_rejects_a_nonempty_jd_fragment_as_incomplete(self):
        job = dict(JOB, description="Python and SQL required.")

        snapshot = make_snapshot(job, "2026-08-03T12:00:00Z")

        self.assertEqual(snapshot["jd"]["status"], "incomplete")
        self.assertIn("too_short", snapshot["jd"]["completeness_failures"])

    def test_write_snapshots_uses_one_date_file_with_full_jd(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_snapshots([JOB], "2026-08-03", root)
            self.assertEqual(path, os.path.join(root, "2026-08-03.json"))
            with open(path, encoding="utf-8") as handle:
                archive = json.load(handle)

        self.assertEqual(archive["schema_version"], 1)
        self.assertEqual(archive["jobs"][0]["job_key"], "greenhouse:provider-42")
        self.assertEqual(archive["jobs"][0]["jd"]["text"], JOB["description"])

    def test_write_snapshots_appends_new_jobs_without_mutating_existing_same_day_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            write_snapshots([JOB], "2026-08-03", root)
            changed = dict(JOB, description=JOB["description"] + " Changed later.")
            second = dict(JOB, id="provider-43", url="https://jobs.example.test/43")
            path = write_snapshots([changed, second], "2026-08-03", root)
            with open(path, encoding="utf-8") as handle:
                archive = json.load(handle)

        self.assertEqual(len(archive["jobs"]), 2)
        original = next(item for item in archive["jobs"] if item["source_id"] == "provider-42")
        self.assertEqual(original["jd"]["text"], JOB["description"])

    def test_worker_payload_keeps_one_card_per_snapshot(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        payload = interactive_cards_payload([snapshot])

        self.assertEqual(payload["cards"][0]["job_key"], "greenhouse:provider-42")
        self.assertEqual(payload["cards"][0]["snapshot"], snapshot)
        self.assertEqual(payload["cards"][0]["payload"]["components"][0]["components"][0]["label"], "Tailor CV")

    def test_worker_payload_carries_an_application_owned_quiet_day_heartbeat(self):
        heartbeat = {"key": "heartbeat:2026-08-03", "content": "No new roles today."}

        payload = interactive_cards_payload([], heartbeat=heartbeat)

        self.assertEqual(payload["heartbeat"], heartbeat)


class TestTailoringEvidenceGate(unittest.TestCase):
    SOURCE = {
        "exp-1": "Built Python data pipelines that reduced reporting time by 40%.",
        "project-1": "Created an LLM retrieval prototype using Python.",
    }

    def test_accepts_reordered_supported_bullets_with_source_references(self):
        tailored = {
            "summary": "Python engineer with LLM retrieval experience.",
            "bullets": [
                {"text": "Created an LLM retrieval prototype using Python.", "source_refs": ["project-1"]},
                {"text": "Built Python data pipelines that reduced reporting time by 40%.", "source_refs": ["exp-1"]},
            ],
            "gaps": ["Kubernetes is not evidenced in the source CV."],
        }
        self.assertEqual(validate_tailored_resume(tailored, self.SOURCE), [])

    def test_rejects_hard_fact_not_present_in_its_cited_source(self):
        tailored = {
            "summary": "Python engineer.",
            "bullets": [{"text": "Reduced reporting time by 90%.", "source_refs": ["exp-1"]}],
            "gaps": [],
        }
        self.assertIn("90%", validate_tailored_resume(tailored, self.SOURCE)[0])

    def test_rejects_bullet_without_source_reference(self):
        tailored = {
            "summary": "Python engineer.",
            "bullets": [{"text": "Built Python pipelines.", "source_refs": []}],
            "gaps": [],
        }
        self.assertIn("source reference", validate_tailored_resume(tailored, self.SOURCE)[0])


class TestResumeIngestion(unittest.TestCase):
    def test_preview_normalizes_text_and_records_source_hash(self):
        raw = "Abdullah Ahmed\n\nSenior Data Engineer\n\nBuilt Python pipelines."
        preview = prepare_resume_preview(raw, "resume.pdf")

        self.assertEqual(preview["markdown"], "# Abdullah Ahmed\n\nSenior Data Engineer\n\nBuilt Python pipelines.\n")
        self.assertEqual(preview["metadata"]["filename"], "resume.pdf")
        self.assertEqual(preview["metadata"]["source_sha256"], hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.assertEqual(preview["warnings"], [])

    def test_preview_rejects_text_that_cannot_be_reviewed(self):
        with self.assertRaisesRegex(ValueError, "too little readable text"):
            prepare_resume_preview("Name", "resume.pdf")


class TestInteractiveRunner(unittest.TestCase):
    def test_runner_accepts_only_a_hash_verified_full_jd_snapshot(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")

        result = run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"})

        self.assertEqual(result["status"], "completed")
        self.assertIn(JOB["title"], result["content"])
        self.assertEqual(result["job_key"], snapshot["job_key"])

    def test_runner_returns_a_private_pdf_for_a_verified_snapshot(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        result = run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"})

        self.assertEqual(result["filename"], "tailored-resume.pdf")
        self.assertTrue(result["pdf_base64"])
        self.assertEqual(result["jd_sha256"], snapshot["jd"]["sha256"])

    def test_runner_rejects_a_changed_jd(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        snapshot["jd"]["text"] = "A different description"

        with self.assertRaisesRegex(ValueError, "digest"):
            run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"})

    def test_runner_rejects_a_hash_valid_fragment_marked_as_captured(self):
        fragment = "Python and SQL required."
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        snapshot["jd"] = {
            "status": "captured",
            "text": fragment,
            "sha256": hashlib.sha256(fragment.encode()).hexdigest(),
            "completeness_failures": [],
        }

        with self.assertRaisesRegex(ValueError, "complete job description"):
            run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"})

    def test_process_task_uses_hmac_for_input_and_result_requests(self):
        secret = "callback-secret"
        task_id = "task-123"
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def _valid_signature(self, body):
                timestamp = self.headers.get("X-Job-Digest-Timestamp", "")
                nonce = self.headers.get("X-Job-Digest-Nonce", "")
                canonical = f"{timestamp}\n{nonce}\n{self.command}\n{self.path}\n".encode() + body
                expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
                return hmac.compare_digest(self.headers.get("X-Job-Digest-Signature", ""), expected)

            def do_GET(self):
                received["get_path"] = self.path
                received["get_signed"] = self._valid_signature(b"")
                body = json.dumps({"input": {"job_snapshot": snapshot}, "status": "dispatched"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received["post_path"] = self.path
                received["post_signed"] = self._valid_signature(body)
                received["result"] = json.loads(body)
                response = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            succeeded = process_task(f"http://127.0.0.1:{server.server_port}", secret, task_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertTrue(succeeded)
        self.assertEqual(received["get_path"], f"/internal/tasks/{task_id}/input")
        self.assertTrue(received["get_signed"])
        self.assertEqual(received["post_path"], f"/internal/tasks/{task_id}/result")
        self.assertTrue(received["post_signed"])
        self.assertEqual(received["result"]["status"], "completed")


class TestInteractiveWorkflowWiring(unittest.TestCase):
    """The runner's tailoring silently degrades to an untailored resume when
    GEMINI_API_KEY is absent, so the workflow must supply it."""

    WORKFLOW = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "interactive.yml"
    )

    def _workflow_text(self):
        with open(self.WORKFLOW, encoding="utf-8") as handle:
            return handle.read()

    def test_workflow_passes_the_gemini_key_to_the_runner(self):
        self.assertIn("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", self._workflow_text())

    def test_workflow_still_passes_the_worker_callback_configuration(self):
        text = self._workflow_text()
        for name in ("INTERACTIVE_WORKER_URL", "WORKER_CALLBACK_SECRET", "TASK_ID"):
            self.assertIn(f"{name}:", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
