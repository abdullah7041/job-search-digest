"""Evidence helpers shared by resume ingestion and on-demand tailoring.

A tailoring model is allowed to rephrase a bullet for the job's own language.
It is not allowed to invent. These checks are what make that distinction
enforceable: every hard number must survive verbatim from the exact bullet it
claims to rewrite, and every proper noun must already appear somewhere in the
master resume or in the target job title.

Three parts of a tailored document used to reach the PDF with no check at all:

* A skills line, because nothing rewrote one. Now that tailoring reorders and
  prunes them, `subset_failures` holds them to a stricter rule than the rest of
  the document -- a skills line may lose items and reorder them, and may not
  gain a single word. Resume-wide vocabulary is too loose here: it would let a
  term from the experience section drift into the skills list, where it reads
  as a skill the candidate never claimed.
* The headline, which was applied verbatim from the model's answer.
  `headline_failures` holds it to the candidate's own headline plus the target
  job title, which is all rule 4 of the prompt ever permitted.
* The summary's length. "Three sentences at most" was a prompt rule with
  nothing enforcing it.
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


# A quoted snippet shorter than this overlaps almost any source by chance, so
# it is not evidence even when it technically matches.
MIN_SNIPPET_CHARS = 8

# How much of a snippet's vocabulary must appear in its cited source before a
# paraphrase still counts as a citation. Watheq's figure, kept for its reason:
# models rewrite lightly around a real quote, so demanding a verbatim match
# would throw away honest evidence, while a half would accept coincidence.
SNIPPET_OVERLAP = 0.75


def _snippet_text(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def snippet_supported(snippet: str, source: str) -> bool:
    """Does `snippet` actually appear in `source`, verbatim or near enough?

    The mirror of `unsupported_terms`. That asks whether a named thing is absent
    from the resume; this asks whether a quoted snippet was really copied from
    the text it cites. A model asserting a blocker or a strength has to point at
    evidence, and this is what makes the pointing checkable rather than
    decorative.

    Ported from Watheq's `verifyEvidenceSnippet`
    (netlify/lib/strategic-reality-check.js:105), which is the mechanism behind
    its low-ceiling verdict. See plans/018.
    """
    text = _snippet_text(snippet)
    if len(text) < MIN_SNIPPET_CHARS:
        return False
    haystack = _snippet_text(source)
    if not haystack:
        return False
    if text in haystack:
        return True
    words = {word for word in text.split() if len(word) >= 2}
    if not words:
        return False
    known = {word for word in haystack.split() if len(word) >= 2}
    return len(words & known) / len(words) >= SNIPPET_OVERLAP


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


# Grammar words carry no claim, so a rewrite may use them freely. Deliberately
# tiny, and deliberately not NEUTRAL_TERMS: that set exists to stop ordinary
# capitalised words being read as credentials, and it contains real skill words
# like "cloud" and "data" that must not be addable to a skills line.
SUBSET_STOPWORDS = {"a", "an", "and", "the", "or", "of", "for", "in", "on",
                    "with", "to", "using", "via"}

SUMMARY_MAX_SENTENCES = 3
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _tokens(text: str) -> list[str]:
    """Comparable words, with slash-joined pairs also counted as their parts.

    WORD_RE keeps "/" inside a word, so "AI/ML" is one token. That is right for
    matching a title verbatim and wrong for comparing two spellings of the same
    claim: a headline written "AI / ML" would otherwise fail against a job title
    written "AI/ML". Splitting both sides makes the two agree.
    """
    found = []
    for word in WORD_RE.findall(_normalise(text)):
        for part in [word] + (word.split("/") if "/" in word else []):
            key = part.lower().strip(".,")
            if key and key not in found:
                found.append(key)
    return found


def subset_failures(text: str, evidence: str) -> list[str]:
    """Words a rewrite added that its own source line does not contain.

    Subtractive editing only: reorder freely, drop what the job does not ask
    for, add nothing. This is what lets a skills line be tailored at all without
    it becoming a place to quietly grant the candidate a skill.
    """
    known = set(_tokens(evidence))
    return [key for key in _tokens(text)
            if key not in known and key not in SUBSET_STOPWORDS]


def headline_failures(headline: str, allowed: set[str]) -> list[str]:
    """Words in the headline that neither the resume nor the job title supplies.

    Every word is checked, including the first. unsupported_terms deliberately
    skips a sentence's opening word to avoid flagging ordinary capitalisation,
    but a headline is a claim from its first word onward.
    """
    known = set(allowed)
    for word in list(known):
        known.update(part for part in word.split("/") if part)
    return [key for key in _tokens(headline or "")
            if key not in known and key not in SUBSET_STOPWORDS]


def sentence_count(text: str) -> int:
    return len([part for part in SENTENCE_SPLIT_RE.split((text or "").strip()) if part])


def validate_tailored_resume(tailored: dict, sources: dict[str, str],
                             allowed_terms: set[str] | None = None,
                             master_text: str | None = None,
                             subset_refs: set[str] | None = None,
                             headline_terms: set[str] | None = None,
                             expected_headline: str | None = None,
                             master_summary: str | None = None) -> list[str]:
    """Return user-visible evidence failures; an empty list permits rendering.

    ``subset_refs`` names the refs whose rewrite must be a subset of its source
    -- skills lines, which may be pruned and reordered but never added to.
    ``headline_terms`` is the vocabulary the headline is held to.
    ``expected_headline`` and ``master_summary`` enforce the role-specific
    rewrite rules that vocabulary and fact checks alone cannot express.
    """
    errors: list[str] = []
    subset_refs = subset_refs or set()
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
        if any(ref in subset_refs for ref in refs):
            for word in subset_failures(text, evidence):
                errors.append(
                    f"Bullet {index} is a skills line, so it may only drop and reorder "
                    f"items; it added \"{word}\", which its source line does not contain."
                )

    headline = tailored.get("headline", "")
    if headline and headline_terms is not None:
        for word in headline_failures(headline, headline_terms):
            errors.append(
                f"The headline uses \"{word}\", which is in neither your own headline "
                "nor the target job title."
            )
    if headline and expected_headline and headline.strip() != expected_headline.strip():
        errors.append(
            "The headline must be the current title, an em dash, then the target job title."
        )

    summary = tailored.get("summary", "")
    if summary and allowed_terms is not None:
        # The summary draws on the whole resume, not one bullet, so it is
        # checked against the full master text rather than a single source.
        joined = master_text if master_text is not None else " ".join(sources.values())
        for fact in unsupported_facts(summary, joined):
            errors.append(f"The summary contains unsupported hard fact {fact}.")
        for term in unsupported_terms(summary, allowed_terms):
            errors.append(f"The summary claims unsupported term {term}.")
    if summary and sentence_count(summary) > SUMMARY_MAX_SENTENCES:
        errors.append(
            f"The summary runs to {sentence_count(summary)} sentences; "
            f"keep it to {SUMMARY_MAX_SENTENCES} at most."
        )
    if summary and master_summary and summary.strip() == master_summary.strip():
        errors.append("The summary is unchanged from your master resume; rewrite it for this job.")
    return errors
