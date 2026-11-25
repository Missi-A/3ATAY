"""Portails carrières publics : un flux RSS ou une page, sans appel par offre."""

import re
from html import unescape
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ...extraction import EMAIL, valid_email, visible_soup
from ...models import web_url
from ...pages import FetchError
from ...verification import normalized, same_employer
from ..connector import Connector, Item, Outcome, SourceUnavailable
from ..keys import domain_key, email_key


def detect_careers(url):
    """Reconnaît un portail exact, sans identifiants ni port non standard."""
    if not web_url(url):
        return None
    parts = urlsplit(url)
    if parts.port not in (None, 443) or "@" in parts.netloc:
        return None
    match = re.fullmatch(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.teamtailor\.com", parts.hostname)
    if match and match[1] not in {"www", "app"}:
        board = match[1]
        public_url = f"https://{board}.teamtailor.com/"
        return dict(
            provider="teamtailor",
            board=board,
            public_url=public_url,
            feed_url=public_url + "jobs.rss",
        )
    match = re.match(r"^/company/([a-zA-Z0-9_-]+)(?:/|$)", parts.path)
    if parts.hostname == "careers.flatchr.io" and match:
        board = match[1]
        return dict(
            provider="flatchr",
            board=board,
            public_url=f"https://careers.flatchr.io/company/{board}",
            feed_url="",
        )
    return None


def _teamtailor_cdn(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "teamtailor-cdn.com" or host.endswith(".teamtailor-cdn.com")


def _custom_domain(url, site_url):
    """Portail Teamtailor sur un domaine propre, reconstruit depuis l'URL : hôte rattaché au site
    retenu (même domaine enregistrable, même locataire d'un hébergement partagé)."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.port not in (None, 443)
        or "@" in parts.netloc
        or not site_url
        or domain_key(url) != domain_key(site_url)
    ):
        return None
    root = f"https://{parts.hostname}/"
    return dict(
        provider="teamtailor",
        board=parts.hostname,
        public_url=root,
        feed_url=root + "jobs.rss",
        custom_domain=True,
    )


def teamtailor_hosted(page, site_url):
    """Site carrières propulsé par Teamtailor sur un domaine de l'entreprise : même flux RSS
    public documenté, à la racine de ce domaine.

    Preuve exigée : le code ET la feuille de style de la page servis par le CDN Teamtailor (ce que
    charge un site carrières Teamtailor) ; une image, un lien ou un widget isolé ne suffit pas.
    La page finale (après redirection) doit appartenir au site retenu de l'entreprise."""
    if urlsplit(page.url).hostname and urlsplit(page.url).hostname.endswith(".teamtailor.com"):
        return detect_careers(page.url)
    scripts = any(
        _teamtailor_cdn(urljoin(page.base_url, s["src"])) for s in page.soup.select("script[src]")
    )
    styles = any(
        _teamtailor_cdn(urljoin(page.base_url, s["href"]))
        for s in page.soup.select('link[rel~="stylesheet"][href]')
    )
    return _custom_domain(page.url, site_url) if scripts and styles else None


def _from_lead(lead, site_url):
    """Portail reconnu par son URL, ou domaine propre reconstruit et rattaché au site retenu :
    les données portées par la piste ne valent jamais preuve."""
    found = detect_careers(lead.url)
    if found is None and (lead.data or {}).get("custom_domain"):
        found = _custom_domain(lead.url, site_url)
    return found


# Contrat nommé explicitement : vaut dans le titre comme dans la description.
CONTRACT = re.compile(
    r"contrat\s+d.apprentissage|contrat\s+de\s+professionnalisation|contrat\s+pro\b|"
    r"work[\s-]study|apprenticeship",
    re.I,
)
# Mot seul : ne vaut que dans le titre de l'offre.
ALTERNANCE = re.compile(r"\balternan\w*|\bapprenti\w*|\bapprentissage\b|\bcontrat\s+pro\b", re.I)
# Négations et métiers de la gestion de l'alternance : retirés avant la recherche.
NOT_CONTRACT = re.compile(
    r"\b(?:sans|pas\s+d.|hors|non)\s*(?:alternance|apprentissage|apprentis?)\b|"
    r"\b(?:responsable|charg[ée]e?s?|gestionnaire|coordinat\w+|r[ée]f[ée]rent\w*|conseill\w+)"
    r"\s+(?:de\s+l.|des\s+|d.|en\s+)?(?:alternance|apprentissage|apprentis)\b",
    re.I,
)


def is_alternance(title, description=""):
    """Offre en alternance : contrat explicite (titre ou description) ou mot dans le titre ;
    les négations et les postes qui gèrent l'alternance ne comptent pas."""
    title, description = (NOT_CONTRACT.sub(" ", t or "") for t in (title, description))
    return bool(ALTERNANCE.search(title) or CONTRACT.search(title) or CONTRACT.search(description))


def _emails(text, url, ctx):
    """Ne conserve que les adresses effectivement présentes dans le texte publié."""
    items = []
    for email in dict.fromkeys(email_key(e) for e in EMAIL.findall(text)):
        if not email or not valid_email(email):
            continue
        third_party = domain_key(email) != domain_key(ctx.site_url)
        items.append(
            Item(
                "email",
                email,
                url,
                scope="rh",
                confidence="incertain" if third_party else "probable",
                extra=dict(
                    rh_evidence=["publiée dans une offre d'emploi de l'entreprise"],
                    **({"third_party": True} if third_party else {}),
                ),
            )
        )
    return items


# Flux RSS Teamtailor : 100 offres par page (paramètres documentés), au plus 500 par portail.
PER_PAGE = 100
MAX_OFFERS = 500


class _CareersConnector(Connector):
    phase = "contacts"
    accepts = ("careers_portal",)
    min_interval = 1.0

    def wants(self, ctx, lead):
        return lead.kind in self.accepts and lead.data.get("provider") == self.name

    def run(self, ctx, lead):
        found = _from_lead(lead, ctx.site_url)
        if not found or found["provider"] != self.name:
            return Outcome(error="portail carrières non reconnu")
        url = (
            found["feed_url"] + f"?per_page={PER_PAGE}"
            if found["feed_url"]
            else found["public_url"]
        )
        ctx.check()
        outcome = Outcome(queries=1)
        try:
            page = ctx.web.fetch(url)
        except FetchError as exc:
            message = str(exc)
            if re.search(r"\b429\b|anti-robot|défi", message, re.I):
                raise SourceUnavailable(message) from exc
            if re.search(r"\b403\b", message):
                self._forbidden = getattr(self, "_forbidden", 0) + 1
                if self._forbidden >= 2:
                    raise SourceUnavailable(message) from exc
            outcome.error = "portail introuvable" if re.search(r"\b404\b", message) else message
            return outcome
        self._forbidden = 0
        # Même contrôle que Web, y compris pour les pages fournies par un cache ou un faux client.
        soup = (
            BeautifulSoup(page.body, "html.parser")
            if not found["feed_url"] or not re.search(r"<rss(?:\s|/?>)", page.body, re.I)
            else None
        )
        title = soup.title.get_text(" ", strip=True).lower() if soup and soup.title else ""
        if (
            soup and soup.select_one("#challenge-form, #challenge-running, #cf-challenge-running")
        ) or any(
            marker in title
            for marker in (
                "just a moment",
                "access denied",
                "verify you are human",
                "attention required",
                "captcha",
            )
        ):
            raise SourceUnavailable("Défi anti-robot ; exploration arrêtée.")
        if self.name == "teamtailor":
            try:
                channel = ET.fromstring(page.body).find("channel")
            except ET.ParseError as exc:
                return Outcome(queries=1, error=f"flux RSS invalide : {exc}")
            if channel is None:
                return Outcome(queries=1, error="flux RSS invalide : canal absent")
            outcome.items = self._rss_items(ctx, channel, url)
            # Pagination documentée du flux (offset/per_page) : pages suivantes tant que la page
            # est pleine, bornée à MAX_OFFERS ; une page en échec arrête la lecture, sans perte.
            offset, seen = 0, {i.findtext("link") for i in channel.findall("item")}
            while len(channel.findall("item")) == PER_PAGE and offset + PER_PAGE < MAX_OFFERS:
                offset += PER_PAGE
                ctx.check()
                next_url = f"{found['feed_url']}?per_page={PER_PAGE}&offset={offset}"
                try:
                    channel = ET.fromstring(ctx.web.fetch(next_url).body).find("channel")
                except (FetchError, ET.ParseError) as exc:
                    outcome.error = f"page {offset // PER_PAGE + 1} du flux non lue : {exc}"[:160]
                    break
                outcome.queries += 1
                links = (
                    {i.findtext("link") for i in channel.findall("item")}
                    if channel is not None
                    else set()
                )
                if channel is None or not links - seen:
                    break  # page vide ou répétée (paramètre ignoré) : rien de plus à lire
                seen |= links
                outcome.items += self._rss_items(ctx, channel, next_url)
        else:
            outcome.items = self._page_items(ctx, page, found)
        outcome.items.insert(
            0,
            Item(
                "portal",
                found["public_url"],
                found["public_url"],
                scope="rh",
                confidence="probable",
            ),
        )
        return outcome


class TeamtailorConnector(_CareersConnector):
    name = "teamtailor"

    def _rss_items(self, ctx, channel, source_url):
        profile = getattr(ctx, "profile", None) or {}
        names = [n for n in (*profile.get("names", []), profile.get("sigle", "")) if n]
        employer = (channel.findtext("title") or "").strip()
        # Un titre de flux peut simplement annoncer les offres, sans nommer l'employeur.
        employer = re.sub(r"\s*[-|:]\s*(?:jobs|careers|offres.*)$", "", employer, flags=re.I)
        different = bool(
            employer
            and normalized(employer) not in {"jobs", "careers", "offres", "offres d emploi"}
            and not same_employer(employer, ctx.company, names)
        )
        items, seen = [], set()
        for job in channel.findall("item"):
            url = (job.findtext("link") or "").strip()
            if not web_url(url) or url in seen:
                continue
            seen.add(url)
            extra = {"provider": self.name}
            title = (job.findtext("title") or "").strip()
            description = job.find("description")
            text = "" if description is None else " ".join(description.itertext())
            text = BeautifulSoup(unescape(text), "html.parser").get_text(" ")
            if is_alternance(title, text):
                extra["alternance"] = True
            location = next(
                (
                    " ".join(tag.itertext()).strip()
                    for tag in job.iter()
                    if tag.tag.rsplit("}", 1)[-1] == "location"
                ),
                "",
            )
            if location:
                extra["location"] = location
            if different:
                extra["employer"] = employer
            items.append(
                Item(
                    "offer",
                    url,
                    source_url,
                    label=(job.findtext("title") or "").strip(),
                    scope="rh",
                    confidence="incertain" if different else "probable",
                    extra=extra,
                )
            )
            items.extend(_emails(text, source_url, ctx))
        return items


class FlatchrConnector(_CareersConnector):
    name = "flatchr"

    def _page_items(self, ctx, page, found):
        items, seen = [], set()
        root = found["public_url"]
        for anchor in visible_soup(page).select("a[href]"):
            try:
                url = urljoin(page.base_url, anchor["href"].strip()).split("#")[0]
            except ValueError:
                continue  # lien malformé publié : les autres offres restent exploitables
            detected = detect_careers(url)
            if (
                not detected
                or detected["public_url"] != root
                or not urlsplit(url).path.startswith(urlsplit(root).path + "/")
                or url in seen
            ):
                continue
            seen.add(url)
            items.append(
                Item(
                    "offer",
                    url,
                    page.url,
                    label=anchor.get_text(" ", strip=True),
                    scope="rh",
                    confidence="probable",
                    extra={"provider": self.name},
                )
            )
            if len(items) >= 20:
                break
        items.extend(_emails(visible_soup(page).get_text(" "), page.url, ctx))
        return items


CAREERS_CONNECTORS = [TeamtailorConnector, FlatchrConnector]
