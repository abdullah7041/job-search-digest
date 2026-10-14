"""Evidence-gated reality check for the jobs the ranker scored highest.

Plan 018A fixed what the ranker READS. This fixes what the card SAYS. A score of
85 with a one-line reason cannot tell you that the posting requires a degree you
do not have; it just reads as good news.

Ported from Watheq's strategic reality check
(`netlify/lib/strategic-reality-check.js`, `src/lib/match/optimizationOutlook.ts`).
The important part of that design is not the prompt -- it is that the verdict is
computed from a SEPARATELY VERIFIED evidence object rather than from the number.
A model can say anything; `verify` below re-checks every claim against the
actual resume and job text before any of it is allowed to change a score.

The asymmetry is deliberate and is Watheq's:

  * a blocker whose evidence does not check out is DEMOTED to `unclear`
  * a strength whose evidence does not check out is DELETED

Unverifiable pessimism is softened into a question. Unverifiable optimism is
removed. Getting this backwards would let an unfounded claim inflate a card,
which is the failure this whole pass exists to fix.
"""

from __future__ import annotations

import re
import time

from job_archive import jd_requirements, jd_signal
from openrouter import chat_json
from resume_source import (achievement_evidence, quote_supported, snippet_supported,
                           unsupported_facts, unsupported_terms, vocabulary)

# Only the risk types this pipeline can actually see. Watheq's list also carries
# language/localisation and career-pattern types that need a fuller profile than
# a JD excerpt and a one-page resume.
BLOCKER_TYPES = [
    "missing_required_skill",
    "experience_gap",
    "seniority_mismatch",
    "domain_gap",
    "education_certification_gap",
    "location_work_authorization",
    "role_scope_mismatch",
    # A required skill the resume only LISTS, never demonstrates. Watheq carries
    # this type and it is exactly the case 018A's demonstrated-vs-listed rule
    # exists for, so it earns its place rather than collapsing into "other".
    "evidence_quality",
    "other",
]

SEVERITIES = ["medium", "high", "critical"]

# One call per job, so the population must stay small. split_tiers' strong tier
# runs 3-6 on a normal day; this bounds a board-discovery day that floods it.
MAX_CHECKED_JOBS = 12

# Shared across every job's call, in the style of RANK_BUDGET_SECONDS.
#
# The arithmetic matters. digest.yml gives the execute job timeout-minutes: 10,
# and that wall is NOT graceful -- it kills the run before state persistence, so
# seen.json never records what was delivered and tomorrow re-delivers it. This
# deadline IS graceful: jobs past it keep a neutral result and simply go
# unchecked. So the budget is set to lose checks rather than lose the digest.
#
# 120 + 120 leaves six of the ten minutes for fetching, JD enrichment, link
# verification and delivery. At 180 it was five, which is too thin a margin for
# a step whose duration depends on how many third-party pages need fetching.
# Revisit with real wall times from a live run before raising it.
CHECK_BUDGET_SECONDS = 120

JD_CHECK_CHARS = 3000
RESUME_CHECK_CHARS = 8000

# A digest must never tell its reader what an employer will decide. Ported from
# Watheq's BANNED_CLAIM_PATTERN (strategic-reality-check.js:43).
BANNED_CLAIM_RE = re.compile(
    r"\b(?:will\s+(?:be\s+)?(?:reject(?:ed)?|screen(?:ed)?\s*out|fail|"
    r"get\s+an\s+interview|not\s+get\s+an\s+interview)|guarantee(?:d|s)?|"
    r"certain(?:ly)?\s+(?:reject|screen|fail|hire)|hire\s+probability|"
    r"interview\s+probability|disqualified|fatal\s+flaw)\b", re.I)

MAX_BLOCKERS = 3
MAX_STRENGTHS = 3
MAX_TEXT = 200

