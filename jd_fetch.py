"""Get the real job description out of a posting URL.

An aggregator card hands over a title and a link. The link usually leads
somewhere that *does* publish the whole job description — it is just not in the
body text a tag-stripper would find. Two places are worth looking before giving
up on a page:

1. The ``JobPosting`` block in ``application/ld+json``. Google for Jobs indexes
   from it, so a board that wants to be indexed publishes one, and it holds the
   description in full even when the visible page is a login wall.
2. The employer's own ATS. Jooble and JSearch mostly relist a posting that lives
   on Greenhouse, Lever, Ashby, Workable or Pinpoint, and those APIs are the
   same ones this project already reads for its tracked companies. Hopping to
   the source is both more complete and more stable than scraping the relister.

Only when both miss does this fall back to reading the page as text, which is
what the whole last-resort path used to be.

Nothing here raises: a failure returns "" and the caller degrades exactly as it
did before. ``get`` is injectable throughout so the tests never touch a network.
"""

from __future__ import annotations

import html
import json
import re

import requests

from job_archive import jd_completeness_failures


USER_AGENT = "job-digest/1.0 (personal job search)"
FETCH_TIMEOUT = 30
JD_LIMIT = 6000

# A page this project fetches is either an ATS JSON payload or a posting page
# whose description is truncated to JD_LIMIT anyway, so nothing legitimate comes
# close to this. The cap exists because the URLs come from an aggregator relaying
# third-party sites: requests' timeout is per read, not per transfer, so an
# oversized or slow-drip body is read until the 10-minute workflow timeout kills
# the whole digest. Four megabytes is far above any real posting page.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class BoundedResponse:
    """A response whose body was read only up to MAX_RESPONSE_BYTES.

    Exposes exactly what this project's callers use -- status_code, text,
    json(), url and history -- so nothing downstream changes. A truncated body
    is not an error: a posting page cut short simply fails the completeness gate
    the same way a bot-blocked one does, which is the fail-soft outcome the rest
    of the pipeline is built around.
    """

    def __init__(self, response, content: bytes, truncated: bool):
        self.status_code = response.status_code
        self.url = response.url
        self.history = response.history
        self.headers = response.headers
        self.encoding = response.encoding
        self.content = content
        self.truncated = truncated

    @property
    def text(self) -> str:
        # requests would sniff an undeclared charset here; without the whole body
        # that sniffing is unreliable, so an undeclared page is read as UTF-8 and
        # undecodable bytes become replacement characters rather than an
        # exception. A page whose text is mangled fails the completeness gate.
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self):
        # json.loads raises ValueError on bad input, which is what every caller
        # already catches -- requests' own JSONDecodeError is a ValueError too.
        return json.loads(self.text)


def bounded_get(url, *, timeout: int = FETCH_TIMEOUT,
                limit: int = MAX_RESPONSE_BYTES) -> BoundedResponse:
    """GET a URL, reading at most ``limit`` bytes of its body."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=timeout, stream=True)
    try:
        chunks, total = [], 0
        for chunk in response.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        body = b"".join(chunks)
    finally:
        response.close()
    return BoundedResponse(response, body[:limit], total >= limit)


def default_get(url):
    return bounded_get(url)


def strip_html(s):
    """Escaped or raw HTML -> readable plain text for LLM prompts. Greenhouse
    double-escapes its content field (&lt;p&gt;), so unescape twice-safe first,
    then drop tags and collapse whitespace."""
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(br|/p|/li|/h[1-6])[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


# --------------------------- structured data --------------------------------

JSONLD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)


def _job_postings(node):
    """Every JobPosting object anywhere inside one parsed JSON-LD document.

    Boards nest it differently: some publish it bare, some wrap it in an
    ``@graph``, some bury it under ``mainEntity``. Walking the whole tree costs
    nothing and stops the shape of the wrapper from mattering.
    """
    if isinstance(node, list):
        for item in node:
            yield from _job_postings(item)
        return
    if not isinstance(node, dict):
        return
    declared = node.get("@type")
    declared = declared if isinstance(declared, list) else [declared]
    if any(isinstance(name, str) and name.lower() == "jobposting" for name in declared):
        yield node
    for value in node.values():
        if isinstance(value, (list, dict)):
            yield from _job_postings(value)


def jsonld_job_posting(page_html: str) -> str:
    """The description from the page's JobPosting metadata, or ""."""
    for match in JSONLD_RE.finditer(page_html or ""):
        raw = match.group(1).strip()
        # A few boards HTML-escape the block they embed, so try both readings.
        for candidate in (raw, html.unescape(raw)):
            try:
                document = json.loads(candidate)
            except ValueError:
                continue
            for posting in _job_postings(document):
                text = strip_html(posting.get("description") or "")
                if text:
                    return text
            break
    return ""


# ------------------------------- ATS hop ------------------------------------

