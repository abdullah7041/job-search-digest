#!/usr/bin/env python3
"""Run one HMAC-authenticated interactive task from the Discord Worker.

This first workflow slice validates the immutable full-JD handoff. PDF
generation remains a later stage, so the callback says exactly what completed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from io import BytesIO
from urllib.parse import quote
from urllib.parse import urlsplit

import requests

from job_archive import jd_completeness_failures
from resume_source import source_blocks


SIGNATURE_HEADER = "X-Job-Digest-Signature"


def _tailored_pdf(snapshot: dict, resume_path: str = "resume.md") -> tuple[str, str, str]:
    """Create a conservative, source-only two-page PDF for one captured JD."""
    if not os.path.exists(resume_path):
        raise ValueError("No confirmed master resume is available. Use /resume first.")
    resume = open(resume_path, encoding="utf-8").read().strip()
    if not resume:
        raise ValueError("The confirmed master resume is empty.")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    jd_words = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", snapshot["jd"]["text"])}
    evidence = source_blocks(resume)
    selected = [line for line in evidence.values() if jd_words.intersection(re.findall(r"[A-Za-z]{3,}", line.lower()))]
    selected = selected[:18] or list(evidence.values())[:18]
    title = snapshot.get("title", "Target Role")
    company = snapshot.get("company", "Employer")
    story = [Paragraph("<b>Tailored Resume — %s</b>" % title, getSampleStyleSheet()["Title"]),
             Paragraph("Target: %s. Evidence-preserving version generated from the confirmed master resume." % company, getSampleStyleSheet()["BodyText"]), Spacer(1, 8)]
    style = getSampleStyleSheet()["BodyText"]
    for line in selected:
        story.append(Paragraph("• " + line.replace("&", "&amp;"), style))
    gaps = [word for word in sorted(jd_words) if not any(word in line.lower() for line in evidence.values())][:8]
    story.append(Spacer(1, 8)); story.append(Paragraph("<b>Gaps to verify:</b> " + (", ".join(gaps) if gaps else "No clear unsupported keyword gaps identified."), style))
    output = BytesIO(); SimpleDocTemplate(output, pagesize=A4, topMargin=36, bottomMargin=36).build(story)
    from pypdf import PdfReader
    if len(PdfReader(BytesIO(output.getvalue())).pages) > 2:
        raise ValueError("Tailored resume exceeded the two-page limit.")
    return base64.b64encode(output.getvalue()).decode("ascii"), "tailored-resume.pdf", "; ".join(gaps)


def _encoded_result(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    canonical = f"{timestamp}\n{nonce}\n{method}\n{path}\n".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def run_task(task: dict) -> dict:
    """Validate the exact archived JD before any later tailoring can run."""
    snapshot = task.get("input", {}).get("job_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("The task has no immutable job snapshot.")
    jd = snapshot.get("jd") or {}
    jd_text = jd.get("text") or ""
    if jd.get("status") != "captured" or not jd_text.strip():
        raise ValueError("A full job description is required before tailoring.")
    actual_digest = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    if not jd.get("sha256") or not hmac.compare_digest(actual_digest, jd["sha256"]):
        raise ValueError("The job description no longer matches its archived digest.")
    if jd_completeness_failures(jd_text):
        raise ValueError("The snapshot does not contain a complete job description.")
    if not snapshot.get("job_key"):
        raise ValueError("The job snapshot has no stable key.")

    title = snapshot.get("title") or "the selected role"
    company = snapshot.get("company") or "the employer"
    pdf_base64, filename, gaps = _tailored_pdf(snapshot)
    return {
        "status": "completed",
        "content": (
            f"Verified the full job description for **{title}** at **{company}**. "
            "The private tailoring workflow is connected; PDF generation is the next implementation slice."
        ),
        "job_key": snapshot["job_key"],
        "jd_sha256": actual_digest,
        "pdf_base64": pdf_base64,
        "filename": filename,
        "gaps": gaps,
    }


def _request(method: str, url: str, secret: str, body: bytes = b"") -> requests.Response:
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    path = urlsplit(url).path
    response = requests.request(
        method,
        url,
        data=body if method != "GET" else None,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            SIGNATURE_HEADER: _signature(secret, timestamp, nonce, method, path, body),
            "X-Job-Digest-Timestamp": timestamp,
            "X-Job-Digest-Nonce": nonce,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response


def process_task(worker_url: str, secret: str, task_id: str) -> bool:
    """Fetch, run, and report one task. Failures are reported before exit."""
    root = worker_url.rstrip("/")
    safe_task_id = quote(task_id, safe="")
    input_url = f"{root}/internal/tasks/{safe_task_id}/input"
    result_url = f"{root}/internal/tasks/{safe_task_id}/result"

    try:
        task = _request("GET", input_url, secret).json()
        result = run_task(task)
    except Exception as error:
        result = {
            "status": "failed",
            "content": f"Interactive task failed safely: {str(error)[:1000]}",
        }

    body = _encoded_result(result)
    _request("POST", result_url, secret, body)
    return result["status"] == "completed"


def main() -> int:
    worker_url = os.environ.get("INTERACTIVE_WORKER_URL", "")
    secret = os.environ.get("WORKER_CALLBACK_SECRET", "")
    task_id = os.environ.get("TASK_ID", "")
    missing = [
        name
        for name, value in (
            ("INTERACTIVE_WORKER_URL", worker_url),
            ("WORKER_CALLBACK_SECRET", secret),
            ("TASK_ID", task_id),
        )
        if not value
    ]
    if missing:
        print(f"Missing required configuration: {', '.join(missing)}", file=sys.stderr)
        return 2
    return 0 if process_task(worker_url, secret, task_id) else 1


if __name__ == "__main__":
    raise SystemExit(main())
