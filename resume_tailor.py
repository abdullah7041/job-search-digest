"""Turn a verified job description into a tailored, evidence-checked resume.

The model is given the parsed master resume with a stable ref on every bullet,
and it answers with refs, not prose it invented from nothing. Selection and
ordering are its job; fabrication is structurally awkward because anything it
writes is checked back against the exact bullet it cited.
"""

from __future__ import annotations

import copy
import json
import re
import time
import requests

from openrouter import chat_json, configured_chain
from resume_render import LABEL_RE, _clean
from resume_source import (MATCH_CATEGORIES, snippet_supported, unsupported_facts,
                           unsupported_terms, validate_tailored_resume, vocabulary)
from fit_check import CHECK_SCHEMA, blocked_score_ceiling, verify as verify_fit


# Tailoring is long-form generation under hard truth constraints, and the
# evidence check rejects a draft that loosens them — which costs a retry. So the
# chain is ordered by instruction-following per token, not by headline price:
# the flash-class tier below these reliably fails the check and saves nothing.
# One request body is sent to whichever member serves the call, so every member
# has to accept response_format and reasoning_effort.
DEFAULT_MODEL = "openai/gpt-5.6-luna"
DEFAULT_FALLBACKS = "google/gemini-3.7-flash,minimax/minimax-m3"

TAILOR_MODEL, TAILOR_FALLBACKS = configured_chain(
    "TAILOR_MODEL", "TAILOR_FALLBACK_MODELS", DEFAULT_MODEL, DEFAULT_FALLBACKS)

# The prompt carries the master resume, name and contact details included, so
# routing must skip providers that retain prompts. It must also skip endpoints
# that would ignore the schema and answer in prose, which require_parameters
# enforces.
TAILOR_PROVIDER = {"data_collection": "deny", "require_parameters": True}

JD_CHARS = 6000
MAX_ATTEMPTS = 2

# One click must always report an outcome. The workflow is killed at ten minutes
# and the Worker drops the task at fourteen, and a kill posts nothing at all,
# leaving "Preparing your tailored CV privately..." on screen forever. This
# budget spans every attempt and every retry, so the runner always gets to
# answer — even when the answer is that tailoring ran out of time.
BUDGET_SECONDS = 210
COMPARISON_BUDGET_SECONDS = 90

COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "before_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "after_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "changes": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "properties": {
                "source_ref": {"type": "string"}, "jd_quote": {"type": "string"},
                "after_quote": {"type": "string"}},
            "required": ["source_ref", "jd_quote", "after_quote"], "additionalProperties": False}},
        "fit_check": CHECK_SCHEMA,
    },
    "required": ["before_score", "after_score", "changes", "fit_check"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "highlight_terms": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
                "required": ["ref", "text"],
                "additionalProperties": False,
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "fit_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "highlight_terms", "bullets", "gaps", "fit_notes"],
    "additionalProperties": False,
}


# Which sections the model may reorder and prune the standalone lines of.
# EXPERIENCE and PROJECTS lines are achievements. SKILLS lines are lists of
# claims, so they get the stricter subtractive rule below rather than the
# ordinary evidence rule. EDUCATION and LANGUAGES are credentials: a
# role-specific edit there would drop qualifications the reader expects to see.
ACHIEVEMENT_LINE_SECTIONS = ("EXPERIENCE", "PROJECTS")
SUBSET_LINE_SECTIONS = ("SKILLS",)


def subset_line_refs(document: dict) -> set[str]:
    """Refs whose rewrite may only drop and reorder what the source already has."""
    return {line["ref"] for section in document["sections"]
            if section["heading"] in SUBSET_LINE_SECTIONS
            for line in section["lines"]}