# Matched against the posting URL itself and against every href on the page.
# Each pattern captures the board token and the posting id, which is everything
# the corresponding API needs.
ATS_PATTERNS = (
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{36})")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)/j/([A-Za-z0-9]+)")),
    ("pinpoint", re.compile(r"([a-z0-9-]+)\.pinpointhq\.com/(?:[a-z]{2}/)?postings/([0-9a-fA-F-]{36})", re.I)),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)")),
    ("greenhouse", re.compile(
        r"greenhouse\.io/embed/job_app\?for=([A-Za-z0-9_-]+)&(?:amp;)?token=(\d+)")),
)

HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)


def ats_reference(page_html: str, url: str) -> dict | None:
    """The ATS posting this URL is, or that this page links out to."""
    candidates = [url or ""]
    candidates.extend(html.unescape(href) for href in HREF_RE.findall(page_html or ""))
    for candidate in candidates:
        for source, pattern in ATS_PATTERNS:
            match = pattern.search(candidate)
            if match:
                reference = {"source": source, "token": match.group(1), "id": match.group(2)}
                # Greenhouse is the one board whose front end is region-split.
                # ATS_PATTERNS matches the EU host on purpose, so the region has
                # to survive into the reference or ats_url rebuilds an
                # EU-hosted posting on the global host, where it 404s.
                if ".eu.greenhouse.io" in match.group(0):
                    reference["host"] = "job-boards.eu.greenhouse.io"
                return reference
    return None


# The inverse of ATS_PATTERNS: a reference back to the employer's canonical
# posting URL. This is the link a candidate should be given -- an aggregator
# card is a rendering of the posting, the employer's board is the posting.
ATS_URL_BUILDERS = {
    "greenhouse": "https://{host}/{token}/jobs/{id}",
    "lever": "https://jobs.lever.co/{token}/{id}",
    "ashby": "https://jobs.ashbyhq.com/{token}/{id}",
    "workable": "https://apply.workable.com/{token}/j/{id}/",
    "pinpoint": "https://{token}.pinpointhq.com/postings/{id}",
}

# Only Greenhouse reads {host}; the rest ignore the keyword. A reference that
# carries no host is a board on the global front end, which is every board the
# embed pattern matches and every non-EU one the board pattern does.
ATS_DEFAULT_HOSTS = {"greenhouse": "job-boards.greenhouse.io"}


def ats_url(reference: dict | None) -> str:
    """The employer's own posting URL for a reference, or "" if unbuildable."""
    if not reference:
        return ""
    source = reference.get("source") or ""
    template = ATS_URL_BUILDERS.get(source)
    if not template or not reference.get("token") or not reference.get("id"):
        return ""
    return template.format(token=reference["token"], id=reference["id"],
                           host=reference.get("host") or ATS_DEFAULT_HOSTS.get(source, ""))


