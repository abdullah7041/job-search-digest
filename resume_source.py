"""Evidence helpers shared by resume ingestion and on-demand tailoring.

A tailoring model is allowed to rephrase a bullet for the job's own language.
It is not allowed to invent. These checks are what make that distinction
enforceable: every hard number must survive verbatim from the exact bullet it
claims to rewrite, and every proper noun must already appear somewhere in the
master resume or in the target job title.
"""

from __future__ import annotations

import re


HARD_FACT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./&'’_-]*")
SENTENCE_START_RE = re.compile(r"(?:^|[.;:!?]\s+|\s[-–—]\s)$")

# Ordinary capitalised words that carry no factual claim, so a model using one
# mid-sentence is a style choice rather than a fabricated credential.
NEUTRAL_TERMS = {
    "i", "a", "an", "the", "and", "or", "for", "with", "arabic", "english",
    "saudi", "arabia", "riyadh", "senior", "junior", "lead", "engineer",
    "engineering", "developer", "analyst", "manager", "product", "data",
    "software", "machine", "learning", "artificial", "intelligence", "science",
    "scientist", "full", "stack", "backend", "frontend", "cloud", "systems",
}


def source_blocks(resume_markdown: str) -> dict[str, str]:
    """Give each non-empty source line a stable reference for model citations."""
    blocks: dict[str, str] = {}
    for number, line in enumerate(resume_markdown.splitlines(), start=1):
        text = line.strip(" -\t")
        if text:
            blocks[f"line-{number}"] = text
    return blocks


def _normalise(text: str) -> str:
    """Lowercase and drop thousands separators so 28,000 matches 28000."""
    return re.sub(r"(?<=\d),(?=\d)", "", (text or "").lower())


def vocabulary(*texts: str) -> set[str]:
    """Every word the model is permitted to treat as established fact."""
    words: set[str] = set()
    for text in texts:
        words.update(word.lower() for word in WORD_RE.findall(_normalise(text)))
    return words


def unsupported_facts(text: str, evidence: str) -> list[str]:
    """Numbers in a rewritten bullet that its cited source does not contain."""
    haystack = _normalise(evidence)
    missing = []
    for fact in HARD_FACT_RE.findall(_normalise(text)):
        if fact.rstrip("%") and fact not in haystack:
            missing.append(fact)
    return missing


def unsupported_terms(text: str, allowed: set[str]) -> list[str]:
    """Proper nouns and acronyms with no basis in the master resume.

    Only terms that look like named things are checked — an acronym, a
    CamelCase product name, or a capitalised word used mid-sentence. That is
    where an invented framework or cloud platform would show up, and it avoids
    flagging the ordinary capital letter that opens a sentence.
    """
    found = []
    for match in WORD_RE.finditer(text):
        word = match.group(0)
        preceding = text[: match.start()]
        if SENTENCE_START_RE.search(preceding) or not preceding.strip():
            continue
        looks_named = (
            (word.isupper() and len(word) >= 2)
            or re.search(r"[a-z][A-Z]", word) is not None
            or word[:1].isupper()
        )
        key = word.lower().strip(".,")
        if looks_named and key not in allowed and key not in NEUTRAL_TERMS:
            found.append(word)
    return found


def validate_tailored_resume(tailored: dict, sources: dict[str, str],
                             allowed_terms: set[str] | None = None,
                             master_text: str | None = None) -> list[str]:
    """Return user-visible evidence failures; an empty list permits rendering."""
    errors: list[str] = []
    for index, bullet in enumerate(tailored.get("bullets", []), start=1):
        refs = bullet.get("source_refs") or ([bullet["ref"]] if bullet.get("ref") else [])
        if not refs:
            errors.append(f"Bullet {index} has no source reference.")
            continue
        unknown = [ref for ref in refs if ref not in sources]
        if unknown:
            errors.append(f"Bullet {index} cites unknown source reference {unknown[0]}.")
            continue
        evidence = " ".join(sources[ref] for ref in refs)
        text = bullet.get("text", "")
        for fact in unsupported_facts(text, evidence):
            errors.append(f"Bullet {index} contains unsupported hard fact {fact}.")
        if allowed_terms is not None:
            for term in unsupported_terms(text, allowed_terms):
                errors.append(f"Bullet {index} claims unsupported term {term}.")

    summary = tailored.get("summary", "")
    if summary and allowed_terms is not None:
        # The summary draws on the whole resume, not one bullet, so it is
        # checked against the full master text rather than a single source.
        joined = master_text if master_text is not None else " ".join(sources.values())
        for fact in unsupported_facts(summary, joined):
            errors.append(f"The summary contains unsupported hard fact {fact}.")
        for term in unsupported_terms(summary, allowed_terms):
            errors.append(f"The summary claims unsupported term {term}.")
    return errors