def build_prompt(document: dict, snapshot: dict, corrections: list[str] | None = None) -> str:
    """Compose the tailoring instruction around the resume's own refs."""
    inventory = []
    for section in document["sections"]:
        for entry in section["entries"]:
            for bullet in entry["bullets"]:
                inventory.append({"ref": bullet["ref"], "section": section["heading"],
                                  "role": entry["title"], "text": bullet["source"]})
        for line in section["lines"]:
            inventory.append({"ref": line["ref"], "section": section["heading"],
                              "role": "", "text": line["source"]})

    achievements = (
        sum(len(entry["bullets"]) for section in document["sections"]
            for entry in section["entries"])
        + sum(len(section["lines"]) for section in document["sections"]
              if section["heading"] in ("EXPERIENCE", "PROJECTS"))
    )
    target = max(6, min(16, round(0.7 * achievements)))

    prompt = (
        "You tailor one candidate's resume to one job. You are editing an existing "
        "resume, not writing a new one.\n\n"
        "RULES, all mandatory:\n"
        "1. Every bullet you return must cite the ref of the source bullet it came from. "
        "Never invent a bullet that has no ref.\n"
        "2. Every word you write must already appear in this resume, in the target role's "
        "title, or in the employer's name. Use the job description to decide which bullets "
        "to keep, how to order them, and which of the candidate's own words to lead with — "
        "never to decide what words to add. A number, percentage, employer, date, product, "
        "framework, platform or credential that the cited ref's source text does not already "
        "contain is rejected, and so is any other proper noun or acronym that appears only in "
        "the job description.\n"
        "3. Drop bullets that do not help for this job. Order the ones you keep by "
        "relevance to the job's stated requirements. Keep at least two per role you keep.\n"
        "4. The headline is the candidate's current title, then an em dash, then the "
        "job's title, and nothing else: \"<current title> — <target title>\".\n"
        "5. Rewrite the summary for this job. Its first word must be exactly Saudi, "
        "followed by a space. Choose the wording after Saudi for this job using only "
        "evidence in the resume; no fixed professional title is required. Lead with "
        "the two or three things this job "
        "asks for that the resume genuinely evidences, described in the resume's own "
        "words, and drop what this job does not care about. Three sentences at most, and "
        "every fact in it must already be in the resume. Returning the existing summary "
        "unchanged is a failure: it is written for a different job.\n"
        "Keep experience dates attached to their original career track. Commercial analytics "
        "experience is not software engineering experience. Listed knowledge such as RAG "
        "concepts or n8n is not a claim of shipped implementations. Never promote it into "
        "an achievement or claim model training from LLM application/evaluation work.\n"
        "6. gaps: the job's real requirements that this resume does not evidence. Name the "
        "specific missing framework, platform, or credential. Never list ordinary English "
        "words. Three to five items, empty if there are genuinely none.\n"
        "7. fit_notes: the two or three strongest reasons this candidate matches, each "
        "naming concrete evidence.\n"
        f"8. This resume offers {achievements} achievement bullets. Keep about {target} of "
        "them — the strongest for this job — and drop the rest, so the result fits one "
        "page. Never return more than 16. Skills lines are not achievements and do not "
        "count towards that number; education and language lines are never returned at "
        "all.\n"
        "9. Return every line from the SKILLS section, in the order this job makes "
        "sensible — the group this job cares about most first. Inside a line you may drop "
        "items the job has no use for and reorder the rest. You may not add an item, a "
        "word, or a group: a skills line is a list of claims, and anything new in one "
        "reads as a skill the candidate never made. Keep the \"Label:\" prefix.\n"
        "10. highlight_terms: up to eight short skills or phrases appearing in both "
        "the target title or job description and your completed resume. Choose specific "
        "job-relevant terms, not whole sentences. Return all resume wording as plain "
        "text, without Markdown or HTML; formatting is applied separately.\n\n"
        f"TARGET ROLE: {snapshot.get('title', '')} at {snapshot.get('company', '')}, "
        f"{snapshot.get('location', '')}\n\n"
        f"JOB DESCRIPTION:\n{(snapshot.get('jd', {}).get('text') or '')[:JD_CHARS]}\n\n"
        f"CANDIDATE HEADLINE: {document['headline']}\n"
        f"CANDIDATE SUMMARY: {document['summary']}\n\n"
        f"SOURCE BULLETS:\n{json.dumps(inventory, ensure_ascii=False, indent=1)}\n"
    )
    if corrections:
        prompt += (
            "\nYour previous answer was rejected by the evidence check. Fix exactly these "
            "problems and keep everything else:\n- " + "\n- ".join(corrections[:12]) + "\n"
        )
    return prompt


