#!/usr/bin/env python3
"""Offline contract tests for the interactive tailoring pipeline."""

import datetime
import hashlib
import hmac
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest import mock

import interactive_runner
import resume_tailor
from job_archive import make_snapshot, job_card_payload, prune_snapshots, write_snapshots
from resume_render import parse_resume, render_pdf, source_refs
from resume_source import validate_tailored_resume, vocabulary
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

    def test_prune_removes_archives_older_than_the_ttl_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as root:
            # Written directly rather than via write_snapshots: write_snapshots
            # prunes against wall-clock today on every call, so backfilling an
            # old date through it would delete the 2026-01-01 fixture before
            # this test's own pinned-clock prune ever runs.
            for date in ("2026-01-01", "2026-08-01", "2026-08-09"):
                with open(os.path.join(root, f"{date}.json"), "w", encoding="utf-8") as handle:
                    json.dump({"schema_version": 1, "date": date, "jobs": []}, handle)
            removed = prune_snapshots(root, ttl_days=120, today=datetime.date(2026, 8, 9))

            remaining = sorted(os.listdir(root))

        self.assertEqual([os.path.basename(path) for path in removed], ["2026-01-01.json"])
        self.assertEqual(remaining, ["2026-08-01.json", "2026-08-09.json"])

    def test_prune_ignores_files_it_does_not_recognise(self):
        with tempfile.TemporaryDirectory() as root:
            write_snapshots([JOB], "2026-01-01", root)
            with open(os.path.join(root, "notes.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            prune_snapshots(root, ttl_days=120, today=datetime.date(2026, 8, 9))

            remaining = sorted(os.listdir(root))

        self.assertEqual(remaining, ["notes.json"])

    def test_writing_todays_archive_never_prunes_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_snapshots([JOB], "2026-08-09", root)
            self.assertTrue(os.path.exists(path))


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
MASTER = """# ADA LOVELACE
**AI Product Engineer**
Riyadh, Saudi Arabia · ada@example.test

## SUMMARY
AI Product Engineer shipping production LLM systems, with 5+ years across engineering.

## EXPERIENCE

### Founder & AI Product Engineer — Watheq | Riyadh, Saudi Arabia | Aug 2025 – Present
Arabic-first AI SaaS, live in production.

- Built an LLM evaluation pipeline that judge-scores prompt and model variants.
- Instrumented production monitoring with Sentry and real-time Telegram alerting.
- Implemented credit-based monetization with guest rate limiting.

### Data Analyst — Alghalia | Al-Ahsa, Saudi Arabia | Mar 2020 – Present
- Cut expired-unit waste 60% year-over-year through quantitative analysis.
- Delivered weekly performance reporting for 4+ years.

## SKILLS
- **Languages:** Python, TypeScript, SQL
"""


def _plan(**overrides):
    plan = {
        "headline": "AI Product Engineer / Machine Learning Engineer",
        "summary": "AI Product Engineer shipping production LLM systems, with 5+ years across engineering.",
        "bullets": [
            {"ref": "b1", "text": "Built an LLM evaluation pipeline that judge-scores prompt and model variants."},
            {"ref": "b2", "text": "Instrumented production monitoring with Sentry and real-time Telegram alerting."},
            {"ref": "b4", "text": "Cut expired-unit waste 60% year-over-year through quantitative analysis."},
            {"ref": "b5", "text": "Delivered weekly performance reporting for 4+ years."},
        ],
        "gaps": [
            "The resume does not evidence PyTorch or TensorFlow, both named in the job description.",
            "No experience with AWS, GCP or Azure is shown anywhere in the resume.",
        ],
        "fit_notes": ["Direct production LLM evaluation experience."],
    }
    plan.update(overrides)
    return plan


class TestResumeStructure(unittest.TestCase):
    def test_parser_separates_name_headline_contact_and_bullets(self):
        document = parse_resume(MASTER)

        self.assertEqual(document["name"], "ADA LOVELACE")
        self.assertEqual(document["headline"], "AI Product Engineer")
        self.assertEqual(document["contact"], ["Riyadh, Saudi Arabia · ada@example.test"])
        self.assertEqual(len(document["sections"][0]["entries"]), 2)
        self.assertEqual(document["sections"][0]["entries"][0]["lead"],
                         "Arabic-first AI SaaS, live in production.")

    def test_rendered_pdf_is_one_page_and_carries_no_markdown_syntax(self):
        from pypdf import PdfReader

        pdf = render_pdf(parse_resume(MASTER))
        reader = PdfReader(BytesIO(pdf))
        text = reader.pages[0].extract_text()

        self.assertEqual(len(reader.pages), 1)
        for leak in ("##", "**", "- **", "### "):
            self.assertNotIn(leak, text)
        self.assertIn("ADA LOVELACE", text)
        self.assertIn("EXPERIENCE", text)

    def test_full_master_resume_still_fits_one_page(self):
        from pypdf import PdfReader

        with open("resume.md", encoding="utf-8") as handle:
            document = parse_resume(handle.read())

        self.assertEqual(len(PdfReader(BytesIO(render_pdf(document))).pages), 1)


class TestEvidenceGuards(unittest.TestCase):
    def test_rejects_an_invented_framework_in_a_rewritten_bullet(self):
        sources = {"b1": "Built an LLM evaluation pipeline that judge-scores prompt variants."}
        tailored = {
            "summary": "",
            "bullets": [{"text": "Built an evaluation pipeline in PyTorch.", "source_refs": ["b1"]}],
        }

        errors = validate_tailored_resume(tailored, sources, vocabulary(MASTER), MASTER)

        self.assertTrue(any("PyTorch" in error for error in errors))

    def test_accepts_a_rephrasing_that_keeps_every_hard_fact(self):
        sources = {"b4": "Cut expired-unit waste 60% year-over-year through quantitative analysis."}
        tailored = {
            "summary": "",
            "bullets": [{"text": "Reduced expired-unit waste by 60% year-over-year using quantitative analysis.",
                         "source_refs": ["b4"]}],
        }

        self.assertEqual(validate_tailored_resume(tailored, sources, vocabulary(MASTER), MASTER), [])

    def test_thousands_separators_do_not_read_as_invented_numbers(self):
        sources = {"b1": "Built the platform solo in 28,000+ lines of TypeScript."}
        tailored = {"summary": "", "bullets": [
            {"text": "Shipped 28,000 lines of TypeScript solo.", "source_refs": ["b1"]}]}

        self.assertEqual(validate_tailored_resume(tailored, sources, vocabulary(MASTER), MASTER), [])


class TestTailoringPlan(unittest.TestCase):
    def test_plan_keeps_only_cited_bullets_in_the_models_order(self):
        document = parse_resume(MASTER)
        plan = _plan(bullets=[
            {"ref": "b2", "text": "Instrumented production monitoring with Sentry."},
            {"ref": "b1", "text": "Built an LLM evaluation pipeline."},
        ])

        tailored = resume_tailor.apply_plan(document, plan)
        watheq = tailored["sections"][0]["entries"][0]

        self.assertEqual([bullet["ref"] for bullet in watheq["bullets"]], ["b2", "b1"])
        self.assertEqual(tailored["headline"], "AI Product Engineer / Machine Learning Engineer")

    def test_a_role_with_no_kept_bullets_leaves_the_resume(self):
        document = parse_resume(MASTER)
        plan = _plan(bullets=[{"ref": "b1", "text": "Built an LLM evaluation pipeline."}])

        tailored = resume_tailor.apply_plan(document, plan)

        self.assertEqual([entry["title"] for entry in tailored["sections"][0]["entries"]],
                         ["Founder & AI Product Engineer — Watheq"])

    def test_skills_survive_tailoring_even_though_no_bullet_cites_them(self):
        document = parse_resume(MASTER)

        tailored = resume_tailor.apply_plan(document, _plan())
        skills = [section for section in tailored["sections"] if section["heading"] == "SKILLS"][0]

        self.assertTrue(skills["lines"])

    def test_stopword_gaps_are_discarded_and_concrete_ones_kept(self):
        document = parse_resume(MASTER)
        plan = _plan(gaps=[
            "about, accuracy, acknowledge, address, advantage",
            "The resume does not evidence PyTorch or TensorFlow, both named in the job description.",
        ])

        gaps = resume_tailor.clean_gaps(plan, document, {})

        self.assertEqual(len(gaps), 1)
        self.assertIn("PyTorch", gaps[0])

    def test_a_draft_that_keeps_almost_nothing_is_rejected_as_too_thin(self):
        document = parse_resume(MASTER)
        plan = _plan(bullets=[{"ref": "b1", "text": "Built an LLM evaluation pipeline."}])

        failures = resume_tailor.substance_failures(plan, document)

        self.assertTrue(any("only 1 bullets" in failure for failure in failures))
        self.assertTrue(any("experience bullets" in failure for failure in failures))

    def test_an_empty_draft_never_reaches_the_renderer(self):
        document = parse_resume(MASTER)
        snapshot = {"title": "ML Engineer", "company": "Jobgether", "jd": {"text": "Python."}}

        with mock.patch.object(resume_tailor, "call_model", return_value=_plan(bullets=[])):
            with self.assertRaisesRegex(ValueError, "evidence check"):
                resume_tailor.tailor(document, snapshot, "key", source_refs(document), MASTER)

    def test_tailoring_retries_once_then_gives_up_on_bad_evidence(self):
        document = parse_resume(MASTER)
        bad = _plan(bullets=[{"ref": "b4", "text": "Cut expired-unit waste 95% year-over-year."}])
        snapshot = {"title": "Machine Learning Engineer", "company": "Jobgether", "jd": {"text": "Python."}}

        with mock.patch.object(resume_tailor, "call_model", return_value=bad) as call:
            with self.assertRaisesRegex(ValueError, "evidence check"):
                resume_tailor.tailor(document, snapshot, "key", source_refs(document), MASTER)

        self.assertEqual(call.call_count, 2)
        self.assertIn("unsupported hard fact 95", call.call_args_list[1].args[0])


class TestRunnerDelivery(unittest.TestCase):
    def setUp(self):
        # The fixture plan cites the small MASTER above, so the runner is
        # pointed at it rather than at the repository's real resume.
        patch = mock.patch.object(interactive_runner, "_master_document",
                                  return_value=(parse_resume(MASTER), MASTER))
        patch.start()
        self.addCleanup(patch.stop)

    def _snapshot(self):
        return make_snapshot(JOB, "2026-08-03T12:00:00Z")

    def test_gaps_reach_the_message_and_never_the_pdf(self):
        from pypdf import PdfReader

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        text = PdfReader(BytesIO(base64.b64decode(result["pdf_base64"]))).pages[0].extract_text()

        self.assertIn("PyTorch", result["content"])
        self.assertNotIn("PyTorch", text)
        self.assertNotIn("Gaps to verify", text)
        self.assertTrue(result["tailored"])

    def test_a_model_failure_still_delivers_the_master_resume_with_a_reason(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", side_effect=ValueError("Gemini HTTP 503")):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["pdf_base64"])
        self.assertFalse(result["tailored"])
        self.assertIn("untailored master resume", result["content"])

    def test_a_missing_api_key_degrades_instead_of_failing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertEqual(result["status"], "completed")
        self.assertIn("GEMINI_API_KEY", result["content"])

    def test_the_callback_no_longer_claims_pdf_generation_is_unbuilt(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertNotIn("implementation slice", result["content"])
        self.assertLessEqual(len(result["content"]), interactive_runner.MESSAGE_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
