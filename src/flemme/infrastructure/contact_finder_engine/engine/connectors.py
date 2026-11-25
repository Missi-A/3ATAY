"""Adaptateurs GET publics documentés ; aucun endpoint de candidature n'est appelé."""

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from .pages import Page, link_base
from .verification import normalized

DOCUMENTATION = {
    "greenhouse": "https://docs.greenhouse.io/job-board",
    "lever": "https://github.com/lever/postings-api",
    "smartrecruiters": "https://developers.smartrecruiters.com/reference/v1listpostings",
    "ashby": "https://developers.ashbyhq.com/docs/public-job-posting-api",
    "recruitee": "https://docs.recruitee.com/reference/offers",
    "lba": "https://api.apprentissage.beta.gouv.fr/fr/documentation-technique",
    "wikidata": "https://www.wikidata.org/wiki/Wikidata:Data_access",
}


def api_provider(url):
    parts = urlsplit(url)
    if (
        parts.scheme == "https"
        and parts.hostname == "www.wikidata.org"
        and parts.path == "/w/api.php"
        and parse_qs(parts.query).get("action", []) in (["wbsearchentities"], ["wbgetentities"])
    ):
        return "wikidata"
    if (
        parts.scheme == "https"
        and parts.hostname == "query.wikidata.org"
        and parts.path == "/sparql"
    ):
        return "wikidata"  # service SPARQL public et gratuit (lecture seule)
    rules = {
        "boards-api.greenhouse.io": ("greenhouse", r"/v1/boards/[\w-]+/jobs(?:/\d+)?"),
        "api.lever.co": ("lever", r"/v0/postings/[\w.-]+(?:/[\w-]+)?"),
        "api.eu.lever.co": ("lever", r"/v0/postings/[\w.-]+(?:/[\w-]+)?"),
        "api.smartrecruiters.com": (
            "smartrecruiters",
            r"/v1/companies/[\w-]+/postings(?:/[\w-]+)?",
        ),
        "api.apprentissage.beta.gouv.fr": ("lba", r"/api/job/v1/search"),
        "api.ashbyhq.com": ("ashby", r"/posting-api/job-board/[\w-]+"),
    }
    if (
        parts.scheme == "https"
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.recruitee\.com", parts.hostname or "")
        and parts.path == "/api/offers/"
        and not parts.username
        and not parts.password
        and parts.netloc.lower() == parts.hostname
    ):
        return "recruitee"
    rule = rules.get(parts.hostname)
    if rule and rule[0] == "ashby" and parts.netloc.lower() != "api.ashbyhq.com":
        return ""
    if parts.hostname == "api.smartrecruiters.com" and parse_qs(parts.query).get(
        "destination", ["PUBLIC"]
    ) != ["PUBLIC"]:
        return ""
    return rule[0] if parts.scheme == "https" and rule and re.fullmatch(rule[1], parts.path) else ""


def endpoints(page):
    urls = [page.url]
    urls.extend(urljoin(link_base(page), a["href"]) for a in page.soup.select("a[href]"))
    urls.extend(
        urljoin(link_base(page), a["src"])
        for a in page.soup.select("iframe[src]")
        if "careers.flatchr.io/company/" in a["src"]
    )
    result = []
    for url in urls:
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        if (
            parts.hostname
            in (
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "boards.eu.greenhouse.io",
                "job-boards.eu.greenhouse.io",
            )
            and segments
        ):
            result.append(
                "https://boards-api.greenhouse.io/v1/boards/"
                + quote(segments[0])
                + "/jobs?content=false"
            )
        elif (
            parts.hostname == "careers.flatchr.io"
            and parts.path.startswith("/company/")
            and url != page.url
        ):
            result.append(url)
        elif parts.hostname in ("jobs.lever.co", "jobs.eu.lever.co") and segments:
            api = "api.eu.lever.co" if ".eu." in parts.hostname else "api.lever.co"
            result.append(
                f"https://{api}/v0/postings/{quote(segments[0])}?mode=json&limit=50&skip=0"
            )
        elif parts.hostname == "jobs.ashbyhq.com" and segments:
            result.append("https://api.ashbyhq.com/posting-api/job-board/" + quote(segments[0]))
        elif re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.recruitee\.com", parts.hostname or ""):
            result.append(f"https://{parts.hostname}/api/offers/")
        elif (
            parts.hostname in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com")
            and segments
        ):
            result.append(
                "https://api.smartrecruiters.com/v1/companies/"
                + quote(segments[0])
                + "/postings?destination=PUBLIC&q=alternance&limit=50&offset=0"
            )
    return list(dict.fromkeys(result))