def call_model(prompt: str, api_key: str, deadline: float) -> dict:
    """One tailoring plan from the model chain, or raise for the caller."""
    return chat_json(prompt, RESPONSE_SCHEMA, "tailoring_plan", api_key,
                     TAILOR_MODEL, TAILOR_FALLBACKS, deadline,
                     provider=TAILOR_PROVIDER)


def apply_highlights(document: dict, terms: list[str], snapshot: dict) -> dict:
    """Add bounded emphasis without changing validated wording or existing labels."""
    result = copy.deepcopy(document)
    posting = snapshot.get("title", "") + " " + (snapshot.get("jd", {}).get("text") or "")
    patterns = []
    for term in terms[:8]:
        if not isinstance(term, str) or not term.strip():
            continue
        # A skill such as C must not match C++ or C#, nor SQL PostgreSQL.
        phrase = r"\s+".join(re.escape(word) for word in term.split())
        pattern = re.compile(r"(?<![\w+#])" + phrase + r"(?![\w+#])", re.IGNORECASE)
        if pattern.search(posting):
            patterns.append(pattern)
    patterns.sort(key=lambda pattern: len(pattern.pattern), reverse=True)
    if not patterns:
        return result
    matcher = re.compile("|".join(pattern.pattern for pattern in patterns), re.IGNORECASE)

    def emphasize(text, limit):
        remaining = limit
        def replace(match):
            nonlocal remaining
            if remaining <= 0:
                return match.group(0)
            remaining -= 1
            return "**" + match.group(0) + "**"
        # Existing emphasis (including labels) is never nested or counted as
        # a new highlight. Each match retains the resume's exact spelling.
        return "".join(part if part.startswith("**") else matcher.sub(replace, part)
                       for part in re.split(r"(\*\*.+?\*\*)", text))

    result["summary"] = emphasize(result["summary"], 3)
    for section in result["sections"]:
        if section["heading"] not in (*ACHIEVEMENT_LINE_SECTIONS, *SUBSET_LINE_SECTIONS):
            continue
        items = section["lines"] + [bullet for entry in section["entries"] for bullet in entry["bullets"]]
        for item in items:
            text = item["text"]
            if item.get("emphasis") and "**" not in text:
                text = LABEL_RE.sub(r"**\1**\2", text)
            item["text"] = emphasize(text, 2)
    return result


def apply_plan(document: dict, plan: dict) -> dict:
    """Rebuild the document from the model's kept refs, in its chosen order."""
    tailored = copy.deepcopy(document)
    rewrites = {item["ref"]: item["text"].strip() for item in plan.get("bullets", [])}
    order = {item["ref"]: index for index, item in enumerate(plan.get("bullets", []))}

    if plan.get("headline"):
        tailored["headline"] = plan["headline"].strip()
    if plan.get("summary"):
        tailored["summary"] = plan["summary"].strip()

    for section in tailored["sections"]:
        for entry in section["entries"]:
            kept = [dict(bullet, text=rewrites[bullet["ref"]])
                    for bullet in entry["bullets"] if bullet["ref"] in rewrites]
            entry["bullets"] = sorted(kept, key=lambda bullet: order[bullet["ref"]])
        section["entries"] = [entry for entry in section["entries"]
                              if entry["bullets"] or entry["lead"]]
        if section["heading"] in SUBSET_LINE_SECTIONS:
            # Every skills category survives. The model decides which line leads
            # and what stays inside it, never whether a category exists at all:
            # dropping "Data & Analytics" on a data role because the model spent
            # its picks elsewhere would present a narrower engineer than the
            # master resume does. A line it did not rewrite keeps its master
            # wording and falls in behind the ones it did.
            rewritten = [dict(line, text=rewrites[line["ref"]])
                         for line in section["lines"] if line["ref"] in rewrites]
            untouched = [line for line in section["lines"] if line["ref"] not in rewrites]
            section["lines"] = sorted(rewritten, key=lambda line: order[line["ref"]]) + untouched
        elif section["heading"] in ACHIEVEMENT_LINE_SECTIONS:
            kept_lines = [dict(line, text=rewrites[line["ref"]])
                          for line in section["lines"] if line["ref"] in rewrites]
            if kept_lines:
                section["lines"] = sorted(kept_lines, key=lambda line: order[line["ref"]])

    # A role kept only for its lead line adds height without evidence.
    for section in tailored["sections"]:
        section["entries"] = [entry for entry in section["entries"] if entry["bullets"]]
    return tailored


