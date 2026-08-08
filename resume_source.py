"""Evidence helpers shared by resume ingestion and on-demand tailoring."""

from __future__ import annotations

import re


HARD_FACT_RE = re.compile(r"\b\d+(?:\.\d+)?(?:%|\b)")


def source_blocks(resume_markdown: str) -> dict[str, str]:
    """Give each non-empty source line a stable reference for model citations."""
    blocks: dict[str, str] = {}
    for number, line in enumerate(resume_markdown.splitlines(), start=1):
        text = line.strip(" -\t")
        if text:
            blocks[f"line-{number}"] = text
    return blocks


def validate_tailored_resume(tailored: dict, sources: dict[str, str]) -> list[str]:
    """Return user-visible evidence failures; an empty list permits rendering."""
    errors: list[str] = []
    for index, bullet in enumerate(tailored.get("bullets", []), start=1):
        refs = bullet.get("source_refs") or []
        if not refs:
            errors.append(f"Bullet {index} has no source reference.")
            continue
        unknown = [ref for ref in refs if ref not in sources]
        if unknown:
            errors.append(f"Bullet {index} cites unknown source reference {unknown[0]}.")
            continue
        evidence = " ".join(sources[ref] for ref in refs).lower()
        for fact in HARD_FACT_RE.findall(bullet.get("text", "")):
            if fact.lower() not in evidence:
                errors.append(f"Bullet {index} contains unsupported hard fact {fact}.")
    return errors
