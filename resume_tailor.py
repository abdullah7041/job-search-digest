"""Turn a verified job description into a tailored, evidence-checked resume.

The model is given the parsed master resume with a stable ref on every bullet,
and it answers with refs, not prose it invented from nothing. Selection and
ordering are its job; fabrication is structurally awkward because anything it
writes is checked back against the exact bullet it cited.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time

import requests

from resume_source import unsupported_terms, validate_tailored_resume, vocabulary


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Tailoring is long-form generation under hard truth constraints, and the
# evidence check rejects a draft that loosens them — which costs a retry. So the
# chain is ordered by instruction-following per token, not by headline price:
# the flash-class tier below these reliably fails the check and saves nothing.
# One request body is sent to whichever member serves the call, so every member
# has to accept response_format and reasoning_effort.
DEFAULT_MODEL = "openai/gpt-5.6-luna"
DEFAULT_FALLBACKS = "google/gemini-3.6-flash,minimax/minimax-m3"

# A GitHub `${{ vars.X }}` that is unset renders as an empty string, so the
# two-argument form of os.environ.get would return "" instead of the default.
TAILOR_MODEL = os.environ.get("TAILOR_MODEL") or DEFAULT_MODEL
TAILOR_FALLBACKS = [name.strip() for name
                    in (os.environ.get("TAILOR_FALLBACK_MODELS") or DEFAULT_FALLBACKS).split(",")
                    if name.strip()]

JD_CHARS = 6000
MAX_ATTEMPTS = 2

# One click must always report an outcome. The workflow is killed at ten minutes
# and the Worker drops the task at fourteen, and a kill posts nothing at all,
# leaving "Preparing your tailored CV privately..." on screen forever. This
# budget spans every attempt and every retry, so the runner always gets to
# answer — even when the answer is that tailoring ran out of time.
BUDGET_SECONDS = 210
CALL_TIMEOUT = 60
MIN_CALL_SECONDS = 15
HTTP_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 6)
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
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
    "required": ["headline", "summary", "bullets", "gaps", "fit_notes"],
    "additionalProperties": False,
}


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
        "4. The headline may combine the candidate's current title with the job's title, "
        "and nothing else.\n"
        "5. The summary must be three sentences at most, drawn only from facts already in "
        "the resume.\n"
        "6. gaps: the job's real requirements that this resume does not evidence. Name the "
        "specific missing framework, platform, or credential. Never list ordinary English "
        "words. Three to five items, empty if there are genuinely none.\n"
        "7. fit_notes: the two or three strongest reasons this candidate matches, each "
        "naming concrete evidence.\n"
        f"8. This resume offers {achievements} achievement bullets. Keep about {target} of "
        "them — the strongest for this job — and drop the rest, so the result fits one "
        "page. Never return more than 16, and never pad the count with skills, education "
        "or language lines.\n\n"
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


def _retry_after_seconds(response) -> float | None:
    """The wait the server asked for, when it named one we can act on."""
    try:
        return max(0.0, float(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return None


def _plan_from_payload(payload: dict) -> dict:
    """Read the tailoring plan out of one OpenRouter response body.

    Every failure here names the model, because that string is what reaches the
    candidate in Discord and it is the only clue they get about which member of
    the chain answered.
    """
    error = payload.get("error")
    if error:
        # OpenRouter reports some upstream failures as a 200 with an error body.
        raise ValueError(f"OpenRouter error ({TAILOR_MODEL}): {str(error)[:200]}")
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError(f"OpenRouter returned no choices ({TAILOR_MODEL}).")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        reason = choices[0].get("finish_reason") or "unknown"
        raise ValueError(
            f"OpenRouter returned an empty answer ({TAILOR_MODEL}, finish_reason={reason})."
        )
    return json.loads(content)


def call_model(prompt: str, api_key: str, deadline: float) -> dict:
    """One tailoring plan from the model chain, or raise for the caller.

    OpenRouter walks ``models`` itself when a member is missing, rate-limited or
    erroring, which is exactly what the old single-model call could not do: a
    404 on the model name or a 429 on the quota ended the whole attempt. The
    loop below covers the remaining case, where the entire chain is briefly
    unavailable, and never runs past the caller's deadline.
    """
    body = {
        "model": TAILOR_MODEL,
        "models": [TAILOR_MODEL, *TAILOR_FALLBACKS],
        "messages": [{"role": "user", "content": prompt}],
        # Not every member of the chain accepts temperature, so seed carries the
        # determinism this stage wants and temperature is a best-effort hint.
        "temperature": 0.25,
        "seed": 7,
        "reasoning": {"effort": "low"},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tailoring_plan",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
        # The prompt carries the master resume, name and contact details
        # included, so routing must skip providers that retain prompts. It must
        # also skip endpoints that would ignore the schema and answer in prose,
        # which is what require_parameters enforces.
        "provider": {"data_collection": "deny", "require_parameters": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/abdullah7041/job-search-digest",
        "X-Title": "job-search-digest",
    }

    last_error = ""
    for attempt in range(HTTP_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining < MIN_CALL_SECONDS:
            raise ValueError(
                "Tailoring ran out of its time budget before the model answered. "
                f"Last problem: {last_error or 'none'}"
            )
        retry_after = None
        try:
            response = requests.post(ENDPOINT, json=body, headers=headers,
                                     timeout=min(CALL_TIMEOUT, remaining))
        except requests.RequestException as error:
            last_error = f"OpenRouter request failed ({TAILOR_MODEL}): {error}"
        else:
            if response.status_code == 200:
                return _plan_from_payload(response.json())
            last_error = (f"OpenRouter HTTP {response.status_code} ({TAILOR_MODEL}): "
                          f"{response.text[:200]}")
            if response.status_code not in RETRY_STATUSES:
                raise ValueError(last_error)
            retry_after = _retry_after_seconds(response)

        if attempt == HTTP_ATTEMPTS - 1:
            break
        wait = BACKOFF_SECONDS[attempt] if retry_after is None else retry_after
        if wait > deadline - time.monotonic() - MIN_CALL_SECONDS:
            # Sleeping this long would leave no room to use the result.
            break
        time.sleep(wait)
    raise ValueError(last_error)


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
        # Skills, education and languages are credentials, not achievements:
        # a role-specific edit would drop qualifications the reader expects.
        if section["heading"] in ("EXPERIENCE", "PROJECTS"):
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
    kept = {item.get("ref") for item in plan.get("bullets", [])}
    available = {bullet["ref"] for section in document["sections"]
                 for entry in section["entries"] for bullet in entry["bullets"]}
    available |= {line["ref"] for section in document["sections"] for line in section["lines"]}
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


def tailor(document: dict, snapshot: dict, api_key: str, sources: dict[str, str],
           master_text: str) -> tuple[dict, list[str], list[str]]:
    """Return the tailored document, its gaps and its fit notes.

    Raises ValueError if the model cannot produce an evidence-clean answer, so
    the caller can fall back to sending the untouched master resume.
    """
    allowed = vocabulary(master_text, snapshot.get("title", ""), snapshot.get("company", ""))
    corrections: list[str] = []
    last_errors: list[str] = []
    # One budget spans both attempts and all their retries, so a slow chain
    # cannot outlive the workflow that is waiting to report an outcome.
    deadline = time.monotonic() + BUDGET_SECONDS

    for _ in range(MAX_ATTEMPTS):
        plan = call_model(build_prompt(document, snapshot, corrections), api_key, deadline)
        checked = {
            "summary": plan.get("summary", ""),
            "bullets": [{"text": item["text"], "source_refs": [item["ref"]]}
                        for item in plan.get("bullets", [])],
        }
        errors = validate_tailored_resume(checked, sources, allowed, master_text)
        errors += substance_failures(plan, document)
        if not errors:
            return apply_plan(document, plan), clean_gaps(plan, document, snapshot), \
                [str(note) for note in plan.get("fit_notes", [])][:3]
        last_errors = errors
        corrections = errors

    raise ValueError("The tailored draft failed the evidence check: " + "; ".join(last_errors[:3]))
