#!/usr/bin/env python3
"""Offline contract tests for the interactive tailoring pipeline."""

import datetime
import hashlib
import hmac
import importlib
import importlib.util
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest import mock

import interactive_runner
import openrouter
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

        # The candidate's name comes from the master resume, not from a constant,
        # so the recruiter's inbox shows who sent it before what it is for.
        self.assertEqual(result["filename"],
                         "Abdullah_Bin_Ahmed_Senior_Machine_Learning_Engineer.pdf")
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


class TestWorkerClient(unittest.TestCase):
    def test_signed_request_uses_the_existing_hmac_canonical_form(self):
        """Catches a client that signs a different path/body than the Worker verifies."""
        spec = importlib.util.find_spec("worker_client")
        self.assertIsNotNone(spec, "worker_client must provide signed_request for shared Worker calls")
        signed_request = importlib.import_module("worker_client").signed_request
        secret = "callback-secret"
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                timestamp = self.headers.get("X-Job-Digest-Timestamp", "")
                nonce = self.headers.get("X-Job-Digest-Nonce", "")
                canonical = f"{timestamp}\n{nonce}\nGET\n{self.path}\n".encode()
                expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
                received["signed"] = hmac.compare_digest(
                    self.headers.get("X-Job-Digest-Signature", ""), expected
                )
                body = b'{}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = signed_request(
                "GET", f"http://127.0.0.1:{server.server_port}/internal/confirmed-resume", secret
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(received["signed"])


class TestInteractiveWorkflowWiring(unittest.TestCase):
    """The runner's tailoring silently degrades to an untailored resume when
    OPENROUTER_API_KEY is absent, so the workflow must supply it."""

    WORKFLOW = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "interactive.yml"
    )

    def _workflow_text(self):
        with open(self.WORKFLOW, encoding="utf-8") as handle:
            return handle.read()

    def test_workflow_passes_the_openrouter_key_to_the_runner(self):
        self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", self._workflow_text())

    def test_workflow_makes_the_model_override_reachable(self):
        """TAILOR_MODEL was an env override the workflow never passed, so the
        model name was effectively hardcoded and a bad one could not be
        retuned without a commit."""
        text = self._workflow_text()
        self.assertIn("TAILOR_MODEL: ${{ vars.TAILOR_MODEL }}", text)
        self.assertIn("TAILOR_FALLBACK_MODELS: ${{ vars.TAILOR_FALLBACK_MODELS }}", text)

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
        "headline": "AI Product Engineer — Senior Machine Learning Engineer",
        "summary": "AI Product Engineer with 5+ years across engineering, shipping production LLM systems.",
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
        self.assertEqual(tailored["headline"], "AI Product Engineer — Senior Machine Learning Engineer")

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