_EVIDENCE = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["resume", "job_description"]},
        "snippet": {"type": "string"},
    },
    "required": ["source", "snippet"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": BLOCKER_TYPES},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "evidence": {"type": "array", "items": _EVIDENCE},
                },
                "required": ["type", "severity", "title", "explanation", "evidence"],
                "additionalProperties": False,
            },
        },
        "unclear": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence_needed": {"type": "string"},
                },
                "required": ["topic", "reason", "evidence_needed"],
                "additionalProperties": False,
            },
        },
        "strengths": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "evidence": {"type": "array", "items": _EVIDENCE},
                },
                "required": ["title", "why_it_matters", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["blockers", "unclear", "strengths"],
    "additionalProperties": False,
}


def neutral_result(reason: str = "") -> dict:
    """What a failed or empty check returns: no blockers, therefore no verdict.

    Silence must mean "no verdict", never "bad verdict". A check that times out
    or comes back unparseable must not be able to cap a score -- otherwise a
    provider outage quietly demotes every strong match in the digest.
    """
    return {"blockers": [], "unclear": [], "strengths": [],
            "ceiling": "unknown", "note": reason}


def check_text(job: dict) -> str:
    """The part of the posting this check is allowed to judge, and its demands.

    A plain `description[:JD_CHECK_CHARS]` slice is what made this pass useless
    on the posting it was built for. The Ma'aden posting archived 2026-09-08 is
    5187 characters with its Minimum Qualifications at 3958, so a 4000-character
    prefix cut mid-word at "Bachelor's degre": the check saw the heading, could
    not quote a verifiable snippet from half a word, returned zero blockers, and
    `ceiling_band` duly reported "high" on a job whose stated minimum
    qualification the candidate does not hold.

    Same two-part reading as the ranker, from the same helpers, so the two
    cannot drift: the window where the posting describes the work, plus the
    requirement lines read from the whole text wherever they sit.
    """
    description = job.get("description") or ""
    excerpt = jd_signal(description, JD_CHECK_CHARS)
    requirements = jd_requirements(description)
    if not requirements:
        return excerpt
    return excerpt + "\n\nStated requirements:\n" + "\n".join(
        f"- {line}" for line in requirements)


def build_prompt(job: dict, resume_text: str) -> str:
    """One job, the resume, and the rules that make the answer checkable."""
    return (
        "You are a conservative hiring analyst. Decide what would genuinely stop "
        "this candidate being competitive for this specific role.\n\n"
        f"<resume>\n{resume_text[:RESUME_CHECK_CHARS]}\n</resume>\n\n"
        f"<job_description>\n{check_text(job)}\n</job_description>\n\n"
        f"Job title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Location: {job.get('location', '')}\n\n"
        "Rules:\n"
        "- A blocker is a HARD requirement this posting states that the resume "
        "does not evidence. A requirement written as 'preferred', 'nice to have' "
        "or 'a plus' is not a blocker. A degree accepting equivalent experience "
        "is not a missing-degree blocker; assess any stated experience requirement separately. "
        "At most three, most severe first.\n"
        "- The 'Stated requirements' list, when present, is where this posting "
        "says what it demands. It is read from the whole posting, so a "
        "requirement there counts even if the text above never repeats it.\n"
        "- A degree, licence or certification the posting states as a minimum "
        "and the resume does not show is an education_certification_gap. Say so "
        "plainly rather than folding it into a skills blocker.\n"
        "- Every blocker and every strength must cite evidence: a short snippet "
        "COPIED VERBATIM from the resume or the job description, with source set "
        "to which one it came from. Do not paraphrase a snippet.\n"
        "- Cite the job description for what the role requires, and the resume "
        "for what the candidate has or lacks.\n"
        "- If something is missing, ambiguous, or you had to infer it, put it in "
        "unclear instead of blockers. unclear is not a weaker blocker; it is the "
        "honest place for anything you cannot show.\n"
        "- A skill the resume only LISTS under a skills or summary heading, "
        "without an experience or project entry showing it in use, is not "
        "evidence. Treat it as unproven.\n"
        "- Invent nothing: no skills, credentials, employers, dates or metrics "
        "that are not in the text.\n"
        "- Never state or imply what the employer will decide. Do not say the "
        "candidate will be rejected, screened out, or interviewed.\n"
        "- Keep every title under 12 words and every explanation under 30."
    )