def _json_or_none(response):
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def ats_description(reference: dict, get=default_get) -> str:
    """The full JD straight from the employer's ATS.

    Endpoints and field names are the ones the daily fetchers already use, so
    anything verified live for a tracked company holds here too.
    """
    source, token, posting_id = reference["source"], reference["token"], reference["id"]
    try:
        if source == "greenhouse":
            payload = _json_or_none(
                get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{posting_id}?questions=false"))
            return strip_html((payload or {}).get("content") or "")

        if source == "lever":
            payload = _json_or_none(get(f"https://api.lever.co/v0/postings/{token}?mode=json"))
            for job in payload or []:
                if str(job.get("id")) == posting_id:
                    return (job.get("descriptionPlain")
                            or strip_html(job.get("description") or "")).strip()
            return ""

        if source == "ashby":
            payload = _json_or_none(
                get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"))
            for job in (payload or {}).get("jobs", []):
                if str(job.get("id")) == posting_id:
                    return (job.get("descriptionPlain")
                            or strip_html(job.get("descriptionHtml") or "")).strip()
            return ""

        if source == "workable":
            payload = _json_or_none(
                get(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"))
            for job in (payload or {}).get("jobs", []):
                if str(job.get("shortcode")) == posting_id:
                    return strip_html(job.get("description") or "").strip()
            return ""

        if source == "pinpoint":
            payload = _json_or_none(get(f"https://{token}.pinpointhq.com/postings.json"))
            for job in (payload or {}).get("data", []):
                if str(job.get("id")) == posting_id:
                    return "\n".join(strip_html(job.get(field) or "") for field in (
                        "description", "key_responsibilities", "skills_knowledge_expertise")).strip()
            return ""
    except (requests.RequestException, ValueError):
        return ""
    return ""


# ------------------------------ page text -----------------------------------

def readable_text(page_html: str) -> str:
    """The page with its chrome removed, as plain text."""
    body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page_html or "")
    return strip_html(body)


def extract_posting(url: str, get=default_get, limit: int = JD_LIMIT, log=None) -> dict:
    """What one posting URL can be made to give up: {"text", "employer_url"}.

    ``text`` is the best COMPLETE job description, or "" -- an incomplete one
    scores and tailors worse than nothing, because everything downstream treats
    captured text as evidence. ``jd_completeness_failures`` is the same gate the
    snapshot uses, so anything returned here is tailorable.

    ``employer_url`` is the employer's own posting when the page names one. It
    costs no extra request: the ATS hop below was already being made for the
    description and its result was simply thrown away, leaving the candidate
    with the relister's link to a posting we had just read from the source.
    """
    def note(message):
        if log:
            log(message)

    found = {"text": "", "employer_url": "", "employer_token": ""}
    if not url:
        return found

    page_html = ""
    try:
        response = get(url)
        if response.status_code == 200:
            page_html = response.text
        else:
            note(f"WARN JD page HTTP {response.status_code} for {url}")
    except (requests.RequestException, ValueError) as error:
        note(f"WARN JD page fetch failed for {url}: {error}")

    # Resolved up front rather than inside the description branch below, so a
    # page that publishes usable structured data still gives up its employer
    # link. Pure regex over HTML already in hand -- no request, no failure mode.
    # `ats_reference` matches any ATS-shaped href on the page, and a relister's
    # "people also viewed" rail is full of them, so the token is reported
    # alongside the URL: the caller knows which employer it asked about and is
    # the only one that can refuse a link naming a different one.
    reference = ats_reference(page_html, url)
    found["employer_url"] = ats_url(reference)
    found["employer_token"] = (reference or {}).get("token") or ""

    text = jsonld_job_posting(page_html)
    if text and not jd_completeness_failures(text):
        note(f"JD read from structured data for {url}")
        found["text"] = text[:limit]
        return found

    # The relister did not publish it, but it may have linked to whoever did.
    # This runs even when the page fetch failed, because an aggregator URL is
    # often already an ATS posting. The employer's own API outranks the page
    # body: it is the posting rather than a rendering of it.
    if reference:
        text = ats_description(reference, get)
        if text and not jd_completeness_failures(text):
            note(f"JD read from {reference['source']} for {url}")
            found["text"] = text[:limit]
            return found

    text = readable_text(page_html) if page_html else ""
    if text and not jd_completeness_failures(text):
        note(f"JD read from page text for {url}")
        found["text"] = text[:limit]
        return found

    note(f"WARN no complete job description found at {url}")
    return found


def extract_jd(url: str, get=default_get, limit: int = JD_LIMIT, log=None) -> str:
    """The best complete job description this URL can be made to give up."""
    return extract_posting(url, get=get, limit=limit, log=log)["text"]


# -------------------------------- images ------------------------------------

# Gemini reads a photographed posting well and is already the scoring chain, so
# no new provider relationship is needed. Every member must be multimodal and
# must accept response_format, because one request body is sent to whichever
# serves the call.
DEFAULT_VISION_MODEL = "google/gemini-3.7-flash"
DEFAULT_VISION_FALLBACKS = "google/gemini-3.6-flash,openai/gpt-5.6-luna"

VISION_PROVIDER = {"data_collection": "deny", "require_parameters": True}

OCR_SCHEMA = {
    "type": "object",
    "properties": {"job_description": {"type": "string"}},
    "required": ["job_description"],
    "additionalProperties": False,
}

OCR_INSTRUCTION = (
    "These images are screenshots of one job posting. Transcribe the job description "
    "from them into plain text, in reading order, keeping every heading, responsibility "
    "and requirement exactly as written. Do not summarise, do not reword, and do not add "
    "anything that is not visible in the images. If they do not show a job posting, "
    "return an empty string."
)


def jd_from_image(image_urls, api_key: str, deadline: float, limit: int = JD_LIMIT) -> str:
    """Read a job description out of screenshots of a posting.

    The one case the fetchers cannot reach: a posting behind a login wall that
    the candidate can see and this pipeline cannot. Held to the same
    completeness gate as every other source, so a blurry or partial screenshot
    is refused rather than tailored against.
    """
    from openrouter import chat_json, configured_chain

    model, fallbacks = configured_chain(
        "VISION_MODEL", "VISION_FALLBACK_MODELS",
        DEFAULT_VISION_MODEL, DEFAULT_VISION_FALLBACKS)

    if isinstance(image_urls, str):
        image_urls = [image_urls]
    if not image_urls:
        return ""

    content = [{"type": "text", "text": OCR_INSTRUCTION}]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)

    answer = chat_json(content, OCR_SCHEMA, "job_description", api_key,
                       model, fallbacks, deadline, provider=VISION_PROVIDER)
    text = re.sub(r"[ \t]+", " ", (answer.get("job_description") or "")).strip()
    if not text or jd_completeness_failures(text):
        return ""
    return text[:limit]