class TestTailoringTransport(unittest.TestCase):
    """The transport around the model, which is what actually broke in use.

    A 404 on the model name and a 429 on the quota each ended the whole attempt
    outright, because the call had one model, no retry, and no time budget.
    """

    def _response(self, status, payload=None, headers=None):
        response = mock.Mock()
        response.status_code = status
        response.headers = headers or {}
        response.text = json.dumps(payload or {})
        response.json.return_value = payload or {}
        return response

    def _plan_payload(self):
        return {"choices": [{"message": {"content": json.dumps(_plan())}, "finish_reason": "stop"}]}

    def _deadline(self, seconds=200.0):
        return time.monotonic() + seconds

    def test_the_whole_chain_is_offered_to_openrouter_in_one_request(self):
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(200, self._plan_payload())) as post:
            resume_tailor.call_model("prompt", "key", self._deadline())

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["models"][0], resume_tailor.TAILOR_MODEL)
        self.assertEqual(body["models"][1:], resume_tailor.TAILOR_FALLBACKS)
        self.assertGreater(len(body["models"]), 1)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer key")

    def test_temperature_is_never_sent_so_discounted_endpoints_stay_eligible(self):
        """require_parameters drops any provider missing a parameter the request
        names. Google serves Gemini Flash on Vertex endpoints that accept seed
        but not temperature, and those are the discounted ones — naming
        temperature routes around them to buy a knob seed already provides."""
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(200, self._plan_payload())) as post:
            resume_tailor.call_model("prompt", "key", self._deadline())

        body = post.call_args.kwargs["json"]
        self.assertNotIn("temperature", body)
        self.assertEqual(body["seed"], 7)

    def test_the_resume_is_never_routed_to_a_provider_that_retains_prompts(self):
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(200, self._plan_payload())) as post:
            resume_tailor.call_model("prompt", "key", self._deadline())

        provider = post.call_args.kwargs["json"]["provider"]
        self.assertEqual(provider["data_collection"], "deny")
        # Without this, an endpoint that ignores the schema could serve the call
        # and answer in prose, which no amount of retrying would fix.
        self.assertTrue(provider["require_parameters"])

    def test_a_rate_limited_call_is_retried_rather_than_abandoned(self):
        responses = [self._response(429, {"error": "rate limited"}),
                     self._response(200, self._plan_payload())]
        with mock.patch.object(openrouter.requests, "post", side_effect=responses) as post, \
                mock.patch.object(openrouter.time, "sleep") as sleep:
            plan = resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(plan["headline"], _plan()["headline"])

    def test_a_retry_honours_the_wait_the_server_asked_for(self):
        responses = [self._response(429, {}, {"Retry-After": "4"}),
                     self._response(200, self._plan_payload())]
        with mock.patch.object(openrouter.requests, "post", side_effect=responses), \
                mock.patch.object(openrouter.time, "sleep") as sleep:
            resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertEqual(sleep.call_args.args[0], 4.0)

    def test_an_unretryable_status_fails_immediately(self):
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(401, {})) as post:
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertEqual(post.call_count, 1)
        self.assertIn("401", str(raised.exception))

    def test_a_failure_names_the_model_so_discord_can_report_it(self):
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(404, {})):
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertIn(resume_tailor.TAILOR_MODEL, str(raised.exception))

    def test_a_backoff_that_would_overrun_the_budget_is_not_taken(self):
        """A killed workflow posts no result at all, leaving the user watching
        'Preparing your tailored CV privately...' forever. Sleeping past the
        budget to buy one more attempt trades a reported failure for silence."""
        # Room for one call, but not for the backoff plus a second one.
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(503, {})) as post, \
                mock.patch.object(openrouter.time, "sleep") as sleep:
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", time.monotonic() + 16)

        self.assertEqual(post.call_count, 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertIn("503", str(raised.exception))

    def test_an_exhausted_budget_says_so_instead_of_calling_anyway(self):
        with mock.patch.object(openrouter.requests, "post") as post:
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", time.monotonic() - 1)

        self.assertEqual(post.call_count, 0)
        self.assertIn("time budget", str(raised.exception))

    def test_an_error_returned_with_a_200_is_not_read_as_an_answer(self):
        payload = {"error": {"message": "no endpoints found"}}
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(200, payload)):
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertIn("no endpoints found", str(raised.exception))

    def test_a_truncated_answer_names_its_finish_reason(self):
        payload = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        with mock.patch.object(openrouter.requests, "post",
                               return_value=self._response(200, payload)):
            with self.assertRaises(ValueError) as raised:
                resume_tailor.call_model("prompt", "key", self._deadline())

        self.assertIn("length", str(raised.exception))

    def test_an_unset_workflow_variable_does_not_erase_the_default_model(self):
        """GitHub renders an unset ${{ vars.X }} as an empty string, which the
        two-argument os.environ.get would have handed straight to the API."""
        try:
            with mock.patch.dict(os.environ, {"TAILOR_MODEL": "", "TAILOR_FALLBACK_MODELS": ""}):
                importlib.reload(resume_tailor)
                self.assertEqual(resume_tailor.TAILOR_MODEL, resume_tailor.DEFAULT_MODEL)
                self.assertTrue(resume_tailor.TAILOR_FALLBACKS)
        finally:
            importlib.reload(resume_tailor)