MIN_BULLETS = 8
MIN_EXPERIENCE_BULLETS = 4
MIN_RETAINED_SHARE = 0.4


def _floor(available: int, cap: int) -> int:
    """How many bullets a draft must retain, scaled to what it was given.

    A short master resume cannot yield sixteen bullets, so a fixed threshold
    would reject every draft built from one. The requirement tracks the
    material available and never exceeds the absolute cap.
    """
    return min(cap, max(2, round(MIN_RETAINED_SHARE * available)))


def substance_failures(plan: dict, document: dict) -> list[str]:
    """Catch a draft that is technically valid but too thin to send.

    Every bullet passing the evidence check says nothing about how many
    survived. A model that returns two bullets produces a document with a
    name, a skills list and almost no career — which renders cleanly, fits one
    page, and would have gone out labelled as successfully tailored.
    """
    # Skills lines are returned on every draft now, so counting them here would
    # let five of them satisfy a floor meant to prove the career survived.
    skills = subset_line_refs(document)
    kept = {item.get("ref") for item in plan.get("bullets", [])} - skills
    available = {bullet["ref"] for section in document["sections"]
                 for entry in section["entries"] for bullet in entry["bullets"]}
    available |= {line["ref"] for section in document["sections"]
                  for line in section["lines"]} - skills
    experience = {bullet["ref"]
                  for section in document["sections"] if section["heading"] == "EXPERIENCE"
                  for entry in section["entries"] for bullet in entry["bullets"]}

    failures = []
    total_floor = _floor(len(available), MIN_BULLETS)
    experience_floor = _floor(len(experience), MIN_EXPERIENCE_BULLETS)
    if len(kept) < total_floor:
        failures.append(
            f"You kept only {len(kept)} bullets; keep at least {total_floor} so the resume is not empty."
        )
    if len(kept & experience) < experience_floor:
        failures.append(
            f"You kept only {len(kept & experience)} experience bullets; a resume needs at "
            f"least {experience_floor}."
        )
    return failures


def clean_gaps(plan: dict, document: dict, snapshot: dict) -> list[str]:
    """Keep only gaps that name something concrete the resume lacks."""
    known = vocabulary(document["summary"], document["headline"],
                       *[bullet["source"] for section in document["sections"]
                         for entry in section["entries"] for bullet in entry["bullets"]],
                       *[line["source"] for section in document["sections"]
                         for line in section["lines"]])
    gaps = []
    for gap in plan.get("gaps", []):
        gap = re.sub(r"\s+", " ", str(gap)).strip()
        if len(gap) < 12:
            continue
        if not unsupported_terms(gap, known):
            # Nothing in it is actually absent from the resume, so it is filler.
            continue
        gaps.append(gap)
    return gaps[:5]


def clean_fit_notes(plan: dict, allowed: set[str], master_text: str) -> list[str]:
    """Keep only fit notes whose claims the resume actually supports.

    The mirror of clean_gaps: a gap must name something the resume lacks, and a
    fit note must name nothing it lacks. This was the one model output nothing
    checked, and it is the text the candidate reads under "Why you fit" and is
    most likely to repeat in an interview -- so an invented framework or an
    inflated percentage here costs more than one in the PDF would.

    ``allowed`` is tailor's own vocabulary -- the resume plus the target title
    and employer -- not clean_gaps' narrower resume-only set. A fit note names
    the role it is arguing for; checked against the resume alone, every note
    that says the employer's name would be thrown away.

    A number the resume does not contain is dropped even when the job
    description supplied it, so a note quoting a stated requirement ("meets the
    5+ years asked for") does not survive unless the resume states that figure.
    That is the deliberate side of the trade: the case worth catching is a note
    inflating one of the candidate's own metrics, and there is no way to tell
    the two apart from the number alone.
    """
    notes = []
    for note in plan.get("fit_notes", []):
        note = re.sub(r"\s+", " ", str(note)).strip()
        if not note:
            continue
        if unsupported_terms(note, allowed) or unsupported_facts(note, master_text):
            continue
        notes.append(note)
    return notes[:3]


