"""Bounded public reads of verified official target sources; existing ATS stay intact."""
import datetime
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests

from jd_fetch import bounded_get, _page_job_postings, strip_html


TARGET_COMPANIES = json.loads(Path(__file__).with_name("target_companies.json").read_text(encoding="utf-8"))
AMAZON_PAGE_LIMIT = 3
RSS_ITEM_LIMIT = 30


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _official(url, company):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in company["canonical_hosts"]
            and not parsed.username and not parsed.password and parsed.port in (None, 443))


def _structured_job(posting, url, company, discovery_url):
    """Preserve structured employer/type evidence and reject unstated/non-Saudi locations."""
    url = _text(posting.get("url")) or url
    employer = posting.get("hiringOrganization")
    employer = _text(employer.get("name")) if isinstance(employer, dict) else ""
    title = _text(posting.get("title"))
    if not title or not employer or not _official(url, company):
        return None
    locations = posting.get("jobLocation") or []
    locations = locations if isinstance(locations, list) else [locations]
    saudi_locations = []
    for location in locations:
        address = location.get("address") if isinstance(location, dict) else None
        if not isinstance(address, dict):
            return None
        country = address.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        # Mixed-country listings cannot imply eligibility to work from Saudi Arabia.
        if _text(country).casefold() not in {"sa", "sau", "saudi arabia"}:
            return None
        city = _text(address.get("addressLocality"))
        saudi_locations.append((city + ", " if city else "") + "Saudi Arabia")
    if not saudi_locations:
        return None
    identifier = posting.get("identifier")
    identifier = identifier.get("value") if isinstance(identifier, dict) else identifier
    identifier = str(identifier) if isinstance(identifier, (str, int)) else ""
    employment = posting.get("employmentType") or ""
    if not isinstance(employment, str) and not (
            isinstance(employment, list) and all(isinstance(v, str) for v in employment)):
        raise ValueError("malformed employmentType")
    return {"company": employer, "target_company": company["name"], "id": identifier or url,
            "requisition_id": identifier, "title": title, "location": "; ".join(dict.fromkeys(saudi_locations)),
            "url": url, "first_published": _text(posting.get("datePosted")),
            "description": strip_html(_text(posting.get("description"))), "employment_type": employment,
            "source": "target", "token": company["token"], "discovery_url": discovery_url}