class TestTailoringPrompt(unittest.TestCase):
    def _prompt(self):
        return resume_tailor.build_prompt(parse_resume(MASTER), {"title": "ML Engineer"})

    def test_the_prompt_no_longer_asks_for_words_the_checker_rejects(self):
        """Rule 2 used to invite the job's vocabulary while the validator allows
        only the resume's own, so the model was told to produce exactly what
        would be thrown out — burning both attempts on every click."""
        prompt = self._prompt()

        self.assertNotIn("use the job's vocabulary", prompt)
        self.assertIn("Every word you write must already appear in this resume", prompt)

    def test_the_word_rule_still_leaves_room_for_the_headline_rule(self):
        """`allowed` is vocabulary(master, title, company), and rule 4 needs the
        job title for the headline. A word rule that forbade everything outside
        the resume would read as banning what rule 4 asks for."""
        prompt = self._prompt()

        self.assertIn("in the target role's title, or in the employer's name", prompt)
        self.assertIn("the candidate's current title, then an em dash, then the job's title", prompt)

    def test_the_summary_rule_asks_for_a_rewrite_rather_than_permitting_a_copy(self):
        """The KAUST CV's summary was the master's summary, word for word. The
        old rule only forbade inventing facts; nothing asked the model to aim
        the summary at the job, so it took the safe path every time."""
        prompt = self._prompt()

        self.assertIn("Rewrite the summary for this job", prompt)
        self.assertIn("Returning the existing summary unchanged is a failure", prompt)

    def test_skills_lines_are_asked_for_and_bounded(self):
        """The skills block shipped identically on every job, because nothing
        ever rewrote it. Now it is requested, and the request names the one
        thing the evidence check will not forgive."""
        prompt = self._prompt()

        self.assertIn("Return every line from the SKILLS section", prompt)
        self.assertIn("You may not add an item, a word, or a group", prompt)

    def test_skills_lines_do_not_count_towards_the_achievement_target(self):
        """Five skills lines would otherwise satisfy a bullet target meant to
        prove the career survived selection."""
        prompt = self._prompt()

        self.assertIn("Skills lines are not achievements and do not count", prompt)

    def test_the_bullet_target_reflects_what_the_resume_actually_offers(self):
        """'Roughly 16' is unreachable on a short resume, so it read as 'keep
        everything' and left selection with nothing to select."""
        document = parse_resume(MASTER)
        available = sum(len(entry["bullets"]) for section in document["sections"]
                        for entry in section["entries"])
        available += sum(len(section["lines"]) for section in document["sections"]
                         if section["heading"] in ("EXPERIENCE", "PROJECTS"))

        prompt = self._prompt()

        self.assertIn("offers {0} achievement bullets".format(available), prompt)
        self.assertNotIn("roughly 16 bullets", prompt)


# The master's real shape: several skills categories, and a LANGUAGES section
# whose only content is a plain line rather than a bullet.
MASTER_FULL = MASTER + """- **Backend & Cloud:** Supabase, REST APIs, GitHub Actions CI/CD, Git
- **Frontend:** React 19, Tailwind CSS, Vite, Zustand

## LANGUAGES
Arabic (native) · English (professional)
"""


class TestIntroOnlySection(unittest.TestCase):
    def test_a_section_that_is_only_an_intro_still_reaches_the_pdf(self):
        """LANGUAGES was absent from every PDF this has ever rendered.

        parse_resume files a plain line under "intro", because only a "- " line
        becomes a bullet, and the renderer skipped any section with no entries
        and no lines. So "Arabic (native) . English (professional)" was dropped
        from the master resume itself, not just from a tailored one -- including
        on jobs that ask for English and for working in an international team.
        """
        from pypdf import PdfReader

        text = PdfReader(BytesIO(render_pdf(parse_resume(MASTER_FULL)))).pages[0].extract_text()

        self.assertIn("LANGUAGES", text)
        self.assertIn("Arabic (native)", text)

    def test_a_section_with_nothing_at_all_is_still_skipped(self):
        document = parse_resume(MASTER_FULL)
        document["sections"].append({"heading": "AWARDS", "entries": [], "lines": []})

        from pypdf import PdfReader
        text = PdfReader(BytesIO(render_pdf(document))).pages[0].extract_text()

        self.assertNotIn("AWARDS", text)


