"""Portails ATS publics : offres et adresses publiées, sans requête par offre."""

import json
import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ...connectors import api_provider, as_job_page, detail_links, endpoints
from ...extraction import EMAIL, valid_email
from ...models import web_url
from ...pages import FetchError, Page
from ...verification import normalized
from ..connector import Connector, Item, Outcome, SourceUnavailable
from ..keys import domain_key, email_key


def detect_ats(url):
    """Reconnaît un hôte ATS exact et retourne ses coordonnées publiques."""
    if not web_url(url):
        return None
    parts = urlsplit(url)
    if parts.port not in (None, 443):
        return None
    candidates = endpoints(Page(url, ""))
    if api_provider(url):
        candidates.insert(0, url)
    for api_url in candidates:
        provider = api_provider(api_url)
        if provider not in {cls.name for cls in ATS_CONNECTORS}:
            continue
        api = urlsplit(api_url)
        segments = api.path.strip("/").split("/")
        if provider == "recruitee":
            board = api.hostname.split(".")[0]
            public_url = f"https://{board}.recruitee.com"
        elif provider == "ashby":
            board = segments[-1]
            public_url = f"https://jobs.ashbyhq.com/{board}"
        else:
            if len(segments) != (3 if provider == "lever" else 4):
                continue
            board = segments[2]
            host = {
                "greenhouse": "boards.greenhouse.io",
                "lever": "jobs.eu.lever.co"
                if api.hostname == "api.eu.lever.co"
                else "jobs.lever.co",
                "smartrecruiters": "careers.smartrecruiters.com",
            }[provider]
            public_url = f"https://{host}/{board}"
        query = dict(parse_qsl(api.query))
        if provider == "greenhouse":
            query["content"] = "true"
        if provider == "smartrecruiters":
            query.pop("q", None)
        api_url = urlunsplit((api.scheme, api.netloc, api.path, urlencode(query), ""))
        return dict(provider=provider, board=board, api_url=api_url, public_url=public_url)
    return None


def _same_employer(left, right):
    """Compare les noms normalisés et leurs sigles, sans forme juridique."""
    legal = {"sa", "sas", "sasu", "sarl", "ltd", "inc", "gmbh"}
    left, right = (
        [w for w in normalized(name).split() if w not in legal] for name in (left, right)
    )
    a, b = "".join(left), "".join(right)
    return a == b or (
        len(a) >= 2
        and len(b) >= 2
        and (a == "".join(w[0] for w in right) or b == "".join(w[0] for w in left))
    )


class AtsConnector(Connector):
    phase = "contacts"
    accepts = ("ats",)
    min_interval = 1.0

    def wants(self, ctx, lead):
        return lead.kind in self.accepts and lead.data.get("provider") == self.name

    def run(self, ctx, lead):
        detected = detect_ats(lead.url)
        if not detected or detected["provider"] != self.name:
            return Outcome(error="portail ATS non reconnu")
        api_url = lead.data.get("api_url") or detected["api_url"]
        target = detect_ats(api_url)
        if not target or any(target[k] != detected[k] for k in ("provider", "board", "public_url")):
            return Outcome(error="API incompatible avec le portail")
        api_url = target["api_url"]
        public_url = detected["public_url"]
        pending, visited, offers = [api_url], set(), set()
        outcome = Outcome()
        while pending and outcome.queries < 3:
            ctx.check()
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)
            outcome.queries += 1
            try:
                page = ctx.web.fetch_api(url)
            except FetchError as exc:
                message = str(exc)
                if re.search(r"\b429\b", message):
                    raise SourceUnavailable(message) from exc
                if re.search(r"\b403\b", message):
                    self._forbidden = getattr(self, "_forbidden", 0) + 1
                    if self._forbidden >= 2:
                        raise SourceUnavailable(message) from exc
                outcome.error = "board introuvable" if re.search(r"\b404\b", message) else message
                return outcome
            self._forbidden = 0
            try:
                converted = as_job_page(page, ctx.company)
                jobs = json.loads(
                    converted.soup.select_one('script[type="application/ld+json"]').string
                )
                # Seules les pages du même listing sont suivies ; jamais les fiches individuelles.
                pending.extend(
                    link
                    for link in detail_links(page)
                    if urlsplit(link)[:3] == urlsplit(api_url)[:3]
                    and api_provider(link) == self.name
                    and link not in visited
                )
            except (ValueError, TypeError, AttributeError) as exc:
                outcome.error = f"réponse ATS invalide : {exc}"
                return outcome
            if not outcome.items:
                outcome.items.append(
                    Item("portal", public_url, public_url, scope="rh", confidence="probable")
                )
            for job in jobs:
                offer_url = job["url"]
                if not web_url(offer_url) or offer_url in offers:
                    continue
                offers.add(offer_url)
                employer = job["hiringOrganization"]["name"]
                different = bool(employer and not _same_employer(employer, ctx.company.name))
                extra = dict(
                    location=job["jobLocation"]["address"]["addressLocality"], provider=self.name
                )
                if different:
                    extra["employer"] = employer
                outcome.items.append(
                    Item(
                        "offer",
                        offer_url,
                        offer_url,
                        scope="rh",
                        label=job["title"],
                        confidence="incertain" if different else "probable",
                        extra=extra,
                    )
                )
                text = BeautifulSoup(unescape(job["description"]), "html.parser").get_text(" ")
                for email in dict.fromkeys(email_key(e) for e in EMAIL.findall(text)):
                    if not email or not valid_email(email):
                        continue
                    third_party = domain_key(email) != domain_key(ctx.site_url)
                    outcome.items.append(
                        Item(
                            "email",
                            email,
                            offer_url,
                            scope="rh",
                            confidence="incertain" if third_party or different else "probable",
                            extra={"third_party": True} if third_party else {},
                        )
                    )
        return outcome


class GreenhouseConnector(AtsConnector):
    name = "greenhouse"


class LeverConnector(AtsConnector):
    name = "lever"


class SmartRecruitersConnector(AtsConnector):
    name = "smartrecruiters"


class AshbyConnector(AtsConnector):
    name = "ashby"


class RecruiteeConnector(AtsConnector):
    name = "recruitee"


ATS_CONNECTORS = [
    GreenhouseConnector,
    LeverConnector,
    SmartRecruitersConnector,
    AshbyConnector,
    RecruiteeConnector,
]