def fetch_target(company, *, get=bounded_get, log=print, source_failure=None):
    """Return official Saudi records; report blocked, malformed and partial reads.

    ``company`` may be a minimal COMPANIES entry with a verified registry token.
    Unknown employment type is retained for the existing preference gate to hold.
    """
    company = next((c for c in TARGET_COMPANIES if c["token"] == company.get("token")), company)
    out = []

    def fail(message):
        message = f"WARN {company['name']}: {message}"
        if source_failure:
            source_failure(company["name"], message)
        else:
            log(message)

    def read(url):
        if not _official(url, company):
            raise ValueError("URL is outside the verified official hosts")
        response = get(url, timeout=30)
        if response.status_code != 200:
            raise ValueError(f"official source HTTP {response.status_code}")
        if getattr(response, "truncated", False):
            raise ValueError("official source response was truncated")
        if not _official(getattr(response, "url", "") or url, company):
            raise ValueError("official source redirected outside verified hosts")
        return response

    try:
        source_format = company["format"]
        if source_format == "existing_ats":
            fail("use the existing verified ATS adapter")
            return []
        listing_url = company.get("listing_url") or company["official_url"]
        if source_format == "amazon_json":
            received_ids = set()
            for page in range(AMAZON_PAGE_LIMIT):
                url = listing_url + (f"&offset={page * 100}" if page else "")
                payload = read(url).json()
                if (not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list)
                        or type(payload.get("hits")) is not int or payload["hits"] < 0
                        or len(payload["jobs"]) > 100):
                    raise ValueError("malformed Amazon search payload")
                duplicates = False
                for raw in payload["jobs"]:
                    if not isinstance(raw, dict):
                        raise ValueError("malformed Amazon job record")
                    identifier = raw.get("id_icims") or raw.get("id")
                    if not isinstance(identifier, (str, int)) or not str(identifier):
                        raise ValueError("Amazon result lacks an identity; coverage incomplete")
                    if str(identifier) in received_ids:
                        duplicates = True
                        continue
                    received_ids.add(str(identifier))
                    if raw.get("country_code") != "SAU":
                        continue
                    path = _text(raw.get("job_path"))
                    job_url = urljoin(listing_url, path)
                    title, employer = _text(raw.get("title")), _text(raw.get("company_name"))
                    if (not title or not employer or not isinstance(identifier, (str, int))
                            or not path.startswith("/en/jobs/") or not _official(job_url, company)):
                        raise ValueError("malformed or untrusted Amazon job record")
                    try:
                        published = datetime.datetime.strptime(_text(raw.get("posted_date")), "%B %d, %Y").date().isoformat()
                    except ValueError:
                        published = ""
                    description = "\n\n".join(strip_html(_text(raw.get(key))) for key in (
                        "description", "basic_qualifications", "preferred_qualifications") if _text(raw.get(key)))
                    out.append({"company": employer, "target_company": company["name"], "id": str(identifier),
                                "requisition_id": str(identifier), "title": title,
                                "location": (_text(raw.get("city")) + ", " if _text(raw.get("city")) else "") + "Saudi Arabia",
                                "url": job_url, "first_published": published, "description": description,
                                "employment_type": _text(raw.get("job_schedule_type")), "source": "target",
                                "token": company["token"], "discovery_url": url})
                if duplicates or len(received_ids) > payload["hits"]:
                    raise ValueError("Amazon results duplicate or contradict advertised count; coverage incomplete")
                if len(received_ids) == payload["hits"]:
                    break
                if len(payload["jobs"]) < 100:
                    raise ValueError("Amazon search ended before advertised result count; coverage incomplete")
            else:
                fail(f"Amazon search exceeds {AMAZON_PAGE_LIMIT * 100} results; coverage incomplete")
        elif source_format == "teamtailor_rss":
            root = ET.fromstring(read(listing_url).text)
            if root.tag != "rss" or root.find("channel") is None:
                raise ValueError("malformed official RSS feed")
            items = root.findall("./channel/item")
            if len(items) > RSS_ITEM_LIMIT:
                fail(f"RSS exceeds {RSS_ITEM_LIMIT} items; coverage incomplete")
            for item in items[:RSS_ITEM_LIMIT]:
                url = _text(item.findtext("link"))
                title = _text(item.findtext("title"))
                identifier = re.search(r"/jobs/(\d+)(?:-|$)", urlsplit(url).path)
                if not title or not identifier or not _official(url, company):
                    raise ValueError("malformed or untrusted RSS job record")
                locations = item.findall("./{*}locations/{*}location")
                if not locations or any(_text(loc.findtext("{*}country")).casefold()
                                        not in {"sa", "sau", "saudi arabia"} for loc in locations):
                    continue
                cities = [_text(loc.findtext("{*}city")) for loc in locations]
                try:
                    published = parsedate_to_datetime(_text(item.findtext("pubDate"))).isoformat()
                except (ValueError, TypeError):
                    published = ""
                out.append({"company": company["name"], "target_company": company["name"],
                            "employer_verified": False, "id": identifier.group(1),
                            "requisition_id": identifier.group(1), "title": title,
                            "location": "; ".join(dict.fromkeys(
                                (city + ", " if city else "") + "Saudi Arabia" for city in cities)),
                            "url": url, "first_published": published,
                            "description": strip_html(_text(item.findtext("description"))),
                            "employment_type": "", "source": "target", "token": company["token"],
                            "discovery_url": listing_url})
        else:
            postings = list(_page_job_postings(read(listing_url).text))
            if not postings:
                raise ValueError("official page has no machine-readable JobPosting listings; coverage incomplete")
            # Search-page metadata may expose a subset, so never call this a complete board scan.
            fail("official page contains JobPosting records; listing completeness is unverified")
            for posting in postings[:100]:
                job = _structured_job(posting, "", company, listing_url)
                if job:
                    out.append(job)
        log(f"{company['name']}: {len(out)} Saudi jobs (official target source)")
    except (requests.RequestException, ValueError, ET.ParseError, KeyError, TypeError) as exc:
        fail(f"official source unavailable or invalid: {exc}")
    for job in out:
        job["id"] = f"{company['token']}:{job['id']}"
    return list({job["url"]: job for job in out}.values())