class TestSkillsTailoring(unittest.TestCase):
    def _skills(self, document):
        section = [s for s in document["sections"] if s["heading"] == "SKILLS"][0]
        return [line["text"] for line in section["lines"]]

    def test_skills_lines_are_reordered_and_pruned_for_the_job(self):
        """The KAUST CV shipped "Frontend: React 19, Tailwind CSS, Vite,
        Zustand" to a supercomputing lab, because apply_plan rewrote lines only
        under EXPERIENCE and PROJECTS."""
        document = parse_resume(MASTER_FULL)
        refs = {line["ref"]: line["source"]
                for section in document["sections"] if section["heading"] == "SKILLS"
                for line in section["lines"]}
        languages_ref, backend_ref, frontend_ref = list(refs)

        plan = _plan(bullets=_plan()["bullets"] + [
            {"ref": backend_ref, "text": "Backend & Cloud: GitHub Actions CI/CD, Git"},
            {"ref": languages_ref, "text": "Languages: Python, SQL"},
            {"ref": frontend_ref, "text": "Frontend: React 19"},
        ])

        lines = self._skills(resume_tailor.apply_plan(document, plan))

        self.assertEqual(lines[0], "Backend & Cloud: GitHub Actions CI/CD, Git")
        self.assertEqual(lines[1], "Languages: Python, SQL")
        self.assertNotIn("Zustand", " ".join(lines))

    def test_every_skills_category_survives_a_partial_plan(self):
        """The model choosing which line leads is tailoring. The model deciding
        a category does not exist would present a narrower engineer than the
        master resume does, so a line it ignored keeps its master wording."""
        document = parse_resume(MASTER_FULL)
        backend_ref = [line["ref"] for section in document["sections"]
                       if section["heading"] == "SKILLS" for line in section["lines"]][1]
        plan = _plan(bullets=_plan()["bullets"]
                     + [{"ref": backend_ref, "text": "Backend & Cloud: GitHub Actions CI/CD"}])

        lines = self._skills(resume_tailor.apply_plan(document, plan))

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Backend & Cloud: GitHub Actions CI/CD")
        self.assertIn("Zustand", " ".join(lines))

    def test_a_skills_line_may_not_gain_a_single_word(self):
        """Resume-wide vocabulary is too loose for a skills line: a term from
        the experience section would pass it, while reading to a recruiter as a
        skill the candidate listed."""
        sources = {"s1": "Languages: Python, TypeScript, SQL"}
        checked = {"bullets": [{"text": "Languages: Python, SQL, PyTorch", "source_refs": ["s1"]}]}

        errors = validate_tailored_resume(checked, sources, vocabulary(MASTER_FULL),
                                          MASTER_FULL, subset_refs={"s1"})

        self.assertTrue(any("pytorch" in error.lower() for error in errors))

    def test_dropping_and_reordering_a_skills_line_is_allowed(self):
        sources = {"s1": "Languages: Python, TypeScript, SQL"}
        checked = {"bullets": [{"text": "Languages: SQL, Python", "source_refs": ["s1"]}]}

        errors = validate_tailored_resume(checked, sources, vocabulary(MASTER_FULL),
                                          MASTER_FULL, subset_refs={"s1"})

        self.assertEqual(errors, [])

    def test_a_skills_line_that_is_not_named_keeps_the_ordinary_rule(self):
        """Only skills lines are held to the subtractive rule; an experience
        bullet may still be rephrased into the job's own framing."""
        sources = {"b1": "Built an LLM evaluation pipeline that judge-scores prompt variants."}
        checked = {"bullets": [{"text": "Built an evaluation pipeline for prompt variants.",
                                "source_refs": ["b1"]}]}

        self.assertEqual(
            validate_tailored_resume(checked, sources, vocabulary(MASTER_FULL), MASTER_FULL), [])


class TestHeadlineAndSummaryGuards(unittest.TestCase):
    def _terms(self):
        return vocabulary("AI Product Engineer", "AI/ML Support Analyst")

    def test_a_headline_word_from_neither_source_is_rejected(self):
        """plan["headline"] went into the document unchecked, so rule 4 was a
        request rather than a constraint."""
        errors = validate_tailored_resume(
            {"headline": "Senior HPC Engineer", "bullets": []}, {},
            headline_terms=self._terms(),
            expected_headline="AI Product Engineer — AI/ML Support Analyst")

        self.assertTrue(any("hpc" in error.lower() for error in errors))

    def test_the_current_title_joined_to_the_job_title_passes(self):
        errors = validate_tailored_resume(
            {"headline": "AI Product Engineer — AI/ML Support Analyst", "bullets": []}, {},
            headline_terms=self._terms())

        self.assertEqual(errors, [])

    def test_a_summary_longer_than_three_sentences_is_rejected(self):
        """"Three sentences at most" was a prompt rule with nothing behind it."""
        summary = ("Python engineer. Builds evaluation pipelines. "
                   "Ships production systems. Writes documentation.")

        errors = validate_tailored_resume(
            {"summary": summary, "bullets": []}, {},
            allowed_terms=vocabulary(MASTER_FULL), master_text=MASTER_FULL)

        self.assertTrue(any("sentences" in error for error in errors))

    def test_an_incomplete_headline_is_rejected_even_when_its_words_are_supported(self):
        errors = validate_tailored_resume(
            {"headline": "AI Product Engineer", "bullets": []}, {},
            headline_terms=self._terms(),
            expected_headline="AI Product Engineer — AI/ML Support Analyst")

        self.assertTrue(any("current title" in error.lower() for error in errors))

    def test_an_unchanged_master_summary_is_rejected(self):
        master_summary = parse_resume(MASTER_FULL)["summary"]

        errors = validate_tailored_resume(
            {"summary": master_summary, "bullets": []}, {},
            allowed_terms=vocabulary(MASTER_FULL), master_text=MASTER_FULL,
            master_summary=master_summary)

        self.assertTrue(any("unchanged" in error.lower() for error in errors))