def _clean(text: object) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_TEXT]
    # Substitution rather than rejection: the surrounding claim may be sound
    # even when its phrasing overreaches.
    if BANNED_CLAIM_RE.search(collapsed):
        return "This digest can find evidence gaps, but not predict an employer's decision."
    return collapsed


def _verified_evidence(items: object, resume_text: str, jd_text: str) -> list[dict]:
    """Keep only cited snippets that really occur in the source they name."""
    kept = []
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        snippet = _clean(item.get("snippet"))
        against = resume_text if source == "resume" else jd_text
        if source in ("resume", "job_description") and snippet_supported(snippet, against):
            kept.append({"source": source, "snippet": snippet})
    return kept


def _as_unclear(blocker: dict, reason: str) -> dict:
    return {
        "topic": _clean(blocker.get("title")) or "Needs review",
        "reason": reason,
        "evidence_needed": _clean(blocker.get("explanation")),
    }


def valid_check_response(raw) -> bool:
    """Validate the small check contract locally, before interpreting a verdict.

    Provider schema enforcement is a request, not proof that the response obeyed
    it. A malformed empty result must never read as a completed clean check.
    """
    if not isinstance(raw, dict) or set(raw) != set(CHECK_SCHEMA["required"]):
        return False
    for name, schema in CHECK_SCHEMA["properties"].items():
        values = raw[name]
        if not isinstance(values, list):
            return False
        item_schema = schema["items"]
        for item in values:
            if not isinstance(item, dict) or set(item) != set(item_schema["required"]):
                return False
            for field, field_schema in item_schema["properties"].items():
                value = item[field]
                if field == "evidence":
                    if not isinstance(value, list):
                        return False
                    for evidence in value:
                        if (not isinstance(evidence, dict) or set(evidence) != {"source", "snippet"}
                                or evidence["source"] not in ("resume", "job_description")
                                or not isinstance(evidence["snippet"], str)):
                            return False
                elif (not isinstance(value, str)
                      or ("enum" in field_schema and value not in field_schema["enum"])):
                    return False
    return True