def as_job_page(page, company):
    """Convertit seulement les formats connus en JobPosting, avec l'employeur publié."""
    provider = api_provider(page.url)
    if not provider:
        return page
    data = json.loads(page.body)
    records = (
        data
        if isinstance(data, list)
        else data.get("jobs", data.get("offers", data.get("content", [])))
    )
    if provider in ("ashby", "recruitee") and not isinstance(records, list):
        raise ValueError("liste d'offres ATS invalide")
    if not isinstance(records, list):
        records = [data] if data.get("id") else []
    elif not records and isinstance(data, dict) and data.get("id"):
        records = [data]
    jobs = []
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("active") is False
            or record.get("visibility", "PUBLIC") != "PUBLIC"
            or provider == "ashby"
            and record.get("isListed") is False
            or provider == "recruitee"
            and record.get("status", "published") != "published"
        ):
            continue
        custom = {
            normalized(field.get("fieldLabel", "")): str(field.get("valueLabel") or "")
            for field in record.get("customField", [])
            if isinstance(field, dict)
        }
        employer = record.get("company") or record.get("company_name") or {}
        if not employer and isinstance(data, dict):
            employer = data.get("company") or data.get("company_name") or {}
        employer = employer.get("name", "") if isinstance(employer, dict) else str(employer)
        employer = custom.get("legal entity") or custom.get("entite juridique") or employer
        title = record.get("title") or record.get("text") or record.get("name") or ""
        url = (
            record.get("absolute_url")
            or record.get("hostedUrl")
            or record.get("ref")
            or record.get("postingUrl")
            or record.get("jobUrl")
            or record.get("careers_url")
            or ""
        )
        if provider == "smartrecruiters" and "/postings/" in str(url):
            url = (
                "https://jobs.smartrecruiters.com/"
                + str(
                    record.get("company", {}).get(
                        "identifier", urlsplit(page.url).path.split("/")[3]
                    )
                )
                + "/"
                + str(record.get("id", ""))
            )
        location = record.get("location") or record.get("city") or {}
        city = (
            location.get("city") or location.get("name", "")
            if isinstance(location, dict)
            else str(location)
        )
        categories = record.get("categories", {})
        if categories:
            city = categories.get("location", city)
        contract = record.get("typeOfEmployment", {})
        contract = (
            " ".join(str(contract.get(k) or "") for k in ("label", "id"))
            if isinstance(contract, dict)
            else str(contract)
        )
        contract = contract or categories.get("commitment", "")
        contract = custom.get("contract type") or custom.get("type de contrat") or contract
        description = (
            record.get("content")
            or record.get("descriptionPlain")
            or record.get("descriptionHtml")
            or record.get("description")
            or ""
        )
        if record.get("jobAd"):
            description = " ".join(
                str(s.get("text", ""))
                for s in record["jobAd"].get("sections", {}).values()
                if isinstance(s, dict)
            )
        created = (
            record.get("first_published")
            or record.get("releasedDate")
            or record.get("publishedAt")
            or ""
        )
        if not created and isinstance(record.get("createdAt"), (int, float)):
            try:
                created = datetime.fromtimestamp(
                    record["createdAt"] / 1000, timezone.utc
                ).isoformat()
            except (ValueError, OverflowError, OSError):
                created = ""
        jobs.append(
            {
                "@type": "JobPosting",
                "title": str(title),
                "description": str(description),
                "employmentType": contract,
                "url": url,
                "applicationUrl": record.get("applyUrl") or record.get("apply_url") or url,
                "identifier": str(record.get("id", "")),
                "datePosted": created,
                "validThrough": record.get("application_deadline") or "",
                "hiringOrganization": {"name": employer},
                "jobLocation": {"address": {"addressLocality": city}},
                "_publisher": provider,
            }
        )
    body = (
        '<script type="application/ld+json">' + json.dumps(jobs).replace("</", "<\\/") + "</script>"
    )
    return Page(page.url, body, "text/html", page.headers)


def detail_links(page):
    provider = api_provider(page.url)
    data = json.loads(page.body)
    parts = urlsplit(page.url)
    query = dict(parse_qsl(parts.query))
    links = []
    if provider == "greenhouse":
        for row in data.get("jobs", []):
            if re.search(r"alternan|apprenti|apprentice", row.get("title", ""), re.I):
                links.append(
                    urlunsplit(
                        (parts.scheme, parts.netloc, parts.path + "/" + str(row["id"]), "", "")
                    )
                )
    elif provider == "smartrecruiters":
        rows = data.get("content", [])
        if rows and int(data.get("totalFound", 0)) > int(query.get("offset", 0)) + len(rows):
            query["offset"] = int(query.get("offset", 0)) + len(rows)
            links.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))
        links += [row["ref"] for row in rows if api_provider(row.get("ref", ""))]
    elif (
        provider == "lever" and isinstance(data, list) and len(data) >= int(query.get("limit", 50))
    ):
        query["skip"] = int(query.get("skip", 0)) + len(data)
        links.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))
    return links
