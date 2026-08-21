#!/usr/bin/env python3
"""Run one HMAC-authenticated interactive task from the Discord Worker.

The task verifies the immutable full-JD handoff, tailors the confirmed master
resume against that job description, and returns a one-page PDF. Gap analysis
travels in the message rather than the document, so nothing that is meant for
the candidate can be sent to an employer by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
from urllib.parse import quote

import requests

from job_archive import GRADE_TEASER, jd_completeness_failures, snapshot_grade
from resume_render import parse_resume, render_pdf, source_refs
from worker_client import signed_request


MESSAGE_LIMIT = 1900
FILENAME_PART = 40
FALLBACK_FILENAME = "Tailored_Resume.pdf"


def _master_document(resume_path: str = "resume.md") -> tuple[dict, str]:
    if not os.path.exists(resume_path):
        raise ValueError("No confirmed master resume is available. Use /resume first.")
    with open(resume_path, encoding="utf-8") as handle:
        resume = handle.read().strip()
    if not resume:
        raise ValueError("The confirmed master resume is empty.")
    return parse_resume(resume), resume


def _tailored_pdf(snapshot: dict, resume_path: str = "resume.md") -> tuple[str, list[str], list[str], str, str]:
    """Tailor the master resume to one captured JD and render it to one page.

    Returns the PDF, the gaps, the fit notes, a note explaining any degradation,
    and the candidate's name as the master resume gives it. A model or evidence
    failure never costs the user a document: it costs them the tailoring, and it
    says so plainly.
    """
    document, resume = _master_document(resume_path)
    name = document["name"]
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return base64.b64encode(render_pdf(document)).decode("ascii"), [], [], \
            "OPENROUTER_API_KEY is not configured, so this is the untailored master resume.", name

    from resume_tailor import tailor

    try:
        tailored, gaps, fit_notes = tailor(document, snapshot, api_key,
                                           source_refs(document), resume)
    except (ValueError, KeyError, TypeError, requests.RequestException) as error:
        fresh, _ = _master_document(resume_path)
        return base64.b64encode(render_pdf(fresh)).decode("ascii"), [], [], \
            f"Tailoring did not pass its own checks, so this is the untailored master resume. Reason: {str(error)[:300]}", name
    return base64.b64encode(render_pdf(tailored)).decode("ascii"), gaps, fit_notes, "", name


def _slug(value: str, titlecase: bool = False) -> str:
    """A filename-safe fragment of a name or job title.

    Truncation is by whole words. Cutting at a character limit used to land
    mid-word and, worse, on a separator — which is where the trailing hyphen in
    ...software-engineer-ii-.pdf came from.

    A job title keeps the case the employer wrote it in, so ML stays ML and
    QuantumBlack stays QuantumBlack; only an all-lowercase word is capitalised.
    A name is title-cased, because a resume heading is usually shouted.
    """
    words = [word for word in re.split(r"[^A-Za-z0-9]+", value or "") if word]
    kept: list[str] = []
    length = 0
    for word in words:
        word = word.capitalize() if titlecase or word.islower() else word
        cost = len(word) + (1 if kept else 0)
        if kept and length + cost > FILENAME_PART:
            break
        length += cost
        kept.append(word)
    return "_".join(kept)


def _filename(name: str, title: str) -> str:
    """Name the PDF after the candidate and the role it was tailored for.

    This is what a recruiter sees in their inbox, so the candidate's name comes
    first and the role second — Abdullah_Bin_Ahmed_Senior_Software_Engineer.pdf.
    The name is read from the master resume rather than hardcoded, so the file
    follows the resume if the heading ever changes.
    """
    parts = [part for part in (_slug(name, titlecase=True), _slug(title)) if part]
    return f"{'_'.join(parts)}.pdf" if parts else FALLBACK_FILENAME


TEASER_WARNING = (
    "This posting only published a short teaser, not a full job description, so "
    "the CV was tailored against a summary. Open the posting before you send it."
)


def _message(title: str, company: str, gaps: list[str], fit_notes: list[str],
             note: str, warnings: list[str] | None = None) -> str:
    """Everything the candidate needs to read, and nothing an employer sees."""
    lines = [f"Tailored one-page CV for **{title}** at **{company}**, built from your confirmed master resume."]
    if note:
        lines.append(f"\n:warning: {note}")
    for warning in warnings or []:
        lines.append(f"\n:warning: {warning}")
    if fit_notes:
        lines.append("\n**Why you fit**")
        lines.extend(f"- {note}" for note in fit_notes)
    if gaps:
        lines.append("\n**Gaps to expect in the interview**")
        lines.extend(f"- {gap}" for gap in gaps)
    if gaps or fit_notes:
        lines.append("\nThese notes are not in the PDF. The attachment is ready to send.")
    return "\n".join(lines)[:MESSAGE_LIMIT]


def _encoded_result(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
    # A teaser clears the completeness gate above, so it is tailorable — but the
    # candidate has to know the CV was aimed at a summary rather than at the
    # requirements. This is a warning, not a degradation: `tailored` stays true
    # because the tailoring itself worked.
    warnings = [TEASER_WARNING] if snapshot_grade(snapshot) == GRADE_TEASER else []
    pdf_base64, gaps, fit_notes, note, name = _tailored_pdf(snapshot)
    return {
        "status": "completed",
        "content": _message(title, company, gaps, fit_notes, note, warnings),
        "job_key": snapshot["job_key"],
        "jd_sha256": actual_digest,
        "pdf_base64": pdf_base64,
        "filename": _filename(name, title),
        "gaps": "; ".join(gaps),
        "tailored": not note,
    }


def process_task(worker_url: str, secret: str, task_id: str) -> bool:
    """Fetch, run, and report one task. Failures are reported before exit."""
    root = worker_url.rstrip("/")
    safe_task_id = quote(task_id, safe="")
    input_url = f"{root}/internal/tasks/{safe_task_id}/input"
    result_url = f"{root}/internal/tasks/{safe_task_id}/result"

    try:
        task = signed_request("GET", input_url, secret).json()
        result = run_task(task)
    except Exception as error:
        result = {
            "status": "failed",
            "content": f"Interactive task failed safely: {str(error)[:1000]}",
        }

    body = _encoded_result(result)
    signed_request("POST", result_url, secret, body)
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
