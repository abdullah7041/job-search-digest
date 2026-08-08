"""Normalize user-approved resume text before it becomes the master source."""

from __future__ import annotations

import hashlib
import re


def prepare_resume_preview(raw_text: str, filename: str) -> dict:
    """Create a human-reviewable Markdown preview without retaining the PDF."""
    readable = raw_text.strip()
    if len(readable) < 40:
        raise ValueError("The uploaded PDF contains too little readable text to review.")
    markdown = re.sub(r"\n{3,}", "\n\n", readable)
    if not markdown.startswith("#"):
        first, _, rest = markdown.partition("\n")
        markdown = f"# {first.strip()}\n{rest}".rstrip() + "\n"
    else:
        markdown = markdown.rstrip() + "\n"
    return {
        "markdown": markdown,
        "metadata": {
            "schema_version": 1,
            "filename": filename,
            "source_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        },
        "warnings": [],
    }