def verify(raw: dict, job: dict, resume_text: str) -> dict:
    """Re-check every claim before any of it is allowed to change a score.

    Pure, so it is testable without a model. This is the whole mechanism; the
    prompt above only asks nicely.

    Blockers and strengths are gated DIFFERENTLY, because they assert different
    things:

    * A blocker says THE POSTING DEMANDS THIS. It must quote the posting. An
      invented requirement cannot be quoted from a document that never states
      it, which is the whole gate.
    * A strength says THE CANDIDATE HAS THIS. Its evidence must check out AND
      its named terms must exist in the resume's vocabulary, since a strength
      is a claim about the candidate rather than about the job.

    `unsupported_terms` is deliberately NOT applied to blocker titles. Plan 018
    specified it, and it fails twice over. Pointed at the resume it rejects
    every true blocker, because a missing requirement is missing from the resume
    by definition. Pointed at the job description it still misfires, because it
    flags capitalised words mid-sentence as proper nouns -- sound for a prose
    bullet, meaningless for a Title Case heading where every word is capital.
    Measured against the live HungerStation posting it threw away all three real
    blockers on the words "Dedicated", "Required" and "Tools", while every one
    of their nine evidence snippets verified. A gate with that false-positive
    rate is worse than no gate.
    """
    if not valid_check_response(raw):
        return neutral_result("requirements check response was malformed")
    jd_text = job.get("description") or ""
    demonstrated = achievement_evidence(resume_text)
    from_resume = vocabulary(demonstrated)

    blockers, unclear, strengths = [], [], []

    for item in (raw.get("unclear") if isinstance(raw.get("unclear"), list) else []):
        if isinstance(item, dict):
            unclear.append({"topic": _clean(item.get("topic")),
                            "reason": _clean(item.get("reason")),
                            "evidence_needed": _clean(item.get("evidence_needed"))})

    for item in (raw.get("blockers") if isinstance(raw.get("blockers"), list) else []):
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        evidence = _verified_evidence(item.get("evidence"), resume_text, jd_text)
        if not evidence:
            unclear.append(_as_unclear(item, "No quoted evidence survived checking."))
            continue
        # A blocker asserts the POSTING demands something. Quoting the posting
        # is what makes that checkable, and an invented requirement cannot be
        # quoted from a document that never states it.
        if not any(e["source"] == "job_description" for e in evidence):
            unclear.append(_as_unclear(
                item, "Nothing quoted from the posting shows it requires this."))
            continue
        # Read the complete clause, since a quote can omit "preferred" or the
        # degree's equivalent-experience alternative at its end.
        clauses = [clause for clause in re.split(r"\n|[;,]\s*|(?<=\.)\s+|\s+(?:and|but|while)\s+", jd_text, flags=re.I)
                   if any(quote_supported(e["snippet"], clause) for e in evidence
                          if e["source"] == "job_description")]
        def optional(clause):
            if re.search(r"\bnot required\b", clause, re.I):
                return True
            if (item.get("type") == "education_certification_gap"
                    and re.search(r"\bor\s+equivalent\b.*\bexperience\b", clause, re.I)):
                return True
            if re.search(r"\b(?:required|mandatory|must)\b", clause, re.I):
                return False
            return bool(re.search(r"\b(?:preferred|nice.to.have|a plus|not required)\b", clause, re.I))
        if clauses and all(optional(clause) for clause in clauses):
            continue
        if any(re.search(r"\b(?:preferred|nice.to.have)\b", clause, re.I)
               and re.search(r"\b(?:required|mandatory|must)\b", clause, re.I) for clause in clauses):
            unclear.append(_as_unclear(item, "Mixed qualification wording needs clarification."))
            continue
        blockers.append({
            "type": item.get("type") if item.get("type") in BLOCKER_TYPES else "other",
            "severity": item.get("severity") if item.get("severity") in SEVERITIES else "medium",
            "title": title,
            "explanation": _clean(item.get("explanation")),
            "evidence": evidence,
        })

    for item in (raw.get("strengths") if isinstance(raw.get("strengths"), list) else []):
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        evidence = _verified_evidence(item.get("evidence"), resume_text, jd_text)
        evidence = [e for e in evidence if e["source"] != "resume"
                    or (quote_supported(e["snippet"], demonstrated)
                        and not unsupported_facts(e["snippet"], demonstrated))]
        # Deleted, not demoted. An unproven strength is exactly the claim that
        # put an 85 on a card, so it does not get a softer landing.
        if (not any(e["source"] == "resume" and quote_supported(e["snippet"], demonstrated)
                    for e in evidence)
                or unsupported_terms(title, from_resume)):
            continue
        strengths.append({"title": title,
                          "why_it_matters": _clean(item.get("why_it_matters")),
                          "evidence": evidence})

    blockers = blockers[:MAX_BLOCKERS]
    # `unclear` is diagnostic today: nothing outside this module reads it, and
    # it reaches neither the snapshot nor the card. The demotion still does real
    # work -- a demoted blocker stops counting toward ceiling_band -- so this is
    # a record of what was rejected, not a feature. If it is ever surfaced, note
    # that this truncation drops model-supplied items before demoted ones.
    return {"blockers": blockers, "unclear": unclear[:MAX_BLOCKERS],
            "strengths": strengths[:MAX_STRENGTHS],
            "ceiling": ceiling_band(blockers), "note": ""}


def ceiling_band(blockers: list[dict]) -> str:
    """How far better wording could get this application.

    Port of Watheq's computeOptimizationOutlook
    (src/lib/match/optimizationOutlook.ts:35-41), minus its score thresholds.
    Watheq bands on the score as well because it runs on any match; this runs
    only on jobs already scored at or above STRONG_MATCH_MIN_SCORE, so the score
    carries no information here and including it would be theatre.
    """
    if not blockers:
        return "high"
    if len(blockers) >= 2 or any(b.get("severity") == "critical" for b in blockers):
        return "low"
    return "mid"