class TestJdResolution(unittest.TestCase):
    """Where the description a CV is built from is allowed to come from."""

    FULL_JD = JOB["description"]

    def _snapshot(self, **jd):
        snapshot = make_snapshot(dict(JOB, description=""), "2026-08-03T12:00:00Z")
        if jd:
            snapshot["jd"].update(jd)
        return snapshot

    def test_the_archived_description_still_wins_and_is_still_verified(self):
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")

        text, source = interactive_runner.resolve_jd(
            snapshot, {}, time.monotonic() + 60,
            fetch=lambda *a, **k: self.fail("no refetch when the snapshot has one"))

        self.assertEqual(source, interactive_runner.JD_CAPTURED)
        self.assertEqual(text, snapshot["jd"]["text"])

    def test_a_card_the_daily_run_could_not_read_is_fetched_again_now(self):
        """The whole point: the 07:00 run was blocked, the click at 13:00 is
        not, and the candidate never had to type anything."""
        snapshot = self._snapshot()

        text, source = interactive_runner.resolve_jd(
            snapshot, {}, time.monotonic() + 60, fetch=lambda *a, **k: self.FULL_JD)

        self.assertEqual(source, interactive_runner.JD_REFETCHED)
        self.assertEqual(text, self.FULL_JD)

    def test_what_the_candidate_pasted_outranks_fetching(self):
        snapshot = self._snapshot()

        text, source = interactive_runner.resolve_jd(
            snapshot, {"pasted_jd": self.FULL_JD}, time.monotonic() + 60,
            fetch=lambda *a, **k: self.fail("a supplied description is not second-guessed"))

        self.assertEqual(source, interactive_runner.JD_PASTED)

    def test_a_paste_that_is_not_a_full_posting_is_refused_with_the_reason(self):
        with self.assertRaisesRegex(ValueError, "full job description"):
            interactive_runner.resolve_jd(self._snapshot(), {"pasted_jd": "Python role, Riyadh."},
                                          time.monotonic() + 60)

    def test_screenshots_are_the_last_resort_and_are_gated_the_same_way(self):
        snapshot = self._snapshot()

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            text, source = interactive_runner.resolve_jd(
                snapshot, {"images": ["https://cdn.discordapp.test/a.png"]},
                time.monotonic() + 60,
                fetch=lambda *a, **k: "",
                transcribe=lambda *a, **k: self.FULL_JD)

        self.assertEqual(source, interactive_runner.JD_IMAGE)

    def test_when_nothing_works_the_message_says_what_to_do_next(self):
        """The old failure named a command that no longer existed. This one
        names the command that does, and the posting it could not read."""
        with self.assertRaises(ValueError) as raised:
            interactive_runner.resolve_jd(self._snapshot(), {}, time.monotonic() + 60,
                                          fetch=lambda *a, **k: "")

        message = str(raised.exception)
        self.assertIn("/tailor-jd", message)
        self.assertIn(JOB["url"], message)

    def test_a_tampered_archived_description_still_fails_closed(self):
        """Recovery must not become a way around the integrity check: a
        snapshot altered after delivery is rejected, never quietly refetched."""
        snapshot = make_snapshot(JOB, "2026-08-03T12:00:00Z")
        snapshot["jd"]["text"] = "A different description"

        with self.assertRaisesRegex(ValueError, "digest"):
            interactive_runner.resolve_jd(snapshot, {}, time.monotonic() + 60,
                                          fetch=lambda *a, **k: self.FULL_JD)

    def test_the_message_says_which_description_the_cv_was_built_from(self):
        """A CV built from screenshots and one built from the archived posting
        are not equally trustworthy, and the candidate is the one deciding
        whether to send it."""
        snapshot = self._snapshot()

        result = run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"},
                          fetch=lambda *a, **k: self.FULL_JD)

        self.assertEqual(result["jd_source"], interactive_runner.JD_REFETCHED)
        self.assertIn("fetched again just now", result["content"])
        self.assertTrue(result["pdf_base64"])