def _alignment_view(document: dict) -> dict:
    """Compare the final visible wording, omitting contact details and internal source text."""
    def item_view(item):
        return {"ref": item["ref"], "text": _clean(item["text"])}
    return {"summary": _clean(document["summary"]), "sections": [
        {"heading": section["heading"], "intro": section.get("intro", ""),
         "entries": [{"title": entry["title"], "meta": entry["meta"], "lead": entry["lead"],
                      "bullets": [item_view(b) for b in entry["bullets"]]}
                     for entry in section["entries"]],
         "lines": [item_view(line) for line in section["lines"]]}
        for section in document["sections"]]}


def compare_alignment(original: dict, final: dict, snapshot: dict,
                      api_key: str, master_text: str) -> dict | None:
    """One paired estimate, guarded by source evidence and unchanged qualification caps."""
    from job_archive import GRADE_FULL, snapshot_grade
    if snapshot_grade(snapshot) != GRADE_FULL:
        return None
    before, after = _alignment_view(original), _alignment_view(final)
    jd_text = snapshot.get("jd", {}).get("text") or ""
    weights = ", ".join(f"{name} {weight}" for name, weight in MATCH_CATEGORIES.items())
    prompt = (
        "Compare original and final CV presentation for the SAME candidate and SAME job. "
        f"Use the existing 0-100 fit rubric: {weights}. 80+ is strong demonstrated alignment, "
        "60-79 is competitive with gaps; below 60 indicates significant gaps. "
        "These are CV alignment estimates, not hiring or ATS probabilities. "
        "Assess both in this one call, on exactly the same requirements and qualifications. "
        "Formatting, a copied target title, or keyword repetition alone earns no increase. "
        "Accept equal or lower after scores. Never invent requirements. "
        "Use experience/projects as demonstrated evidence, not summary/skills keywords alone. "
        "Actual experience dates, education, seniority and location remain unchanged. "
        "Do not transfer analytics years into software experience or credit model training "
        "from LLM application work. Listed RAG/n8n knowledge is not shipped implementation. "
        "For every claimed presentation gain, cite a source_ref from an EXPERIENCE or PROJECT "
        "achievement, a verbatim jd_quote and an after_quote still present in the final CV. "
        "Provide fit_check against the original master: blockers are ONLY hard requirements "
        "the JD states that this candidate lacks. Preferred qualifications or degrees accepting "
        "equivalent experience are not degree blockers. Cite verbatim evidence with source set "
        "to resume or job_description. Put ambiguities in unclear. Keep strengths and blockers "
        "under 12-word titles and 30-word explanations. Never predict an employer decision.\n"
        f"TARGET: {snapshot.get('title', '')} at {snapshot.get('company', '')}, "
        f"{snapshot.get('location', '')}\nJOB DESCRIPTION:\n{jd_text}\n"
        f"ORIGINAL:\n{json.dumps(before, ensure_ascii=False)}\n"
        f"FINAL PDF CONTENT:\n{json.dumps(after, ensure_ascii=False)}"
    )
    try:
        raw = chat_json(prompt, COMPARISON_SCHEMA, "cv_alignment_comparison", api_key,
                        TAILOR_MODEL, TAILOR_FALLBACKS,
                        time.monotonic() + COMPARISON_BUDGET_SECONDS, provider=TAILOR_PROVIDER)
        scores = [raw["before_score"], raw["after_score"]]
        if any(type(score) is not int or not 0 <= score <= 100 for score in scores):
            return None
        job = {"title": snapshot.get("title", ""), "company": snapshot.get("company", ""),
               "description": jd_text}
        check = verify_fit(raw["fit_check"], job, master_text)
        if check["note"] or check["unclear"]:
            return None
        original_refs = {b["ref"]: b["source"] for section in original["sections"]
                         for entry in section["entries"] for b in entry["bullets"]}
        final_refs = {b["ref"]: _clean(b["text"]) for section in final["sections"]
                      for entry in section["entries"] for b in entry["bullets"]}
        changes = []
        for item in raw.get("changes", [])[:3]:
            ref, quote = item.get("source_ref"), item.get("after_quote", "")
            if (ref in original_refs and ref in final_refs
                    and snippet_supported(item.get("jd_quote", ""), jd_text)
                    and snippet_supported(quote, original_refs[ref])
                    and snippet_supported(quote, final_refs[ref])):
                changes.append(quote)
        ceiling = blocked_score_ceiling(check["blockers"])
        if snapshot.get("ranking_verified") and snapshot.get("blockers"):
            ceiling = min(ceiling if ceiling is not None else 100, snapshot["score"])
        if ceiling is not None:
            scores = [min(score, ceiling) for score in scores]
        if before == after:
            scores[1] = scores[0]
        if scores[1] > scores[0] and not changes:
            return None
        return {"before": scores[0], "after": scores[1], "changes": changes,
                "gaps": list(dict.fromkeys([b["title"] for b in check["blockers"]]
                                           + (snapshot.get("blockers") or [])))}
    except (ValueError, KeyError, TypeError, AttributeError, requests.RequestException):
        return None