# How high a score a verified gap still allows. Replaces a single flat cap that
# fired only on a "low" ceiling -- which needs two blockers or one the model
# happened to call critical, so a single verified missing bachelor's degree
# returned "mid", capped nothing, and showed nothing on the card. That is the
# exact case this pass exists for: 42% of the postings in the archive state a
# degree this candidate does not hold.
#
# Ceilings, not penalties. A posting with three gaps is not three times worse
# than the worst of them, and stacked subtraction took a measured example from
# 91 to 46 -- well past the 67 Watheq gave the same posting. The lowest ceiling
# wins and nothing accumulates.
#
# Every verified gap ceiling is below the daily strong-match delivery floor.
SEVERITY_CEILING = {"critical": 55, "high": 69, "medium": 79}

# A stated degree, licence or certification the resume cannot show caps on its
# own whatever severity the model attached to it. Severity is the model's
# opinion; "the posting states a minimum qualification and the resume does not
# meet it" is a fact about two documents.
CREDENTIAL_CEILING = 69
CREDENTIAL_BLOCKER = "education_certification_gap"


def blocked_score_ceiling(blockers: list[dict]) -> int | None:
    """The highest score these verified gaps leave, or None when they leave any.

    Pure, so the arithmetic is testable without a model, and every blocker that
    reaches it has already survived `verify`.
    """
    ceilings = []
    for blocker in blockers or []:
        if blocker.get("type") == CREDENTIAL_BLOCKER:
            ceilings.append(CREDENTIAL_CEILING)
        # `verify` guarantees a severity from SEVERITIES, and defaults an
        # unrecognised one to "medium". A blocker that reached here without one
        # came from a caller that bypassed verify, and it still describes a real
        # gap -- so it gets the same default rather than escaping the ceiling
        # entirely, which would let a malformed blocker read as no blocker.
        ceilings.append(SEVERITY_CEILING.get(blocker.get("severity"),
                                             SEVERITY_CEILING["medium"]))
    return min(ceilings) if ceilings else None


def check_job(job: dict, resume_text: str, api_key: str, deadline: float,
              model: str, fallbacks: list[str], provider: dict | None = None) -> dict:
    """One verified reality check, or a neutral result. Never raises."""
    try:
        raw = chat_json(build_prompt(job, resume_text), CHECK_SCHEMA, "fit_check",
                        api_key, model, fallbacks, deadline, provider=provider)
        return verify(raw, job, resume_text)
    except Exception as exc:  # noqa: BLE001 - a failed check must not fail the run
        return neutral_result(f"check unavailable: {exc}")


def check_jobs(jobs: list[dict], resume_text: str, api_key: str, model: str,
               fallbacks: list[str], provider: dict | None = None,
               log=lambda message: None) -> None:
    """Attach a verified reality check to each job, highest score first.

    Mutates in place, like apply_ranking and cap_teaser_scores. Stops at the
    shared deadline rather than running the workflow out of time, and any job it
    does not reach keeps a neutral result -- an unchecked job must look
    unchecked, not clean.
    """
    if not resume_text or not api_key:
        for job in jobs:
            job["fit_check"] = neutral_result("no resume evidence available"
                                              if not resume_text else "no api key")
        return
    deadline = time.monotonic() + CHECK_BUDGET_SECONDS
    ordered = sorted(jobs, key=lambda j: j.get("score", 0), reverse=True)
    for index, job in enumerate(ordered):
        if index >= MAX_CHECKED_JOBS or time.monotonic() >= deadline:
            job["fit_check"] = neutral_result("not checked: budget exhausted")
            continue
        result = check_job(job, resume_text, api_key, deadline, model, fallbacks, provider)
        job["fit_check"] = result
        if result["note"]:
            log(f"Fit check skipped {job.get('company')} - {job.get('title')}: {result['note']}")
        elif result["blockers"]:
            log(f"Fit check {job.get('company')} - {job.get('title')}: "
                f"{result['ceiling']} ceiling, {len(result['blockers'])} blocker(s)")