class TestTailoredFilename(unittest.TestCase):
    """Permanent channel messages make one constant filename a real problem."""

    def test_the_pdf_is_named_after_the_job(self):
        self.assertEqual(
            interactive_runner._filename("ABDULLAH BIN AHMED", "Senior AI/ML Engineer"),
            "Abdullah_Bin_Ahmed_Senior_AI_ML_Engineer.pdf",
        )

    def test_an_unnamed_job_still_produces_a_usable_filename(self):
        self.assertEqual(interactive_runner._filename("", ""),
                         interactive_runner.FALLBACK_FILENAME)

    def test_the_employers_own_capitalisation_survives(self):
        """Title-casing every word turned ML into Ml and II into Ii, which is
        not how the employer wrote the role or how a recruiter reads it."""
        self.assertEqual(
            interactive_runner._filename("Abdullah Bin Ahmed",
                                         "Software Engineer II - QuantumBlack"),
            "Abdullah_Bin_Ahmed_Software_Engineer_II_QuantumBlack.pdf",
        )

    def test_a_long_title_is_cut_at_a_word_not_mid_word(self):
        """Truncating at a character limit used to land on a separator, which is
        where the trailing hyphen in ...software-engineer-ii-.pdf came from."""
        name = interactive_runner._filename(
            "Abdullah Bin Ahmed",
            "Senior Staff Principal Distributed Systems Reliability Engineer")

        self.assertTrue(name.endswith(".pdf"))
        self.assertNotIn("_.", name)
        self.assertNotIn("__", name)
        for word in name[:-len(".pdf")].split("_"):
            self.assertIn(word, "Abdullah Bin Ahmed Senior Staff Principal "
                                "Distributed Systems Reliability Engineer")


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

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        text = PdfReader(BytesIO(base64.b64decode(result["pdf_base64"]))).pages[0].extract_text()

        self.assertIn("PyTorch", result["content"])
        self.assertNotIn("PyTorch", text)
        self.assertNotIn("Gaps to verify", text)
        self.assertTrue(result["tailored"])

    def test_a_teaser_is_tailored_but_the_message_says_it_was_a_teaser(self):
        """A 497-character aggregator blurb passes the completeness gate, so the
        button works. The candidate still has to know the CV was aimed at a
        summary rather than at the requirements — and `tailored` stays true,
        because the tailoring itself did not degrade."""
        teaser_job = dict(JOB, description=(
            "Pasqal seeks a Senior Machine Learning Engineer to adapt and deploy "
            "quantum ML methods for client datasets, leveraging around 5 years of "
            "experience in ML. You will build pipelines, collaborate with R&D, and "
            "ensure client-ready solutions with strong maintainability. The position "
            "entails close coordination with hardware teams, domain experts from "
            "partner enterprises, and ongoing scientific monitoring to keep the "
            "company at the cutting edge."))
        snapshot = make_snapshot(teaser_job, "2026-08-20T12:00:00Z")
        self.assertEqual(snapshot["jd"]["status"], "captured")

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": snapshot}, "status": "dispatched"})

        self.assertTrue(result["tailored"])
        self.assertIn("teaser", result["content"])

    def test_a_full_posting_carries_no_teaser_warning(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertNotIn("teaser", result["content"])

    def test_a_model_failure_still_delivers_the_master_resume_with_a_reason(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", side_effect=ValueError("OpenRouter HTTP 503 (openai/gpt-5.6-luna)")):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["pdf_base64"])
        self.assertFalse(result["tailored"])
        self.assertIn("untailored master resume", result["content"])

    def test_a_missing_api_key_degrades_instead_of_failing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertEqual(result["status"], "completed")
        self.assertIn("OPENROUTER_API_KEY", result["content"])

    def test_the_callback_no_longer_claims_pdf_generation_is_unbuilt(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}), \
             mock.patch.object(resume_tailor, "call_model", return_value=_plan()):
            result = run_task({"input": {"job_snapshot": self._snapshot()}, "status": "dispatched"})

        self.assertNotIn("implementation slice", result["content"])
        self.assertLessEqual(len(result["content"]), interactive_runner.MESSAGE_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