def tailor(document: dict, snapshot: dict, api_key: str, sources: dict[str, str],
           master_text: str) -> tuple[dict, list[str], list[str]]:
    """Return the tailored document, its gaps and its fit notes.

    Raises ValueError if the model cannot produce an evidence-clean answer, so
    the caller can fall back to sending the untouched master resume.
    """
    allowed = vocabulary(master_text, snapshot.get("title", ""), snapshot.get("company", ""))
    # Rule 4 permits the candidate's own headline and the job's title, nothing
    # else. Until now nothing enforced it: the model's headline was written
    # straight into the document unchecked.
    headline_terms = vocabulary(document["headline"], snapshot.get("title", ""))
    expected_headline = None
    if document["headline"].strip() and snapshot.get("title", "").strip():
        expected_headline = f"{document['headline'].strip()} — {snapshot['title'].strip()}"
    skills_refs = subset_line_refs(document)
    corrections: list[str] = []
    last_errors: list[str] = []
    # One budget spans both attempts and all their retries, so a slow chain
    # cannot outlive the workflow that is waiting to report an outcome.
    deadline = time.monotonic() + BUDGET_SECONDS

    for _ in range(MAX_ATTEMPTS):
        plan = call_model(build_prompt(document, snapshot, corrections), api_key, deadline)
        markup_errors = []
        wording = [plan.get("summary", "")] + [item["text"] for item in plan.get("bullets", [])]
        if any(re.search(r"</?[A-Za-z][^>]*>", text) for text in wording):
            markup_errors.append("Return plain resume text without HTML tags.")
        plan["summary"] = _clean(plan.get("summary", ""))
        for item in plan.get("bullets", []):
            item["text"] = _clean(item["text"])
        checked = {
            "headline": plan.get("headline", ""),
            "summary": plan.get("summary", ""),
            "bullets": [{"text": item["text"], "source_refs": [item["ref"]]}
                        for item in plan.get("bullets", [])],
        }
        errors = validate_tailored_resume(checked, sources, allowed, master_text,
                                          subset_refs=skills_refs,
                                          headline_terms=headline_terms,
                                          expected_headline=expected_headline,
                                          master_summary=document["summary"],
                                          require_saudi_start=True)
        errors += substance_failures(plan, document)
        errors += markup_errors
        if not errors:
            return (apply_highlights(apply_plan(document, plan), plan.get("highlight_terms", []), snapshot),
                    clean_gaps(plan, document, snapshot),
                    clean_fit_notes(plan, allowed, master_text))
        last_errors = errors
        corrections = errors

    raise ValueError("The tailored draft failed the evidence check: " + "; ".join(last_errors[:3]))
