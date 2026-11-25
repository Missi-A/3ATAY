"""Collecteur des annonces des portails Carrières de l'annuaire (lecture publique seule).

Repris des scripts de collecte T6/T7 (`documentation/v5/stages_alternances/scripts`). Adaptateurs : Teamtailor
(sitemap), Recruitee (API publique), SmartRecruiters (API publique), Workday (recherche publique) et sites
d'entreprise (liens d'annonces, pagination). Le réseau passe par `get`, remplaçable dans les tests.
"""
# ruff: noqa: E701, E702, E741
# Code de collecte dense repris des scripts T6 : les séparateurs « ; » sont conservés.
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import faulthandler
import hashlib
import html as H
import json
import queue
import re
import threading
import time
from contextvars import ContextVar, copy_context
from urllib.parse import parse_qsl, urljoin, urlsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ..contact_finder_engine.engine.cascade.connectors.careers import PER_PAGE, detect_careers
from ..contact_finder_engine.engine.cascade.connectors.common import portal_root
from ..contact_finder_engine.engine.models import web_url
from ..contact_finder_engine.engine.network import PublicAdapter
from ..contact_finder_engine.engine.pages import public_url
from ..database.sqlite_connection import utc_now
from .designated_links import FOLLOWED_PER_PAGE, designated_links
from .elsatis_portal import is_elsatis, offer_url
from .elsatis_portal import read_list as read_elsatis_list
from .inline_offers import (
    CAREER_SEGMENT,
    CAREER_TEXT,
    DEFERRED_HOST,
    FILLED,
    JOB_MARKERS,
    NO_AD_SIGN,
    OFFER_LIST_TEXT,
    SPONTANEOUS,
    ad_signs,
    base_url,
    career_links,
    flatchr_scope,
    host_of,
    is_ats,
    javascript_shell,
    link_problem,
    merge_shared_pdf,
    norm,
    page_entries,
    pdf_facts,
    place_alerts,
    platform_name,
    recruitment_problem,
    same_site,
    site_name,
    wttj_scope,
)
from .listing_expander import (
    BrowserUnavailable,
    ChromiumPage,
    announced_total,
    browser_page,
    expand,
    expand_in_browser,
    link_key,
    needs_expansion,
)
from .offer_domains import domain_of
from .offer_merging import flatchr_republication_key
from .offer_pdf import MAX_BYTES as MAX_PDF_BYTES
from .offer_pdf import PdfText, read_pdf
from .offer_text import extract_details, html_to_text, main_content
from .spontaneous_applications import (
    DIRECT_ONLY,
    EMPTY_PORTAL,
    empty_listing_state,
    extract_spontaneous_application,
)


def today() -> dt.date:
    """Jour LOCAL : dates de publication et de fin de validité (les annonces datent du jour du lieu). La date de contrôle
    (`date_controle`) est en UTC, comme le contrôle des retraits (`utc_now`)."""
    return dt.date.today()


TODAY = today()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9"}
POOL = 6
MAX_REQUESTS = 5000  # plafond d'une tentative de lecture d'un portail, détails et PDF compris
# Agences d'emploi et d'intérim : des centaines d'annonces pour des tiers, très longues à lire ; jamais relevées.
STAFFING_AGENCIES = re.compile(r"michaelpage|randstad|manpower|joblink|synergie|ras-interim|adecco|actualgroup|"
                               r"acass|crit-job|proman|gigroup|hays\.|robertwalters|expectra|supplay|daoust|partnaire", re.I)
PORTAL_WORKERS = 3  # portails lus en même temps (jusqu'à PORTAL_WORKERS × POOL requêtes) ; 6 figeait l'interface (verrou de Python)
PORTALS_PER_SITE = 2  # portails d'un même site lus en même temps : un site n'est jamais surchargé
PORTAL_SECONDS = 600  # échéance d'un portail : au-delà, ses requêtes sont refusées et la lecture reste partielle
PORTAL_GRACE_SECONDS = 120  # au-delà de PORTAL_SECONDS + ce délai, une lecture encore bloquée est abandonnée (son fil reste en arrière-plan)
POLL_SECONDS = 5  # fréquence du contrôle des lectures bloquées
STALL_DUMP_DIR = None  # dossier où écrire la pile des fils quand une lecture est abandonnée (fixé au démarrage de l'application)
CHECKPOINT_SECONDS = 30  # le point de reprise contient tous les résultats (des dizaines de Mo) : jamais écrit à chaque portail
MAX_PDF_PER_PORTAL = 25
DISCOVERY_SOURCE = "Découverte sur le site officiel"  # source du canal « portal » enregistré pour une page découverte
MANUAL_PORTAL_SOURCE = "Lien des offres renseigné"
WRONG_LINK = "lien erroné"  # issue d'un lien qui ne mène pas à une page de recrutement (ou à une adresse invalide)


def _filtered(url) -> bool:
    """La liste est filtrée (recherche, catégorie, lieu) : une liste vide ne prouve rien. Le numéro de page, la langue
    et les marqueurs de campagne ne filtrent rien."""
    keys = {k.lower() for k, _ in parse_qsl(urlsplit(url or "").query, keep_blank_values=True)}
    return bool(keys - {"page", "p", "lang", "hl", "locale"} - {k for k in keys if k.startswith("utm_")})


def _gap(info) -> bool:
    """Lecture liée qui laisse un manque : ni invitation à candidater, ni liste vide, ni page du même site sans annonce
    (une page « Étudiants » liée depuis la page Carrières n'est pas une liste manquée ; un portail désigné, si)."""
    return info["statut_parcours"] not in (DIRECT_ONLY, EMPTY_PORTAL) and not info.get("sans_lacune")
DESIGNATED_SOURCE = "Désigné par le site officiel"  # portail d'un autre domaine vers lequel renvoie la page officielle


REDIRECTS = (301, 302, 303, 307, 308)
_local = threading.local()


def session():
    """Session par fil d'exécution ; `PublicAdapter` connecte à l'IP validée (pas de seconde résolution DNS)."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.trust_env = False  # un proxy hérité de l'environnement contournerait PublicHTTPSConnection
        for scheme in ("http://", "https://"):
            _local.session.mount(scheme, PublicAdapter())
    return _local.session


def _charset(content_type):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    return m.group(1) if m else "utf-8"


_requests = [0]
_unscoped_requests = [0]
_requests_lock = threading.Lock()
_portal_requests: ContextVar[list[int] | None] = ContextVar("portal_requests", default=None)
_portal_deadline: ContextVar[float | None] = ContextVar("portal_deadline", default=None)


def site_key(url: str) -> str:
    """Site d'un portail pour la limite de lectures simultanées : les deux derniers libellés du nom d'hôte.
    shortcut: « .co.uk » et domaines assimilés se regroupent par « co.uk » ; plus strict que nécessaire, jamais plus lâche."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return ".".join(host.split(".")[-2:])


def _host_label(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


class SurveyActivity:
    """Avancement en direct d'un relevé (en mémoire, aucune écriture en base) : lu par l'écran chaque seconde."""

    WINDOW = 300  # secondes de la fenêtre qui donne le rythme

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.search = None  # (sites examinés, sites à examiner) pendant la recherche des pages Carrières
            self.total, self.known, self.finished, self.running = 0, set(), {}, {}
            self.durations, self.completions, self.active = {}, [], False

    def start(self, total: int, known=()):
        """Début de la lecture de `total` portails, dont `known` (urls) déjà lus lors d'une tentative précédente.
        La recherche des pages Carrières qui précède reste affichée."""
        search = self.search
        self.reset()
        with self._lock:
            self.search, self.total, self.known, self.active = search, total, set(known), True

    def searching(self, done: int, total: int):
        with self._lock:
            self.search, self.active = (done, total), True

    def begin(self, url: str):
        with self._lock:
            self.running[url] = time.monotonic()

    def end(self, url: str):
        with self._lock:
            began = self.running.pop(url, None)
            now = time.monotonic()
            self.finished[url] = now
            if began is not None:
                self.durations[url] = now - began
            self.completions.append(now)

    def stop(self):
        with self._lock:
            self.active, self.running = False, {}

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            done = len(self.known | set(self.finished))
            recent = [t for t in self.completions if now - t <= self.WINDOW]
            elapsed = min(self.WINDOW, now - min(self.completions)) if self.completions else 0
            per_minute = len(recent) * 60 / max(elapsed, 1) if recent else 0.0
            remaining = max(0, self.total - done)
            slow = sorted(self.durations.items(), key=lambda item: -item[1])[:3]
            return dict(
                active=self.active, search=self.search, done=done, total=self.total,
                running=[dict(host=_host_label(u), seconds=round(now - t))
                         for u, t in sorted(self.running.items(), key=lambda item: item[1])],
                per_minute=per_minute, eta_seconds=round(remaining * 60 / per_minute) if per_minute else None,
                slowest=[dict(host=_host_label(u), seconds=round(d)) for u, d in slow])


survey_activity = SurveyActivity()


def begin_survey():
    """Début d'un relevé : remet à zéro le décompte des requêtes et la mémoire des PDF déjà lus."""
    survey_activity.reset()
    with _requests_lock:
        _requests[0] = 0
        _unscoped_requests[0] = 0
    _pdfs.clear(); _pdf_locks.clear()


def requests_made() -> int:
    return _requests[0]


def _with_request_budget(action, *args):
    token = _portal_requests.set([0])
    deadline = _portal_deadline.set(time.monotonic() + PORTAL_SECONDS)
    try:
        return action(*args)
    finally:
        _portal_deadline.reset(deadline)
        _portal_requests.reset(token)


def _fetch(url, data=None, headers=None, timeout=25, retries=1, limit=4_000_000, strict=False, allow=None):
    """-> (statut HTTP, URL finale, octets, Content-Type, erreur). Chaque saut (redirections comprises) est contrôlé :
    URL publique HTTP(S), sans identifiants ni port exotique, connexion à l'adresse validée. `strict` : une réponse
    plus longue que `limit` est refusée (sinon elle est tronquée). `allow(url)` : périmètre de la ressource ; l'adresse
    demandée comme chaque redirection qui en sort sont refusées AVANT toute requête (statut 0)."""
    h = dict(UA); h.update(headers or {})
    if allow and not allow(url):  # dès la première requête : une adresse hors périmètre n'est jamais demandée
        return 0, url, b"", "", f"adresse hors du périmètre ({host_of(url)})"
    for attempt in range(retries + 1):
        current, body, method = url, data, "POST" if data is not None else "GET"
        try:
            for _ in range(6):
                public_url(current)
                if not take_request():
                    return 0, current, b"", "", budget_exhausted()
                with session().request(method, current, data=body, headers=h, timeout=timeout, stream=True,
                                       allow_redirects=False) as r:
                    status = r.status_code
                    if status in REDIRECTS:
                        target = r.headers.get("Location")
                        if not target:
                            return status, current, b"", "", "redirection sans destination"
                        current = urljoin(current, target)
                        if allow and not allow(current):
                            return 0, current, b"", "", f"redirection hors du périmètre ({host_of(current)})"
                        if status in (301, 302, 303) and method == "POST":
                            method, body = "GET", None
                        continue
                    if status >= 400:
                        break
                    ctype = r.headers.get("Content-Type", "")
                    raw = r.raw.read(limit + 1, decode_content=True)
                    if len(raw) > limit and strict:
                        return status, current, b"", ctype, f"réponse de plus de {limit // 1_000_000} Mo refusée"
                    return status, current, raw[:limit], ctype, ""
            else:
                return 0, url, b"", "", "trop de redirections"
        except ValueError as e:  # URL non publique : refusée sans nouvel essai
            return 0, current, b"", "", f"URL refusée : {str(e)[:80]}"
        except Exception as e:  # réseau, TLS, délai, connexion refusée par PublicAdapter
            if attempt < retries:
                time.sleep(1); continue
            return 0, current, b"", "", f"{type(e).__name__}: {str(e)[:80]}"
        if status in (429, 503) and attempt < retries:
            time.sleep(2); continue
        return status, current, b"", "", f"HTTP {status}"


def get(url, data=None, headers=None, timeout=25, retries=1, allow=None):
    """-> (statut HTTP, URL finale, texte, erreur)."""
    status, final, raw, ctype, error = _fetch(url, data, headers, timeout, retries, allow=allow)
    return status, final, raw.decode(_charset(ctype), "replace") if raw else "", error


_pdfs: dict[str, tuple[int, PdfText]] = {}
_pdf_locks: dict[str, threading.Lock] = {}
_pdfs_lock = threading.Lock()


def read_document(url, allow=None):
    """-> (statut HTTP, PdfText). Un PDF n'est téléchargé qu'une fois par relevé (même s'il est cité par plusieurs
    entrées ou plusieurs portails) ; 404, réponse hors PDF ou trop volumineuse : `PdfText.error` renseigné."""
    with _pdfs_lock:
        lock = _pdf_locks.setdefault(url, threading.Lock())
    with lock:  # deux portails qui citent le même PDF en même temps ne le téléchargent qu'une fois
        if url in _pdfs:
            return _pdfs[url]
        status, _, raw, ctype, error = _fetch(url, headers={"Accept": "application/pdf,*/*"}, retries=0,
                                              limit=MAX_PDF_BYTES, strict=True, allow=allow)
        if status != 200:
            doc = PdfText(error=f"document introuvable (HTTP {status})" if status else f"document non lu ({error})")
        elif "html" in ctype.lower():
            doc = PdfText(error="document illisible (la réponse est une page web)")
        else:
            doc = read_pdf(raw)
        if status == 200 or status in (403, 404, 410):
            _pdfs[url] = (status, doc)  # un échec de réseau ne doit pas bloquer la prochaine tentative
        return status, doc


def pmap(fn, items):
    with cf.ThreadPoolExecutor(POOL) as ex:
        futures = [ex.submit(copy_context().run, fn, item) for item in items]
        return [f.result() for f in futures]


# ---------------------------------------------------------------- lecture d'une annonce
def txt(h):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1\s*>", " ", h or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def jobposting(body):
    for m in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', body, re.S):
        try:
            d = json.loads(m)
        except Exception:
            continue
        for x in d if isinstance(d, list) else [d]:
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return None


CONTRAT_NEG = r"(?:stages?|alternance|apprentissage)\s*(?:ou|et|/)\s*(?:stages?|alternance|apprentissage)?\s*(?:accept|compris|apprécié)|expérience\s*\(?\s*(?:stages?|alternance)"
KW_TITLE_ALT = re.compile(r"alternan|apprenti|contrat[- ]pro|professionnalisation", re.I)
KW_TITLE_STAGE = re.compile(r"\bstages?\b|stagiaire|\binterns?\b|internship|\bPFE\b|\bTFE\b|early career", re.I)
KW_TXT_ALT = re.compile(r"(recherche d.une? |poste en |contrat en |contrat d.|en |de la? )(alternance|apprentissage)|contrat de professionnalisation|alternant\(?e?\)?", re.I)
KW_TXT_STAGE = re.compile(r"ce stage|stage de (?:fin d|\d)|durée du stage|convention de stage|stagiaire (?:ingénieur|assistant)|nous recrutons un\(?e?\)? stagiaire|stage (?:d.|de )\w+", re.I)


def classify(title, etype, text, url=""):
    """-> (contrat, preuve). contrat : stage | alternance | autre | indice stage | indice alternance ;
    preuve : titre | type | texte. Un indice tiré du seul texte n'est jamais compté comme confirmé."""
    c, p = _classify(title, etype, text, url)
    return (f"indice {c}", p) if p == "texte" else (c, p)


def _classify(title, etype, text, url=""):
    head = f"{title} {url}"
    et = (etype or "").upper()
    if KW_TITLE_ALT.search(title) or (KW_TITLE_ALT.search(url) and not KW_TITLE_STAGE.search(title)):
        return "alternance", "titre"
    if KW_TITLE_STAGE.search(head) or "INTERN" in et:
        return "stage", "titre" if KW_TITLE_STAGE.search(head) else "type"
    body = re.sub(CONTRAT_NEG, " ", text or "", flags=re.I)
    if KW_TXT_ALT.search(body[:6000]):
        return "alternance", "texte"
    if KW_TXT_STAGE.search(body[:6000]):
        return "stage", "texte"
    return "autre", ""


def date_flags(posted, valid, text, contract):
    flags = []
    p = (posted or "")[:10]
    try:
        pd = dt.date.fromisoformat(p) if p else None
    except ValueError:
        pd = None; flags.append("date de publication illisible")
    if pd and pd > TODAY:
        flags.append("date de publication dans le futur")
    if pd and (TODAY - pd).days > 365:
        flags.append("publiée il y a plus d'un an")
    if valid and pd and valid[:10] < p:
        flags.append("fin de validité antérieure à la publication")
    if valid and valid[:10] < TODAY.isoformat():
        flags.append("fin de validité dépassée")
    if contract in ("stage", "alternance") and re.search(
            r"(?:date limite|avant le|jusqu.au|clôtur\w*|démarrage|début|débuter|à partir d\w+|dès|poste à pourvoir)[^.]{0,40}"
            r"(20(?:1\d|2[0-5]))", text or "", re.I):
        flags.append("le texte cite une échéance passée")
    return flags


def apply_link(body, page_url):
    """Lien de candidature relevé dans la page (jamais construit)."""
    base = page_url.split("?")[0].rstrip("/")
    cand = re.findall(r'href="([^"]*(?:/applications/new|/c/new|/apply)[^"]*)"', body)
    own = [urljoin(page_url, c) for c in cand if urljoin(page_url, c).startswith(base)]
    return H.unescape(own[0]) if own else ""


SHARED_HOSTS = ("smartrecruiters.com", "lever.co", "ashbyhq.com", "greenhouse.io", "flatchr.io", "workable.com")


def portal_scope(url):
    """Identité du portail : hôte sans « www. », plus l'organisation (1er segment) si l'hôte est partagé."""
    if scope := wttj_scope(url or "") or flatchr_scope(url or ""):
        return scope
    o = urlsplit(url or ""); host = (o.hostname or "").lower().removeprefix("www.")
    first = next((s for s in o.path.split("/") if s), "")
    return f"{host}/{first.lower()}" if first and any(host == d or host.endswith("." + d) for d in SHARED_HOSTS) else host


def in_portal(url, portal_url):
    """Même hôte que le portail et chemin sous le sien (pagination et redirections restent dans son périmètre)."""
    if scope := wttj_scope(portal_url):
        return wttj_scope(url) == scope
    if scope := flatchr_scope(portal_url):
        return flatchr_scope(url) == scope
    a, b = urlsplit(url), urlsplit(portal_url)
    def host(o):
        return (o.hostname or "").lower().removeprefix("www.")
    base = b.path.rstrip("/")
    return host(a) == host(b) and (not base or a.path.rstrip("/") == base or a.path.startswith(base + "/"))


def in_perimeter(portal, final):
    """Un portail découvert sur un site officiel reste, après redirection, sur ce site (sous-domaines compris) ou sur
    une plateforme de recrutement ; un portail déclaré n'a pas de site de référence."""
    if platform_name(host_of(portal["url"])) == "wttj":
        return designated_wttj(portal) and in_portal(final, portal["url"])
    if host_of(final) in portal.get("designated_hosts", ()):  # portail que le site officiel désigne : son domaine fait foi
        return True
    home = host_of((portal.get("preuve") or {}).get("site_officiel", ""))
    return not home or same_site(host_of(final), home) or is_ats(host_of(final))


def apply_end_date(r):
    """Date de fin officielle (`validThrough`) dépassée : l'annonce n'est plus ouverte. Les dates seulement citées
    dans le texte restent des indices (alertes) sans effet sur le statut."""
    try:
        end = dt.date.fromisoformat((r.get("valable_jusqua") or "")[:10])
    except ValueError:
        return r
    if end < today() and r.get("statut") == "ouverte":
        r["statut"] = "fermée"
        r["note"] = (r.get("note", "") + " ; " if r.get("note") else "") + "date de fin officielle dépassée"
    return r


def record(portal, **kw):
    r = dict(portail=portal["url"], plateforme=portal["plateforme"], entreprise_id=None, entreprise=portal["entreprise"],
             titre="", employeur_affiche="", lieu="", type_contrat_declare="", contrat_confirme="", preuve_contrat="",
             reference="", url_officielle="", lien_candidature="", date_publication="", valable_jusqua="",
             statut="à recontrôler", http=0, erreur="", date_controle=utc_now()[:10], alertes=[], doublon_de="",
             rattachement="", note="", description="", details={}, domaine="", preuve=portal.get("preuve") or {})
    r.update(kw)
    return r


def text_fields(text, **explicit):
    """Texte complet de l'annonce (paragraphes et rubriques conservés) et détails publiés ; `explicit` : valeurs
    structurées de la source (télétravail, missions, profil…), prioritaires sur celles lues dans le texte."""
    return dict(description=text, details=extract_details(text, explicit))


def jobposting_text(jp):
    """Description d'un bloc JobPosting, complétée par ses champs structurés (missions, profil, compétences)."""
    parts = [html_to_text(jp.get("description", ""))]
    for title, key in (("Missions", "responsibilities"), ("Profil", "qualifications"), ("Profil", "educationRequirements"),
                       ("Profil", "experienceRequirements"), ("Compétences", "skills")):
        v = jp.get(key)
        v = " ; ".join(map(str, v)) if isinstance(v, list) else (v.get("description", "") if isinstance(v, dict) else v)
        if v and html_to_text(str(v)) not in parts[0]:
            parts.append(f"{title}\n{html_to_text(str(v))}")
    return "\n\n".join(p for p in parts if p)


def jobposting_remote(jp):
    return "oui" if str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE" else ""


# ---------------------------------------------------------------- adaptateurs
def teamtailor_feed(origin, own):
    """Pagination offset/per_page du connecteur Contact Finder, avec preuve de fin explicite."""
    urls, seen, offset = [], set(), 0
    while True:
        url = f"{origin}/jobs.rss?per_page={PER_PAGE}"
        if offset:
            url += f"&offset={offset}"
        status, _, body, error = get(url, allow=own)
        if status != 200:
            return urls, False, f"flux RSS HTTP {status} {error}"
        try:
            channel = ET.fromstring(body).find("channel")
        except ET.ParseError:
            channel = None
        if channel is None:
            return urls, False, "flux RSS illisible"
        items = channel.findall("item")
        links = [H.unescape(i.findtext("link") or "").strip() for i in items]
        valid = [u for u in links if own(u) and re.search(r"/jobs/\d+", u)]
        if len(valid) != len(links):
            return urls, False, "flux RSS : adresse hors du portail ou annonce illisible"
        fresh = [u for u in dict.fromkeys(valid) if u not in seen]
        if items and not fresh:
            return urls, False, "flux RSS répété : fin non prouvée"
        seen.update(fresh)
        urls += fresh
        if len(items) < PER_PAGE:
            return urls, True, f"flux RSS : {len(urls)} annonces"
        offset += PER_PAGE


def a_teamtailor(portal):
    o = urlsplit(portal["url"]); origin = f"{o.scheme}://{o.netloc}"
    own = lambda target: host_of(target) == host_of(origin)  # noqa: E731 - sitemap, flux et annonces : le portail seul
    s, _, sm, err = get(origin + "/sitemap.xml", allow=own)
    urls = sorted(set(re.findall(r"<loc>(https?://[^<]*/jobs/\d+[^<]*)</loc>", sm)))
    complete, note = s == 200, "sitemap"
    if not urls:
        urls, complete, note = teamtailor_feed(origin, own)
    foreign = [u for u in urls if not own(u)]  # adresse d'un flux hors du portail : refusée avant toute requête
    urls = [u for u in urls if own(u)]
    if not urls:
        return [], dict(statut_parcours="inaccessible" if s != 200 else "sans liste d'annonces",
                        remarque=f"sitemap HTTP {s} ; {note} : aucune annonce Teamtailor")
    def one(u):
        st, fin, b, e = get(u, allow=own); jp = jobposting(b) if st == 200 else None
        jid = (re.search(r"/jobs/(\d+)", u) or [0, ""])[1]
        if not jp:
            return record(portal, reference=jid, url_officielle=fin, http=st, erreur=e or "page sans bloc JobPosting",
                          statut="à recontrôler", alertes=["détail illisible"])
        loc = jp.get("jobLocation"); loc = loc[0] if isinstance(loc, list) and loc else loc
        a = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
        et = jp.get("employmentType"); et = " ".join(et) if isinstance(et, list) else (et or "")
        full = jobposting_text(jp); text = txt(jp.get("description", "")); title = H.unescape(jp.get("title", ""))
        c, p = classify(title, et, text, fin)
        ident = jp.get("identifier"); ident = ident.get("value") if isinstance(ident, dict) else ident
        return record(portal, titre=title, employeur_affiche=(jp.get("hiringOrganization") or {}).get("name", ""),
                      lieu=", ".join(filter(None, [a.get("addressLocality"), a.get("addressRegion")])),
                      type_contrat_declare=et, contrat_confirme=c, preuve_contrat=p, reference=str(ident or jid),
                      url_officielle=fin, lien_candidature=apply_link(b, fin) or fin, date_publication=jp.get("datePosted", ""),
                      valable_jusqua=jp.get("validThrough", "") or "", statut="ouverte", http=st,
                      alertes=date_flags(jp.get("datePosted", ""), jp.get("validThrough", ""), text, c), _text=text[:1500],
                      **text_fields(full, teletravail=jobposting_remote(jp)))
    recs = pmap(one, urls)
    failed = sum("détail illisible" in r["alertes"] for r in recs)
    complete = complete and not foreign and not failed
    note += (f" ; {len(foreign)} adresse(s) hors du portail ignorée(s)" if foreign else "")
    note += f" ; {failed} détail(s) illisible(s)" if failed else ""
    return recs, dict(statut_parcours="complet" if complete else "partiel", remarque=note)


def a_recruitee(portal):
    o = urlsplit(portal["url"]); api = f"{o.scheme}://{o.netloc}/api/offers/"
    s, _, b, e = get(api, allow=lambda target: host_of(target) == host_of(api))
    if s != 200:
        return [], dict(statut_parcours="inaccessible", remarque=f"API publique HTTP {s}")
    listing = json.loads(b)
    offers = listing.get("offers") if isinstance(listing, dict) else None
    if not isinstance(offers, list):
        return [], dict(statut_parcours="partiel", remarque="API Recruitee : liste publique non reconnue")
    recs = []
    for x in offers:
        text = txt(x.get("description", "") + " " + x.get("requirements", ""))
        missions, profil = html_to_text(x.get("description", "")), html_to_text(x.get("requirements", ""))
        full = "\n\n".join(p for p in (f"Missions\n{missions}" if missions else "", f"Profil\n{profil}" if profil else "") if p)
        remote = "oui" if x.get("remote") else "partiel" if x.get("hybrid") else ""
        tags = " ".join(x.get("tags") or [])
        c, p = classify(x.get("title", ""), x.get("employment_type_code", "") + " " + tags, text, x.get("careers_url", ""))
        recs.append(record(portal, titre=x.get("title", ""), employeur_affiche=x.get("company_name", ""),
                           lieu=", ".join(filter(None, [x.get("city"), x.get("country_code")])),
                           type_contrat_declare=x.get("employment_type_code", ""), contrat_confirme=c, preuve_contrat=p,
                           reference=str(x.get("id")), url_officielle=x.get("careers_url", ""),
                           lien_candidature=x.get("careers_apply_url", "") or x.get("careers_url", ""),
                           date_publication=(x.get("published_at") or "")[:10], valable_jusqua=(x.get("close_at") or "")[:10],
                           statut="ouverte" if x.get("status") == "published" else "à recontrôler", http=200,
                           alertes=date_flags(x.get("published_at"), x.get("close_at"), text, c), _text=text[:1500],
                           **text_fields(full, teletravail=remote, missions=missions, profil=profil)))
    return recs, dict(statut_parcours="complet", remarque=f"API /api/offers : {len(offers)} annonces")


def _smartrecruiters_unreadable(portal, cid, p, name, status, error):
    """Annonce dont le détail n'a pas pu être lu : titre, contrat et référence viennent de la liste (la référence est
    celle d'une lecture réussie), l'état reste « à recontrôler » et l'erreur est portée par l'annonce."""
    loc = p.get("location") or {}
    et = (p.get("typeOfEmployment") or {}).get("label", "") + " " + (p.get("experienceLevel") or {}).get("label", "")
    c, pr = classify(p.get("name", ""), et, "", "")
    why = error or "réponse illisible"
    return record(portal, titre=p.get("name", ""), employeur_affiche=(p.get("company") or {}).get("name", name),
                  lieu=", ".join(filter(None, [loc.get("city"), (loc.get("country") or "").upper()])),
                  type_contrat_declare=et.strip(), contrat_confirme=c, preuve_contrat=pr,
                  reference=p.get("refNumber") or p["id"],
                  url_officielle=f"https://jobs.smartrecruiters.com/{cid}/{p['id']}",  # adresse publique d'une annonce : clé de rapprochement seule
                  date_publication=(p.get("releasedDate") or "")[:10], statut="à recontrôler", http=status,
                  erreur=why, alertes=["détail illisible"], note=f"détail non récupéré ({why})")


def _sr_api(target):
    return host_of(target) == "api.smartrecruiters.com"


def a_smartrecruiters(portal):
    m = re.search(r"smartrecruiters\.com/([A-Za-z0-9_-]+)", portal["url"]); cid = m.group(1) if m else ""
    if cid.lower() in ("", "oneclick-ui"):
        return [], dict(statut_parcours="lien erroné", remarque="identifiant d'entreprise absent du lien (404)")
    resume = portal.get("resume_pages") or {}
    posts = dict(resume.get("posts", {}))
    off, expected = resume.get("offset", 0), resume.get("total")
    problem, cursor = "", None
    while expected is None or off < expected:
        s, _, b, e = get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100&offset={off}", allow=_sr_api)
        if s != 200:
            problem = f"API HTTP {s} · page au décalage {off} {e}".strip()
            if posts:
                cursor = dict(posts=posts, offset=off, total=expected)
            break
        try:
            page = json.loads(b)
            total = page["totalFound"]
            content = {str(post["id"]): post for post in page["content"]}
        except (KeyError, ValueError, TypeError):
            problem = f"réponse SmartRecruiters illisible au décalage {off}"
            break
        if resume and total != expected:
            return a_smartrecruiters(dict(portal, resume_pages=None, previous_records=()))
        if (type(total) is not int or (expected is not None and total != expected)
                or total < off + len(content) or (not content and off < total) or posts.keys() & content.keys()):
            problem = f"pagination SmartRecruiters incohérente au décalage {off}"
            break
        posts.update(content)
        expected = total
        off += len(content)
    if not posts:
        if problem:
            return [], dict(statut_parcours="inaccessible", remarque=problem)
        return [], dict(statut_parcours="lien erroné", remarque=f"identifiant « {cid} » sans annonce ni société publiée (lien générique ou vide)")
    name = (next(iter(posts.values())).get("company") or {}).get("name", "")
    cached = {r["reference"]: r for r in portal.get("previous_records", ()) if not r["erreur"]}

    def one(p):
        if previous := cached.get(p.get("refNumber") or p["id"]):
            return previous
        s, _, b, e = get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{p['id']}", allow=_sr_api)
        try:
            d = json.loads(b) if s == 200 else None
        except ValueError:
            d = None
        if not isinstance(d, dict):  # détail en erreur : l'identité vient de la liste, jamais de l'identifiant interne
            return _smartrecruiters_unreadable(portal, cid, p, name, s, e)
        secs = (d.get("jobAd") or {}).get("sections") or {}
        text = txt(" ".join((v or {}).get("text", "") for v in secs.values()))
        own = {k: v for k, v in secs.items() if k != "companyDescription" and (v or {}).get("text")}  # présentation de l'employeur : contenu générique
        full = "\n\n".join(f"{v.get('title') or k}\n{html_to_text(v['text'])}" for k, v in own.items())
        wh = d.get("location") or {}
        et = (d.get("typeOfEmployment") or {}).get("label", "") + " " + (d.get("experienceLevel") or {}).get("label", "")
        title = d.get("name", p.get("name", ""))
        c, pr = classify(title, et, text, "")
        loc = d.get("location") or p.get("location") or {}
        return record(portal, titre=title, employeur_affiche=(d.get("company") or {}).get("name", name),
                      lieu=", ".join(filter(None, [loc.get("city"), loc.get("country", "").upper()])), type_contrat_declare=et.strip(),
                      contrat_confirme=c, preuve_contrat=pr, reference=d.get("refNumber") or p["id"],
                      url_officielle=d.get("postingUrl", ""), lien_candidature=d.get("applyUrl", "") or d.get("postingUrl", ""),
                      date_publication=(d.get("releasedDate") or "")[:10], statut="ouverte" if d.get("active", True) else "à recontrôler",
                      http=s, alertes=date_flags(d.get("releasedDate"), "", text, c), _text=text[:1500],
                      **text_fields(full, teletravail="oui" if wh.get("remote") else "partiel" if wh.get("hybrid") else "",
                                    missions=html_to_text((own.get("jobDescription") or {}).get("text", "")),
                                    profil=html_to_text((own.get("qualifications") or {}).get("text", ""))))
    recs = pmap(one, posts.values())
    failed = [r["reference"] for r in recs if r["alertes"] == ["détail illisible"]]
    if failed and not problem:
        cursor = dict(posts=posts, offset=off, total=expected)
    return recs, dict(statut_parcours="partiel" if problem or failed else "complet", societe=name,
                      liste_lue=len(posts), total_annonce=expected, resume_pages=cursor,
                      remarque=f"API publique : {len(posts)} annonces ; société « {name} »"
                               + (f" ; {problem}" if problem else "")
                               + (f" ; détail non récupéré : {', '.join(failed[:5])}" if failed else ""))


def a_workday(portal):
    m = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com", portal["url"])
    if not m:
        return [], dict(statut_parcours="inaccessible", remarque="adresse Workday illisible")
    t, w = m.groups(); host = f"https://{t}.{w}.myworkdayjobs.com"
    own = lambda target: host_of(target) == host_of(host)  # noqa: E731 - le locataire Workday du portail seul
    s, _, rb, _ = get(host + "/robots.txt", headers={"Accept": "*/*"}, allow=own)
    sites = re.findall(r"/([A-Za-z0-9_-]+)/siteMap\.xml", rb)
    if not sites and (listing := re.match(r"/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)(?:/|$)", urlsplit(portal["url"]).path)):
        sites = [listing[1]]
    if not sites:
        return [], dict(statut_parcours="inaccessible", remarque=f"robots.txt HTTP {s} : site de carrières introuvable")
    resume = portal.get("resume_pages") or {}
    if resume and list(dict.fromkeys(sites)) != resume["sites"]:
        resume = {}
    hits = {(site, path): posting for site, path, posting in resume.get("hits", ())}
    totals = dict(resume.get("totals", {}))
    failed, can_continue = [], False
    for site in dict.fromkeys(sites):
        off = sum(board == site for board, _ in hits)
        if off == totals.get(site):
            continue
        while True:
            d = json.dumps({"appliedFacets": {}, "limit": 20, "offset": off, "searchText": ""}).encode()
            st, _, b, error = get(f"{host}/wday/cxs/{t}/{site}/jobs", data=d,
                              headers={"Accept": "application/json", "Content-Type": "application/json"}, allow=own)
            if st != 200:
                failed.append(f"{site} décalage {off} : HTTP {st} {error}".strip())
                can_continue = True
                break
            try:
                page = json.loads(b)
                total = page["total"]
                posts = {p["externalPath"]: p for p in page["jobPostings"]}
            except (ValueError, KeyError, TypeError):
                failed.append(f"{site} décalage {off} : réponse illisible")
                break
            first_total = totals.get(site, total)
            if off > 0 and posts and type(total) is int and total == 0:
                # Workday peut renvoyer zéro après la première page tout en renvoyant des annonces.
                total = first_total
            if resume and total != first_total:
                return a_workday(dict(portal, resume_pages=None, previous_records=()))
            if (not isinstance(total, int) or total < off + len(posts) or total != first_total
                    or any((site, path) in hits for path in posts)
                    or len(posts) != min(20, total - off)):
                failed.append(f"{site} décalage {off} : pagination incohérente")
                break
            totals[site] = total
            hits.update({(site, path): posting for path, posting in posts.items()})
            off += len(posts)
            if off >= total:
                break
    if not hits and not totals:
        return [], dict(statut_parcours="inaccessible",
                        remarque=f"API de recherche Workday inaccessible · {failed[0]}" if failed else "API Workday inaccessible")
    cached = {r["url_officielle"]: r for r in portal.get("previous_records", ()) if not r["erreur"]}

    def one(item):
        (site, path), p = item
        if previous := cached.get(f"{host}/{site}{path}"):
            return previous
        st, _, b, e = get(f"{host}/wday/cxs/{t}/{site}{path}", headers={"Accept": "application/json"}, allow=own)
        try:
            i = json.loads(b)["jobPostingInfo"] if st == 200 else None
        except (KeyError, TypeError, ValueError):
            i = None
        if not isinstance(i, dict):
            return record(portal, titre=p.get("title", ""), lieu=p.get("locationsText", ""), reference=path.rsplit("_", 1)[-1],
                          url_officielle=f"{host}/{site}{path}", http=st, erreur=e or "réponse illisible", alertes=["détail illisible"])
        text = txt(i.get("jobDescription", ""))
        full = html_to_text(i.get("jobDescription", ""))
        c, pr = classify(i.get("title", ""), i.get("timeType", ""), text, "")
        return record(portal, titre=i.get("title", ""), lieu=i.get("location", ""), type_contrat_declare=i.get("timeType", ""),
                      contrat_confirme=c, preuve_contrat=pr, reference=i.get("jobReqId", ""),
                      url_officielle=i.get("externalUrl", f"{host}/{site}{path}"), lien_candidature=i.get("externalUrl", ""),
                      date_publication=(i.get("startDate") or "")[:10], statut="ouverte" if i.get("canApply", True) else "à recontrôler", http=st,
                      alertes=date_flags(i.get("startDate", ""), "", text, c)
                      + ([f"référence datée de {y}"] if (y := (re.search(r"JR(20\d\d)", i.get("jobReqId", "")) or [0, "9999"])[1]) < str(TODAY.year - 1) else []),
                      note="date = mise en ligne (startDate Workday) ; candidature par compte Workday", _text=text[:1500],
                      **text_fields(full, teletravail=i.get("remoteType", "") or ""))
    recs = pmap(one, sorted(hits.items()))
    detail_errors = [f"détail {r['reference']} : {r['erreur']}" for r in recs if r.get("erreur")]
    can_continue |= not failed and bool(detail_errors)
    failed += detail_errors
    expected = sum(totals.values()) if set(sites) == totals.keys() else None
    cursor = (dict(sites=list(dict.fromkeys(sites)), hits=[(*key, post) for key, post in hits.items()], totals=totals)
              if can_continue else None)
    return recs, dict(statut_parcours="partiel" if failed else "complet",
                      liste_lue=len(hits), total_annonce=expected, resume_pages=cursor,
                      remarque=f"recherche publique sur {len(totals)} site(s) ; {len(recs)} annonces"
                               + (f" ; pages en échec : {'; '.join(failed[:3])}" if failed else ""))


JOBLINK = re.compile(r"/(?:jobs?|offres?|offers?|offre-emploi|offres-emploi|emplois?|postes?|vacancies|opportunit\w*|positions?|nos-offres|recrutement|carrieres?|careers?)/[^/?#]{5,}", re.I)
NOTJOB = re.compile(r"/(?:category|categorie|tag|page|author|wp-|feed|contact|actualit|blog|mentions|politique|cgu|cookies)", re.I)
# Annonce désignée par un identifiant dans la requête de la page de liste (« ?job=8006104 »).
JOB_QUERY = re.compile(r"(?:job|jobid|job_id|offre|offer|offerid|offer_id|id_offre|annonce|vacancy|poste)=[\w-]{2,}", re.I)
# Page de détail unique dont l'annonce est désignée par la requête (« /offre-demploi-detail/?offre=2761309 »).
JOB_DETAIL_PATH = re.compile(r"offre|offer|job|emploi|poste|annonce|vacanc|detail", re.I)
# Dernier segment d'une adresse d'annonce : mention « H/F » ou identifiant numérique long.
JOB_SLUG = re.compile(r"(?:^|[-_])(?:h-?f|f-?h|m-?f|f-?m|hf|fh)(?:[-_]x)?(?:$|[-_.])|\d{5,}", re.I)


def anchors(body):
    """[(balise, href, rel, texte)] des vraies balises <a> et <link> de la page ; un « href="…" » écrit dans un script ou
    un commentaire n'en est pas un. Les liens d'annonce viennent des <a> ; la pagination aussi des <link rel="next">."""
    return [(t.name, t["href"].strip(), t.get("rel") or [], t.get_text(" ", strip=True) if t.name == "a" else "")
            for t in BeautifulSoup(body or "", "html.parser").find_all(["a", "link"], href=True)]


def job_slug_links(urls, host):
    """Séries d'annonces sans mot-clé dans le chemin (« /search/1785…-technicien-h-f ») : au moins deux adresses du même
    hôte et du même dossier dont le dernier segment porte « H/F » ou un identifiant long. Une page de métier
    (« /nos-metiers/technicien ») n'en porte pas."""
    groups: dict[str, set[str]] = {}
    for u in urls:
        o = urlsplit(u)
        parts = [p for p in o.path.split("/") if p]
        if (host_of(u) != host or len(parts) < 2 or NOTJOB.search(o.path) or SPONTANEOUS.search(o.path)
                or re.search(r"\.(?:pdf|docx?|odt|jpe?g|png|gif|webp|svg|zip|css|js)$", parts[-1], re.I)):
            continue  # un document (fiche PDF, image) n'est jamais une page d'annonce de la série
        if JOB_SLUG.search(parts[-1]):
            groups.setdefault("/".join(parts[:-1]), set()).add(u.rstrip("/"))
    return {u for group in groups.values() if len(group) >= 2 for u in group}


def budget_exhausted() -> str:
    deadline = _portal_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        return "délai du portail atteint"
    return "limite de requêtes du relevé atteinte"


def take_request() -> bool:
    """Une requête du navigateur compte dans le budget du relevé, comme celles de `_fetch`. Passée l'échéance du
    portail, elle est refusée comme à la limite de requêtes : la lecture reste partielle, jamais « complète »."""
    deadline = _portal_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        return False
    with _requests_lock:
        counter = _portal_requests.get() or _unscoped_requests
        if counter[0] >= MAX_REQUESTS:
            return False
        counter[0] += 1
        _requests[0] += 1
        return True


expand_listing = expand_in_browser  # remplaçable dans les tests (aucun navigateur)


def _generic_offer(portal, u, response, landing):
    st, fin2, bb, e2 = response
    host = urlsplit(landing).netloc.replace("www.", "")
    wttj = bool(wttj_scope(landing))
    if st != 200:
        return record(portal, reference=u, url_officielle=u, http=st, erreur=e2, alertes=["détail illisible"])
    if (urlsplit(fin2).netloc.replace("www.", "") != host
            or (wttj and not in_portal(fin2, landing))):
        return record(portal, reference=u, url_officielle=u, http=st, erreur=f"redirection hors du portail ({urlsplit(fin2).netloc})",
                      alertes=["détail illisible"])
    jp = jobposting(bb); text = txt(bb)[:6000]
    full = jobposting_text(jp) if jp and jp.get("description") else main_content(bb)  # annonce structurée, sinon contenu principal
    title = H.unescape(jp.get("title", "")) if jp else (txt((re.search(r"<h1[^>]*>(.*?)</h1>", bb, re.S) or [0, ""])[1]) or txt((re.search(r"<title[^>]*>(.*?)</title>", bb, re.S) or [0, ""])[1]))
    et = (jp or {}).get("employmentType") or ""
    et = " ".join(et) if isinstance(et, list) else et
    evidence = jp and txt(jp.get("description", "")) or text
    c, pr = classify(title, et, evidence, fin2)
    if wttj and (KW_TITLE_STAGE.search(et) or KW_TITLE_ALT.search(et)):
        c, pr, _ = entry_contract(title, et, evidence, fin2)
    loc = ""
    if jp:
        lc = jp.get("jobLocation"); lc = lc[0] if isinstance(lc, list) and lc else lc
        a = (lc or {}).get("address", {}) if isinstance(lc, dict) else {}
        loc = ", ".join(filter(None, [a.get("addressLocality"), a.get("addressRegion")]))
    listing = bool(re.match(r"\s*(nos |retrouvez |toutes |nous rejoindre|rejoignez|\(fr\) )", title, re.I))
    signs = ad_signs(full, bool(jp), apply_link(bb, fin2))  # texte visible : ni scripts ni filtres de liste
    isjob = bool(signs) and not listing
    closed = isjob and bool(FILLED.search(full or ""))  # « Ce poste est pourvu » : l'annonce reste publiée mais close
    return record(portal, titre=title[:140], employeur_affiche=(jp or {}).get("hiringOrganization", {}).get("name", "") if jp else "",
                  lieu=loc, type_contrat_declare=et, contrat_confirme=c, preuve_contrat=pr,
                  reference=(jp or {}).get("identifier", {}).get("value", "") if jp and isinstance((jp or {}).get("identifier"), dict) else "",
                  url_officielle=fin2, lien_candidature=apply_link(bb, fin2) or fin2,
                  date_publication=(jp or {}).get("datePosted", "") if jp else "", valable_jusqua=(jp or {}).get("validThrough", "") if jp else "",
                  statut="fermée" if closed else "ouverte" if isjob else "à recontrôler", http=st, alertes=date_flags((jp or {}).get("datePosted", ""), (jp or {}).get("validThrough", ""), text, c) + ([] if isjob else [NO_AD_SIGN + " (données structurées, rubriques de poste, référence ou lien de candidature)"]),
                  note="signes : " + ", ".join(signs) if signs else "", _text=text[:1500],
                  **text_fields(full, teletravail=jobposting_remote(jp or {})))


def _designate(page, links, followed, designated, designations):
    """Classe les liens d'une page officielle vers un autre domaine : portails à suivre (`followed`, avec la page qui les
    désigne), vitrine WTTJ (désignation seulement enregistrée : sa lecture reste soumise à `designated_wttj`), agrégateurs
    (signalés, jamais suivis)."""
    vitrines = {}
    for link in links:
        if link["kind"] == "wttj":
            vitrines.setdefault(wttj_scope(link["url"]), link)
        elif link["kind"] == "differe":
            wttj = "welcometothejungle" in link["plateforme"] or "welcomekit" in link["plateforme"]
            designations.append(dict(page=page, lien=link["url"], final="", texte=link["texte"], plateforme=link["plateforme"],
                                     etat=("vitrine WTTJ d'une forme que la règle de désignation ne reconnaît pas : non suivie"
                                           if wttj else "agrégateur différé : non suivi"), annonces=0))
        elif len(designated) < FOLLOWED_PER_PAGE:
            key = link["url"].rstrip("/")
            followed.setdefault(key, (link["url"], ADAPT.get(link["plateforme"])))
            designated.setdefault(key, dict(page=page, texte=link["texte"]))
    if len(vitrines) == 1:
        (link,) = vitrines.values()
        designations.append(dict(page=page, lien=link["url"], final=link["url"], texte=link["texte"], plateforme="wttj",
                                 etat="désignée", annonces=0))
    elif vitrines:
        designations.append(dict(page=page, lien="", final="", texte="", plateforme="wttj", annonces=0,
                                 etat=f"ambigu : {len(vitrines)} vitrines WTTJ désignées"))


def _employer_perimeter(recs):
    """Un portail désigné peut être partagé (site de recrutement d'un groupe) : seules restent les annonces dont l'employeur
    est la fiche ou l'une des fiches possibles (rattachement ambigu, à vérifier). Une annonce dont l'employeur n'est pas
    identifiable parmi les fiches du portail est écartée. -> (annonces gardées, nombre d'annonces écartées)."""
    kept = [r for r in recs if not r.get("designe") or not (
        "non confirmé" in r["rattachement"] or r["rattachement"].startswith("à vérifier : employeur non identifiable"))]
    return kept, len(recs) - len(kept)


def _read_designated(portal, destination, adapter, meta, designations):
    """Suit le lien publié par la page officielle (redirections comprises), puis lit le portail avec son collecteur.
    Le périmètre est le domaine désigné ; la page officielle et l'URL finale sont gardées dans `designations`.
    -> (annonces, bilan, URL finale)."""
    item = dict(page=meta["page"], lien=destination, final=destination, texte=meta["texte"], plateforme="site", etat="",
                parcours="", annonces=0)
    designations.append(item)
    status, final, _, error = get(destination)
    if status != 200:
        reason = error or f"HTTP {status}"
        item.update(etat=f"inaccessible : {reason}", parcours="inaccessible")
        return [], dict(statut_parcours="inaccessible", remarque=f"portail désigné par le site officiel inaccessible : {reason}"), destination
    item["final"], host = final, host_of(final)
    if platform_name(host) == "wttj" or DEFERRED_HOST.search(host):
        item.update(etat=f"agrégateur différé : non suivi (redirection vers {host})", plateforme=host)
        return [], dict(statut_parcours="externe (différé)", remarque=f"le lien désigné mène à un agrégateur ({host}) : non suivi"), final
    platform = platform_of(final)
    adapter = adapter or ADAPT.get(platform)
    child = dict(portal, url=final, plateforme=platform, designated_hosts=sorted({host_of(destination), host}),
                 preuve=dict(site_officiel=meta["page"], lien=destination, texte=meta["texte"], designation=True))
    offers, info = adapter(child) if adapter else a_generique(child, designate=False)
    item.update(etat="lu", plateforme=platform, parcours=info["statut_parcours"], annonces=len(offers))
    return offers, info, final


def a_generique(portal, *, follow_site_link=True, designate=True):
    s, fin, b, e = get(portal["url"], allow=lambda target: in_perimeter(portal, target))
    if s != 200:
        return [], dict(statut_parcours="inaccessible", remarque=f"HTTP {s} {e}")
    if not in_perimeter(portal, fin):
        return [], dict(statut_parcours="inaccessible", remarque=f"redirection hors du périmètre du site officiel ({host_of(fin)})")
    if is_elsatis(fin, b):  # portail Elsatis sur le domaine de l'entreprise, ou atteint par redirection
        return a_elsatis(dict(portal, url=fin if is_elsatis(fin) else portal["url"], plateforme="elsatis"))
    if flatchr_scope(fin):
        ads, info = a_flatchr(dict(portal, url=fin, plateforme="flatchr"), response=(s, fin, b, e))
        return [dict(ad, page_portail=portal["url"]) for ad in ads], dict(info, perimetres=[portal_scope(fin)])
    # Lien enregistré ou découvert (jamais un lien saisi à la main, ni une plateforme connue) : est-ce bien une page de
    # recrutement ? Un blog, une page d'admission ou un renvoi vers l'accueil laisse 3ATAY chercher ailleurs.
    if (follow_site_link and designate and not portal.get("manuels") and not is_ats(host_of(fin))
            and (problem := recruitment_problem(portal["url"], fin, b))):
        return [], dict(statut_parcours="lien erroné", remarque=f"lien invalide : {problem}")
    host = urlsplit(fin).netloc.replace("www.", "")
    wttj = bool(wttj_scope(fin))
    pages, seen_pages = [(fin, b)], {fin, portal["url"]}
    links, followed, refused = set(), {}, ""
    designations = portal["_designations"] = []  # portails désignés par cette page, lus ou non (voir `_run_portal`)
    designated: dict[str, dict] = {}
    listing_path = urlsplit(fin).path.rstrip("/")
    spontaneous_links: set[str] = set()  # entrées « Candidature spontanée » d'une liste : canal, pas annonce

    def job_link(u):
        o = urlsplit(u)
        if (o.netloc.replace("www.", "") != host or (wttj and not in_portal(u, fin)) or NOTJOB.search(u)
                or SPONTANEOUS.search(o.path) or u.rstrip("/") == fin.rstrip("/") or u.rstrip("/") in spontaneous_links):
            return False
        if o.query and JOB_QUERY.fullmatch(o.query) and o.path.rstrip("/") == listing_path:
            return True  # annonce de la même page désignée par son identifiant (« ?job=123 »)
        if o.query and JOB_QUERY.fullmatch(o.query) and JOB_DETAIL_PATH.search(o.path):
            return True  # page de détail commune à toutes les annonces, l'identifiant est dans la requête
        return bool(JOBLINK.search(o.path)) and len(o.path.strip("/").split("/")) >= 2

    def rendered_job(u):  # liste rendue dans le navigateur : mêmes règles, plus les séries « …-h-f » isolées
        parts = [p for p in urlsplit(u).path.split("/") if p]
        return job_link(u) or (host_of(u) == host_of(fin) and len(parts) >= 2 and bool(JOB_SLUG.search(parts[-1]))
                               and not NOTJOB.search(u) and not SPONTANEOUS.search(u))

    for url, body in pages:  # les pages suivantes sont ajoutées à la fin après vérification
        hrefs = anchors(body)
        base = base_url(body, url)
        spontaneous_links.update(urljoin(base, h).split("#")[0].rstrip("/") for tag, h, _, text in hrefs
                                 if tag == "a" and SPONTANEOUS.search(text))
        if not wttj:
            for link in career_links(body, url, limit=8):
                destination = link["url"]
                segment = urlsplit(destination).path.rstrip("/").rsplit("/", 1)[-1]
                if flatchr_scope(destination) and not re.search(r"/vacancy/", urlsplit(destination).path):
                    followed.setdefault(portal_scope(destination), (destination, a_flatchr))
                elif is_elsatis(destination):  # « Découvrir nos offres » vers le portail Elsatis de l'entreprise
                    followed.setdefault(portal_scope(destination), (destination, a_elsatis))
                elif (follow_site_link and same_site(host_of(destination), host_of(fin))
                      and not (urlsplit(destination).query and job_link(destination))  # « ?job=12 » : une annonce
                      and (OFFER_LIST_TEXT.match(link["texte"]) or
                           (CAREER_SEGMENT.fullmatch(segment) and re.match(r"offres?|emplois?|jobs|nos-offres", segment, re.I)))):
                    followed.setdefault(destination.rstrip("/"), (destination, None))
            if designate:  # un portail désigné est lu une fois : ses propres liens externes ne sont pas suivis
                _designate(url, designated_links(body, url, host_of(fin)), followed, designated, designations)
        page_links = [urljoin(base, l).split("#")[0] for tag, l, _, _ in hrefs if tag == "a"]
        links.update(u.rstrip("/") for u in page_links if job_link(u))
        links.update(u for u in job_slug_links(page_links, host_of(fin))
                     if u not in spontaneous_links and (not wttj or in_portal(u, fin)) and u != fin.rstrip("/"))
        for _, href, rel, _ in hrefs:
            next_page = "next" in rel
            if not (next_page or re.search(r"[?&/]page[=/]\d+", href)):
                continue
            address = urljoin(base, href).split("#")[0]
            if address in seen_pages:
                if wttj and next_page and address == url:
                    refused = "pagination WTTJ répétée : fin non prouvée"
                continue
            if not in_portal(address, fin):
                if next_page or host_of(address) == host_of(fin):  # « …/jobs?page=1 » d'un autre site : lien externe
                    refused = f"lien de pagination hors du portail ignoré ({urlsplit(address).netloc})"
                continue
            seen_pages.add(address)
            st, final, content, _ = get(address, allow=lambda target: in_portal(target, fin))
            if st != 200 or not in_portal(final, fin):
                refused = f"page de liste en échec (HTTP {st})" if st != 200 else "redirection hors du portail ignorée"
                continue
            if final != address and final in seen_pages:
                refused = "pagination répétée : fin non prouvée"
                continue
            seen_pages.add(final)
            pages.append((final, content))
    expanded = ""
    counts = {}
    if not wttj and (cause := needs_expansion(b, len(links))):
        # Liste chargée par JavaScript (« Voir plus », pages numérotées, défilement) : le HTML n'en montre que le début.
        opened = expand_listing(fin, job_link, lambda target: in_perimeter(portal, target), take_request)
        links.update(link_key(h) for h in opened.hrefs)
        total = opened.total if opened.total is not None else announced_total(b)
        if total is not None and len(links) <= total:
            counts = dict(liste_lue=len(links), total_annonce=total)
        expanded = f"liste dépliée ({cause}) : {len(opened.hrefs)} annonce(s)" + (
            f" sur {opened.total} annoncées" if opened.total is not None else "")
        if not opened.complete:
            refused = refused or f"liste incomplète : {opened.reason}"
    links.difference_update(seen_pages)
    links.difference_update(destination.rstrip("/") for destination, _ in followed.values())
    inline, einfo, seen_entries = [], [], 0  # annonces intégrées aux pages (avec ou sans fiche PDF), lues avec les liens
    # Aperçu d'une annonce d'un portail suivi (« Consulter l'offre » vers le portail du groupe) : lue sur ce portail, avec
    # son périmètre d'employeur ; jamais attribuée à la fiche depuis la page qui la présente.
    followed_hosts = {host_of(destination) for destination, _ in followed.values()} - {host_of(fin)}
    for page_url, page_body in pages:
        entries = [e for e in page_entries(page_body, page_url)
                   if not (e.href and (e.href.rstrip("/") in links or host_of(e.href) in followed_hosts))]  # lien : lu à part
        if wttj:
            entries = [e for e in entries if all(
                in_portal(u, fin) for u in (e.href, e.pdf_url) if u)]
        ads = [e for e in entries if not e.spontaneous]
        seen_entries += len(entries)
        if ads:
            found, found_info = entry_records(portal, page_url, page_body, ads, spontaneous=len(entries) - len(ads))
            inline += found; einfo.append(found_info)
    linked, link_info, linked_scopes = [], [], set()
    read_pages = {page.rstrip("/") for page in seen_pages}
    for destination, adapter in followed.values():
        if destination.rstrip("/") in read_pages:
            continue
        meta = designated.get(destination.rstrip("/"))
        if meta:  # portail désigné : redirections suivies, périmètre = domaine désigné (voir `_read_designated`)
            offers, info, destination = _read_designated(portal, destination, adapter, meta, designations)
            offers = [dict(offer, designe=True) for offer in offers]
        else:
            child = dict(portal, url=destination, plateforme=platform_of(destination))
            if adapter:
                offers, info = adapter(child)
            else:
                offers, info = a_generique(child, follow_site_link=False, designate=False)
        linked.extend(dict(offer, page_portail=portal["url"]) for offer in offers)
        if not meta and info["statut_parcours"] == "sans liste d'annonces" and same_site(host_of(destination), host_of(fin)):
            info = dict(info, sans_lacune=True)  # page du même site sans annonce : pas une liste manquée
        link_info.append(info)
        linked_scopes.update(info.get("perimetres", ()))
        if info["statut_parcours"] in ("complet", EMPTY_PORTAL):
            linked_scopes.add(portal_scope(destination))
    if not counts and not links and not inline and len(link_info) == 1 and "total_annonce" in link_info[0]:
        counts = {key: link_info[0][key] for key in ("liste_lue", "total_annonce")}
    if not wttj and not (links or inline or link_info or seen_entries) and javascript_shell(b):
        # Coquille JavaScript : la liste et les annonces ne s'affichent qu'après rendu, lu dans le navigateur.
        rendered, info = read_rendered(portal, fin, lambda target: in_perimeter(portal, target), rendered_job,
                                       "liste rendue par JavaScript")
        info.setdefault("candidature", extract_spontaneous_application(b, fin, listing=bool(rendered)))
        return rendered, info
    read, total = len(links) + len(inline), counts.get("total_annonce", announced_total(b) if links else None)
    if links and total is not None and read < total:  # le portail annonce plus d'offres que la liste lue
        counts = dict(liste_lue=read, total_annonce=total)
        refused = refused or f"liste incomplète : {read} lues sur {total} annoncées"
    has_listing = bool(links or inline or linked or any(i["statut_parcours"] == "complet" for i in link_info))
    direct = extract_spontaneous_application(b, fin, listing=has_listing)
    if not has_listing:
        if not direct:
            contacts = {channel["target"]: channel for info in link_info
                        if (channel := info.get("candidature")) and channel.get("target")}
            if len(contacts) == 1:
                direct = next(iter(contacts.values()))
        if any(_gap(i) for i in link_info):
            return [], dict(statut_parcours="partiel", remarque=" ; ".join(i["remarque"] for i in link_info),
                            candidature=direct, **counts)
        if refused:
            return [], dict(statut_parcours="partiel", remarque=refused, candidature=direct, **counts)
        unfiltered = not (_filtered(portal["url"]) or _filtered(fin))
        if direct.get("target") and unfiltered:
            return [], dict(statut_parcours=DIRECT_ONLY, remarque="La page invite à candidater directement",
                            candidature=direct)
        empty_state = empty_listing_state(b) if unfiltered else ""
        if not empty_state and unfiltered and any(i["statut_parcours"] == EMPTY_PORTAL for i in link_info):
            empty_state = EMPTY_PORTAL
        if empty_state:
            return [], dict(statut_parcours=empty_state, remarque="Le portail indique qu'il n'y a aucune offre",
                            candidature=direct, perimetres=sorted(linked_scopes))
        spont = bool(seen_entries) or bool(re.search(r"candidature spontan", b, re.I))
        return [], dict(statut_parcours="sans liste d'annonces",
                        remarque="page Carrières sans annonce individuelle détectée" + (" (candidature spontanée seule : canal de contact)" if spont else " (rendu JavaScript non exclu)"),
                        candidature=direct)
    def one(u):
        st, fin2, bb, e2 = get(u, allow=lambda target: (
            in_portal(target, fin) if wttj else host_of(target) == host_of(fin)))
        return _generic_offer(portal, u, (st, fin2, bb, e2), fin)
    recs = pmap(one, sorted(links))
    linked_titles = {norm(r["titre"]) for r in recs if r["titre"]}
    # Carte sans lien ni fiche qui reprend l'intitulé d'une annonce liée de la même liste : la même annonce, lue une fois.
    inline = [r for r in inline if not (r.pop("_sans_lien", False) and norm(r["titre"]) in linked_titles)]
    partiel = (bool(refused) or any(i["statut_parcours"] == "partiel" for i in einfo)
               or any(i["statut_parcours"] != "complet" and _gap(i) for i in link_info))
    einfo += link_info
    if wttj and any(re.search(r"(?:voir|afficher|charger|load)\s+(?:plus|more)", txt(b), re.I)
                    for _, b in pages):
        partiel = True
        refused = refused or "liste WTTJ tronquée : chargement supplémentaire non lu"
    notes = ([f"{len(recs)} lien(s) d'annonce, {len(pages)} page(s) de liste parcourue(s)"
              + (f" ; {expanded}" if expanded else "") + (f" ; {refused}" if refused else "")] if links else []) + [
        i["remarque"] for i in einfo]
    if refused and not links:
        notes.append(refused)
    return recs + inline + linked, dict(statut_parcours="partiel" if partiel else "complet", remarque=" ; ".join(notes),
                               candidature=direct, **counts,
                               perimetres=sorted(linked_scopes),
                               entrees=sum(i.get("entrees", 0) for i in einfo), spontanees=sum(i.get("spontanees", 0) for i in einfo),
                               pdf_lus=sum(i.get("pdf_lus", 0) for i in einfo), pdf_illisibles=sum(i.get("pdf_illisibles", 0) for i in einfo))


WTTJ_JOBS_PATH = re.compile(r"/(?:[a-z]{2}/)?companies/[\w-]+/jobs")
WTTJ_PROFILE_PATH = re.compile(r"/(?:[a-z]{2}/)?companies/[\w-]+")


def _read_rendered_job(portal, page, url, is_job):
    """Lit le détail dans la même session que la liste et vérifie l'URL de destination."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
        status = response.status if response else 0
        if status != 200:
            return _generic_offer(portal, url, (status, url, "", f"HTTP {status} dans le navigateur"), portal["url"])
        if is_job(page.url):
            ChromiumPage(page).settle()
            if is_job(page.url):
                ad = _generic_offer(portal, url, (status, page.url, page.content(), ""), portal["url"])
                if ad["statut"] in ("ouverte", "fermée"):
                    return ad
        error = "détail non chargé dans le navigateur ou redirection hors de l'annonce"
    except Exception as exc:  # noqa: BLE001 - les autres annonces restent lisibles
        error = f"lecture de l'annonce interrompue dans le navigateur ({type(exc).__name__})"
    return _generic_offer(portal, url, (0, url, "", error), portal["url"])


MAX_RENDERED_DETAILS = 60  # annonces ouvertes une à une dans le navigateur par portail et par relevé
# Échecs d'affilée après lesquels le navigateur ne charge plus les annonces (budget de requêtes épuisé) : on s'arrête.
MAX_DETAIL_FAILURES = 3
INTERRUPTED = "lecture de l'annonce interrompue"


def _read_count(records) -> int:
    return sum(not ad["erreur"] for ad in records)


def read_rendered(portal, url, allow, is_job, label, empty_ok=lambda page_url: True,
                  no_list="la page n'a pas chargé de liste d'annonces"):
    """Lecture publique, dans un seul navigateur, d'une liste rendue par JavaScript et de ses annonces : mécanisme de
    la vitrine WTTJ, aussi employé pour toute page Carrières vide avant rendu. Les annonces lues avant une panne sont
    gardées ; le motif, le nombre lu et le total annoncé (s'il est publié) restent dans le bilan."""
    records, counts, links = [], {}, []
    try:
        with browser_page(url, allow, take_request) as page:
            driver = ChromiumPage(page)
            driver.settle()
            listing = expand(driver, is_job)
            links = sorted({link_key(href) for href in listing.hrefs if is_job(href)})
            if listing.total is not None and len(links) <= listing.total:
                counts = dict(liste_lue=len(links), total_annonce=listing.total)
            if not links:
                if (listing.complete and not (_filtered(url) or _filtered(page.url))
                        and empty_ok(page.url) and (state := empty_listing_state(page.content()))):
                    return [], dict(statut_parcours=state, remarque=f"{label} : liste vide confirmée dans le navigateur")
                reason = listing.reason or no_list
                return [], dict(statut_parcours="partiel", remarque=f"{label} (navigateur) : {reason}", **counts)
            failures = 0
            for job in links[:MAX_RENDERED_DETAILS]:
                records.append(ad := _read_rendered_job(portal, page, job, is_job))
                failures = failures + 1 if (ad["erreur"] or "").startswith(INTERRUPTED) else 0
                if failures >= MAX_DETAIL_FAILURES:
                    break
            if records and not allow(page.url):  # la dernière annonce a mené ailleurs : retour à la liste avant fermeture
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001 - les annonces lues restent acquises
                    pass
    except BrowserUnavailable as exc:
        reason = str(exc)
        if records:  # panne après les premières annonces : elles restent acquises, le bilan dit où la lecture s'est arrêtée
            read = _read_count(records)
            reason = (f"lecture arrêtée : {read} annonces lues sur {len(links)} repérées"
                      if len(records) >= MAX_RENDERED_DETAILS < len(links) else
                      f"lecture interrompue après {read} annonce(s) lue(s) sur {len(links)} repérée(s) "
                      f"(limite de requêtes du navigateur ou annonce hors du portail) ; {reason}")
            counts = dict(liste_lue=read, total_annonce=max(len(links), counts.get("total_annonce") or 0))
        return records, dict(statut_parcours="partiel" if records else "inaccessible",
                             remarque=f"{label} (navigateur) : {reason}", **counts)

    unreadable = [ad["erreur"] for ad in records if ad["erreur"]]
    stopped = ""
    if len(links) > len(records):  # les annonces lues restent ; le bilan dit où la lecture s'est arrêtée
        stopped = f"lecture arrêtée : {_read_count(records)} annonces lues sur {len(links)} repérées" + (
            "" if len(records) >= MAX_RENDERED_DETAILS else " (le navigateur ne charge plus les annonces)")
        counts = dict(liste_lue=_read_count(records), total_annonce=max(len(links), listing.total or 0))
    reasons = " ; ".join(filter(None, (listing.reason, stopped, unreadable[0] if unreadable else "")))
    return records, dict(statut_parcours="complet" if listing.complete and not unreadable and not stopped else "partiel",
                         remarque=f"{label} (navigateur) : {len(links)} annonce(s) repérée(s)"
                                  + (f" ; {reasons}" if reasons else ""), **counts)


def _read_wttj_browser(portal):
    """Lecture publique de la liste de l'organisation et de ses annonces, dans un seul navigateur."""
    scope = wttj_scope(portal["url"])

    def allow(url):
        return bool(scope and wttj_scope(url) == scope)

    def is_job(url):
        path = urlsplit(url).path.rstrip("/")
        return allow(url) and WTTJ_JOBS_PATH.fullmatch(path.rsplit("/", 1)[0]) and not SPONTANEOUS.search(path)

    records, info = read_rendered(portal, portal["url"], allow, is_job, "Welcome to the Jungle",
                                  empty_ok=lambda page_url: bool(WTTJ_JOBS_PATH.fullmatch(urlsplit(page_url).path.rstrip("/"))),
                                  no_list="la page Jobs n'a pas chargé de liste d'annonces")
    if not records and "liste vide" in info["remarque"]:  # vitrine : liste vide confirmée, quel que soit le canal
        info["statut_parcours"] = EMPTY_PORTAL
    return records, info


def entry_contract(title, label, text, url=""):
    """-> (contrat, preuve, alerte). Le contrat annoncé par la page (« CDI », « Stage ») prime sur un simple indice du
    texte ; un intitulé « stage » avec un contrat CDI/CDD annoncé n'est pas confirmé."""
    if re.search(r"stage|stagiaire|intern", label or "", re.I):
        return "stage", "type", ""
    if KW_TITLE_ALT.search(label or ""):
        return "alternance", "type", ""
    c, p = classify(title, "", text, url)
    if label and p == "texte":
        return "autre", "", ""
    if label and c in ("stage", "alternance"):
        return f"indice {c}", "titre", f"contrat annoncé ({label}) en contradiction avec l'intitulé"
    return c, p, ""


def entry_reference(e, page_url):
    """Référence stable d'une entrée : nom du fichier PDF (+ intitulé), sinon page et intitulé. Indépendante des lieux."""
    if e.pdf_url:
        stem = urlsplit(e.pdf_url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"{stem}-{hashlib.sha1(norm(e.title).encode()).hexdigest()[:6]}"
    return "html-" + hashlib.sha1(f"{urlsplit(page_url).path.rstrip('/')}|{norm(e.title)}".encode()).hexdigest()[:10]


def entry_records(portal, page_url, body, entries, spontaneous=0):
    """Annonces intégrées à une page Carrières -> (annonces, bilan du portail). Une entrée qui cite un PDF du même site
    est complétée par le document (lu une fois) ; un PDF introuvable ou illisible laisse l'annonce « à recontrôler »
    (détail illisible) sans la retirer ; deux entrées du même PDF sont une annonce aux lieux cumulés. Une annonce qui
    n'est qu'un lien vers un PDF sans date récente reste « à recontrôler » : sa disponibilité n'est pas établie."""
    entries = merge_shared_pdf(entries)
    site = urlsplit(page_url).hostname or ""
    pdfs = sorted({e.pdf_url for e in entries if e.pdf_url and (urlsplit(e.pdf_url).hostname or "") == site})[:MAX_PDF_PER_PORTAL]
    inside = lambda target: same_site(host_of(target), site)  # noqa: E731 - une fiche redirigée hors du site n'est pas lue
    docs = dict(zip(pdfs, pmap(lambda pdf: read_document(pdf, allow=inside), pdfs), strict=True))
    entries = [e for e in entries if e.label or not (d := docs.get(e.pdf_url)) or d[1].error
               or JOB_MARKERS.search(d[1].text[:4000])]  # document lu sans signe d'annonce : plaquette, pas une annonce
    if not entries:
        return [], dict(statut_parcours="sans liste d'annonces", spontanees=spontaneous,
                        remarque="documents PDF de la page sans signe d'annonce (plaquettes) ignorés")
    employer_page = site_name(body)
    per_pdf = {u: sum(1 for e in entries if e.pdf_url == u) for u in pdfs}
    recs, unreadable = [], []
    for e in entries:
        doc = docs.get(e.pdf_url)
        readable = bool(doc) and not doc[1].error
        text = doc[1].text if readable else e.text
        facts = pdf_facts(text) if readable else {}
        label = e.label or facts.get("contrat", "")
        contract, proof, conflict = entry_contract(e.title, label, text, page_url)
        alerts = [conflict] if conflict else []
        error, date = "", e.date or facts.get("date", "")
        if readable:
            alerts += place_alerts(e.places, facts.get("lieux", []))
        elif e.pdf_url:
            error = doc[1].error if doc else "document hors du site ou au-delà de la limite de lecture : non lu"
            alerts.append("détail illisible")
            unreadable.append(e.title)
        ref = entry_reference(e, page_url)
        recent = bool(date) and (TODAY - dt.date.fromisoformat(date)).days <= 120
        if e.pdf_url and not error and not recent:
            alerts.append("disponibilité non confirmée (fiche PDF sans date de publication récente)")
        opened = not e.pdf_url or (not error and recent)
        alerts += date_flags(date, "", text, contract)
        url = f"{page_url.split('#')[0]}#{ref}"
        if e.pdf_url:
            url = e.pdf_url if per_pdf.get(e.pdf_url, 1) == 1 else f"{e.pdf_url}#{ref}"
        recs.append(record(
            portal, titre=e.title[:140], employeur_affiche=e.employer or employer_page, lieu=" ; ".join(e.places),
            type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof, reference=ref, url_officielle=url,
            lien_candidature=page_url, date_publication=date,
            statut="fermée" if e.closed else "ouverte" if opened else "à recontrôler",
            http=doc[0] if doc else 200, erreur=error, alertes=alerts, _text=text[:1500], **text_fields(text),
            _sans_lien=not (e.href or e.pdf_url)))
    return recs, dict(
        statut_parcours="partiel" if unreadable else "complet", entrees=len(entries), spontanees=spontaneous,
        pdf_lus=sum(1 for _, d in docs.values() if not d.error), pdf_illisibles=sum(1 for _, d in docs.values() if d.error),
        remarque=f"{len(entries)} annonce(s) dans la page, {len(pdfs)} fiche(s) PDF"
        + (f", {spontaneous} candidature(s) spontanée(s) ignorée(s)" if spontaneous else "")
        + (f" ; PDF non lu : {', '.join(unreadable[:3])}" if unreadable else ""))


RTE_HOST = "rte-france.com"
RTE_LISTING = "https://www.rte-france.com/carrieres/recrutement"
RTE_FEED = "https://www.rte-france.com/espace-carriere/filter"
RTE_PAGE_SIZE = 21  # taille demandée par la page publique, hors encarts promotionnels


def is_rte_listing(url):
    """Adresses Carrières RTE qui mènent à la même liste publique."""
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    return ((host_of(url) == RTE_HOST and path in {"/carrieres", "/carrieres/recrutement", "/carrieres/alternance-stage"})
            or (host_of(url) == "recrutement." + RTE_HOST and path == "/search"))


def a_rte(portal):
    """La page RTE hydrate ses résultats depuis une recherche JSON publique et paginée."""
    portal = dict(portal, url=RTE_LISTING)
    resume = portal.get("resume_pages") or {}
    items = dict(resume.get("items", {}))
    expected, first = resume.get("total"), resume.get("page", 1)
    problem, cursor = "", None
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
    for page in range(first, first + MAX_REQUESTS + 1):
        status, _, body, error = get(RTE_FEED, headers=headers, allow=lambda target: target == RTE_FEED,
                                    data=json.dumps(dict(page=page, length=RTE_PAGE_SIZE, ieg=False, distance=5)))
        if status != 200:
            problem = f"page {page} de la liste RTE illisible (HTTP {status}) {error}".strip()
            if items:
                cursor = dict(items=items, total=expected, page=page)
            break
        try:
            listing = json.loads(body)["offres"]
            total, page_items = listing["count"], listing["items"]
            if type(total) is not int or total < 0 or not isinstance(page_items, list):
                raise ValueError("compteur ou liste invalide")
        except (KeyError, ValueError, TypeError):
            problem = f"format de la liste RTE inattendu à la page {page}"
            break
        expected = total if expected is None else expected
        before = len(items)
        for item in page_items:
            if not isinstance(item, dict) or item.get("type") == "push_communiquant":
                continue
            address = urljoin(RTE_LISTING + "/", item.get("url") or "")
            if (item.get("id") and item.get("title") and host_of(address) == RTE_HOST
                    and in_portal(address, RTE_LISTING) and address.rstrip("/") != RTE_LISTING):
                items.setdefault(str(item["id"]), (item, address))
        if total != expected:
            problem = "le nombre d'offres RTE a changé pendant la lecture"
            if resume:  # les numéros de page ne désignent plus la même liste
                return a_rte(dict(portal, resume_pages=None, previous_records=()))
        if page * RTE_PAGE_SIZE >= total or len(items) == before:
            break
    if expected is not None and len(items) != expected:
        problem = problem or f"{len(items)}/{expected} annonces RTE distinctes lues"

    def one(entry):
        item, url = entry
        title, declared = item["title"], item.get("contrat") or ""
        contract, proof, conflict = entry_contract(title, declared, "", url)
        status, final, body, error = get(url, allow=lambda target: in_portal(target, RTE_LISTING))
        readable = status == 200 and in_portal(final, RTE_LISTING)
        full = main_content(body) if readable else ""
        try:
            published = dt.datetime.fromtimestamp(int(item.get("date")), dt.UTC).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            published = ""
        return record(portal, titre=title, reference=item.get("reference") or str(item["id"]),
                      employeur_affiche="RTE Réseau de transport d'électricité",
                      lieu=item.get("departement") or item.get("region") or "", type_contrat_declare=declared,
                      contrat_confirme=contract, preuve_contrat=proof, url_officielle=url, lien_candidature=url,
                      date_publication=published, statut="ouverte", http=status,
                      erreur="" if readable else error or f"HTTP {status}",
                      alertes=([] if readable else ["détail illisible"]) + ([conflict] if conflict else []),
                      _text=full[:1500], **text_fields(full, teletravail="oui" if item.get("teletravail") is True else ""))

    cached = {r["url_officielle"]: r for r in portal.get("previous_records", ()) if not r["erreur"]}

    def read_detail(entry):
        return cached.get(entry[1]) or one(entry)

    with cf.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(copy_context().run, read_detail, item) for item in items.values()]
        records = [future.result() for future in futures]
    state = "partiel" if records and problem else "inaccessible" if problem else "complet"
    missing = sum("détail illisible" in row["alertes"] for row in records)
    note = problem or f"{len(records)} annonces lues sur le portail RTE"
    if missing:
        note += f" · {missing} détails indisponibles"
    return records, dict(statut_parcours=state, remarque=note, resume_pages=cursor,
                         liste_lue=len(items) if expected is not None else None,
                         total_annonce=expected if expected is not None and len(items) <= expected else None)


def a_flatchr(portal, *, response=None):
    scope = flatchr_scope(portal["url"])
    if not scope:
        return [], dict(statut_parcours="lien erroné", remarque="Lien Flatchr sans organisation identifiée")
    status, final, body, error = response or get(portal["url"], allow=lambda target: flatchr_scope(target) == scope)
    if status != 200:
        return [], dict(statut_parcours="inaccessible", remarque=f"Flatchr : HTTP {status} {error}")
    if flatchr_scope(final) != scope:
        return [], dict(statut_parcours="inaccessible", remarque="Flatchr : redirection vers une autre organisation")
    node = BeautifulSoup(body, "html.parser").find("script", id="__NEXT_DATA__")
    try:
        page = json.loads(node.string if node and node.string else "{}")
        props = page["props"]
        data = props.get("data") or {}
        records = data.get("items", [] if props.get("hasNoActiveDiffusion") is True else None)
        organisation = scope.rsplit("/", 1)[-1]
        if (page["page"] != "/company/[companySlug]" or page["query"]["companySlug"] != organisation
                or not isinstance(records, list)):
            raise ValueError("liste d'annonces ou organisation illisible")
    except (AttributeError, KeyError, TypeError, ValueError):
        return [], dict(statut_parcours="partiel", remarque="Flatchr : liste publique non reconnue")

    locale = page.get("locale") or "fr"
    locale = locale if re.fullmatch(r"[a-z]{2}", locale) else "fr"
    origin = urlsplit(final)
    base = f"{origin.scheme}://{origin.netloc}/{locale}/company/{organisation}/vacancy/"
    ads, malformed = [], 0
    for item in records:
        vacancy = item.get("vacancy") if isinstance(item, dict) else None
        if not isinstance(vacancy, dict) or item.get("published") not in (True, False):
            malformed += 1
            continue
        title, slug = str(vacancy.get("title") or ""), str(vacancy.get("slug") or "")
        if not item["published"] or SPONTANEOUS.search(title + " " + slug):
            continue
        employer = vacancy.get("company") or {}
        if (not title or not re.fullmatch(r"[a-z0-9_-]+", slug)
                or not isinstance(employer, dict) or employer.get("slug") != organisation):
            malformed += 1
            continue
        url = base + slug + "/"
        label = vacancy.get("contract_type") or ""
        missions, profile, description = [html_to_text(vacancy.get(key) or "") for key in ("mission", "profile", "description")]
        full = "\n\n".join(filter(None, [description, f"Missions\n{missions}" if missions else "",
                                         f"Profil\n{profile}" if profile else ""]))
        contract, proof, alert = entry_contract(title, label, full)
        address = vacancy.get("address") or {}
        city = address.get("locality") or vacancy.get("addressFormatted") or ""
        posted = item.get("publish_date") or item.get("created_at") or ""
        ads.append(record(portal, titre=title, employeur_affiche=employer.get("name", ""), lieu=city,
                          type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof,
                          reference=str(vacancy.get("id") or slug), url_officielle=url, lien_candidature=url,
                          date_publication=posted, statut="ouverte", http=status, alertes=[alert] if alert else [],
                          **text_fields(full, missions=missions, profil=profile)))
    newest = {}
    for ad in sorted(ads, key=lambda row: (row["date_publication"], row["reference"]), reverse=True):
        key = flatchr_republication_key(ad["url_officielle"], ad["titre"], ad["lieu"],
                                         ad["type_contrat_declare"], ad["description"])
        if key:
            winner = newest.setdefault(key, ad)
            if winner is not ad:
                ad["doublon_de"] = winner["url_officielle"]
    duplicate_count = sum(bool(ad["doublon_de"]) for ad in ads)
    state = "partiel" if malformed else "complet"
    if not malformed and not ads and all(
            urlsplit(url).query in ("", "iframe=true", "iframe=false") for url in (portal["url"], final)):
        state = EMPTY_PORTAL
    return ads, dict(statut_parcours=state,
                     remarque=f"Flatchr : {len(ads) - duplicate_count} annonce(s) distincte(s)"
                              + (f", {duplicate_count} republication(s)" if duplicate_count else "")
                              + (f", {malformed} entrée(s) illisible(s)" if malformed else ""))


def _elsatis_text(x):
    """(texte complet, missions, profil) d'une annonce Elsatis ; les rubriques HTML de la source sont conservées."""
    missions = html_to_text(x.get("jobDescription") or x.get("job_description") or "")
    profil = html_to_text(x.get("profileDescription") or x.get("profile_description") or "")
    company = html_to_text(x.get("descriptionCompany") or x.get("description_company") or "")
    parts = (("Missions", missions), ("Profil", profil), ("Entreprise", company))
    return "\n\n".join(f"{title}\n{text}" for title, text in parts if text), missions, profil


def _elsatis_date(*values):
    return next((v[:10] for v in values if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v)), "")


expand_elsatis = read_elsatis_list  # remplaçable dans les tests (aucun navigateur)


def a_elsatis(portal):
    """Portail Elsatis : annonces chargées par la page elle-même après son ouverture (voir `elsatis_portal`). Seule une
    lecture complète (compteur affiché atteint, plus de « Afficher plus ») est « complète » ; seule une liste vide
    ainsi confirmée est « aucune offre publiée »."""
    listing = expand_elsatis(portal["url"], lambda target: in_perimeter(portal, target), take_request)
    counts = (dict(liste_lue=len(listing.offers), total_annonce=listing.visible)
              if listing.visible is not None and len(listing.offers) <= listing.visible else {})
    if not listing.complete:  # les annonces déjà lues sont conservées ; l'absence des autres ne prouve rien
        read = f" ({len(listing.offers)} annonce(s) lue(s)" + (
            f" sur {listing.visible} affichées)" if listing.visible is not None else ")") if listing.offers else ""
        return _elsatis_records(portal, listing), dict(
            statut_parcours="partiel", remarque="relevé incomplet : " + (listing.reason or "liste non lue") + read,
            **counts)
    if listing.empty:
        return [], dict(statut_parcours=EMPTY_PORTAL, remarque="Elsatis : liste vide confirmée par la lecture complète")
    return _elsatis_records(portal, listing), dict(
        statut_parcours="complet",
        remarque=f"Elsatis : {len(listing.offers)} annonce(s) lue(s)"
        + (f" sur {listing.visible} affichées par le portail" if listing.visible is not None else ""), **counts)


def _elsatis_records(portal, listing):
    recs = []
    for x in listing.offers:
        title = H.unescape(x.get("title") or x.get("postName") or "").strip()
        label = x.get("contract") or x.get("employmentName") or ""
        full, missions, profil = _elsatis_text(x)
        url = offer_url(portal["url"], x["code"])
        posted = _elsatis_date(x.get("externalPublicationDate"), x.get("internalPublicationDate"), x.get("release"),
                               x.get("creation"))
        until = _elsatis_date(x.get("externalPublicationEndDate"), x.get("validity"))
        contract, proof, conflict = entry_contract(title, label, full, url)
        open_ad = x.get("active") is not False and str(x.get("status") or "ENCOURS").upper() == "ENCOURS"
        recs.append(record(
            portal, titre=title[:140], employeur_affiche=listing.name, lieu=x.get("location") or "",
            type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof, reference=str(x["code"]),
            url_officielle=url, lien_candidature=url, date_publication=posted, valable_jusqua=until,
            statut="ouverte" if open_ad else "à recontrôler", http=200,
            alertes=([conflict] if conflict else []) + date_flags(posted, until, full, contract),
            _text=full[:1500], **text_fields(full, missions=missions, profil=profil)))
    return recs


# ---------------------------------------------------------------- API publiques de plateformes (Oracle HCM, BPCE)
ORACLE_SITE = re.compile(r"/hcmUI/CandidateExperience/(?P<lang>[A-Za-z]{2})/sites/(?P<site>[A-Za-z0-9_]+)")
API_PAGE = 100  # annonces demandées par appel
API_MAX_PAGES = 60  # garde-fou : au-delà, la liste est « partielle »
BPCE_ORIGIN = "https://recrutement.bpce.fr"


def oracle_scope(url):
    """(origine, langue, site) d'un portail Oracle HCM « CandidateExperience », sinon None."""
    o = urlsplit(url or "")
    m = ORACLE_SITE.search(o.path)
    if m and platform_name(host_of(url)) == "oracle_hcm":
        return f"{o.scheme}://{o.netloc}", m["lang"].lower(), m["site"]
    return None


def _api_json(url, *, own, data=None):
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if data is not None else {})}
    status, _, body, error = get(url, data=data, headers=headers, allow=own)
    if status != 200:
        return None, f"HTTP {status} {error}".strip()
    try:
        return json.loads(body), ""
    except ValueError:
        return None, "réponse JSON illisible"


def _api_outcome(name, recs, total, problem):
    """Complet seulement si le total annoncé par l'API est atteint ; sinon partiel avec le motif."""
    counts = dict(liste_lue=len(recs), total_annonce=total) if total is not None and len(recs) <= total else {}
    if problem or total is None or len(recs) < total:
        reason = problem or f"{len(recs)} annonce(s) lue(s) sur {total}"
        return recs, dict(statut_parcours="partiel" if recs else "inaccessible", remarque=f"{name} : {reason}", **counts)
    return recs, dict(statut_parcours="complet" if recs else EMPTY_PORTAL,
                      remarque=f"{name} : {len(recs)} annonce(s) sur {total} annoncées", **counts)


def _read_api_pages(call, items_of, key_of):
    """Appelle `call(offset)` page après page jusqu'au total annoncé -> (annonces par clé, total, motif d'arrêt)."""
    seen, total, problem, offset = {}, None, "", 0
    for _ in range(API_MAX_PAGES):
        data, problem = call(offset)
        if data is None:
            break
        total, batch = items_of(data, total)
        fresh = [x for x in batch if key_of(x) not in seen]
        seen.update((key_of(x), x) for x in fresh)
        offset += len(batch)
        if not batch or not fresh or (total is not None and len(seen) >= total):
            break
    else:
        problem = problem or "limite de pages atteinte"
    return seen, total, problem


def a_oracle_hcm(portal):
    """Portail Oracle HCM (« CandidateExperience ») : liste publique `recruitingCEJobRequisitions`, sans navigateur ni
    compte. Le contrat n'est pas publié par l'API : il vient du titre et du résumé (règle commune)."""
    scope = oracle_scope(portal["url"])
    if scope is None:
        return [], dict(statut_parcours="partiel", remarque="Oracle HCM : site de recrutement non reconnu dans l'adresse")
    origin, lang, site = scope

    def own(target):
        return host_of(target) == host_of(origin)

    def call(offset):
        return _api_json(f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                         f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
                         f"limit={API_PAGE},offset={offset},sortBy=POSTING_DATES_DESC", own=own)

    def items_of(data, total):
        first = (data.get("items") or [{}])[0]
        return first.get("TotalJobsCount", total), first.get("requisitionList") or []

    seen, total, problem = _read_api_pages(call, items_of, lambda x: str(x.get("Id")))
    recs = []
    for x in seen.values():
        title, text = (x.get("Title") or "").strip(), txt(x.get("ShortDescriptionStr") or "")
        url = f"{origin}/hcmUI/CandidateExperience/{lang}/sites/{site}/job/{x['Id']}"
        label = x.get("ContractType") or x.get("WorkerType") or ""
        contract, proof, conflict = entry_contract(title, label, text, url)
        recs.append(record(
            portal, titre=title[:140], employeur_affiche=x.get("Organization") or "", lieu=x.get("PrimaryLocation") or "",
            type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof, reference=str(x["Id"]),
            url_officielle=url, lien_candidature=url, date_publication=(x.get("PostedDate") or "")[:10],
            valable_jusqua=(x.get("PostingEndDate") or "")[:10], statut="ouverte", http=200,
            alertes=([conflict] if conflict else []) + date_flags(x.get("PostedDate"), x.get("PostingEndDate"), text, contract),
            _text=text[:1500], **text_fields(text)))
    return _api_outcome("Oracle HCM", recs, total, problem)


def a_bpce(portal):
    """recrutement.bpce.fr (Caisses d'Epargne, Banques Populaires…) : API de recherche publique de la page, filtrée par
    `tax_brands` comme l'adresse du portail. Une annonce se dédoublonne par `post_id` (`job_number` n'est pas unique)."""
    query = dict(parse_qsl(urlsplit(portal["url"]).query))
    api = f"{BPCE_ORIGIN}/app/wp-json/bpce/v1/search/jobs"

    def own(target):
        return host_of(target) == host_of(BPCE_ORIGIN)

    def call(offset):
        body = dict(lang="fr", keyword="", tax_brands=query.get("tax_brands", ""), external=query.get("external") == "true",
                    userID="", size=API_PAGE)
        body["from"] = offset
        return _api_json(api, own=own, data=json.dumps(body).encode())

    def items_of(data, total):
        page = data.get("data") or {}
        return page.get("total", total), page.get("items") or []

    seen, total, problem = _read_api_pages(call, items_of, lambda x: str(x.get("post_id")))
    recs = []
    for x in seen.values():
        title, label = (x.get("title") or "").strip(), ", ".join(x.get("contract") or [])
        text = html_to_text(x.get("description") or "")
        url = BPCE_ORIGIN + ((x.get("link") or {}).get("url") or "")
        contract, proof, conflict = entry_contract(title, label, text, url)
        recs.append(record(
            portal, titre=title[:140], employeur_affiche=", ".join(x.get("brand") or []), lieu=x.get("localisation") or "",
            type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof, reference=str(x["post_id"]),
            url_officielle=url, lien_candidature=(x.get("postulate_link") or {}).get("url") or url,
            date_publication=(x.get("date") or "")[:10], statut="ouverte", http=200,
            alertes=([conflict] if conflict else []) + date_flags(x.get("date"), "", text, contract),
            _text=text[:1500], **text_fields(text, teletravail=x.get("teletravail") or "")))
    return _api_outcome("API BPCE", recs, total, problem)


ADAPT = dict(teamtailor=a_teamtailor, recruitee=a_recruitee, smartrecruiters=a_smartrecruiters,
             workday=a_workday, flatchr=a_flatchr, elsatis=a_elsatis, oracle_hcm=a_oracle_hcm, bpce=a_bpce)
EXTERNE = re.compile(r"welcometothejungle|jobteaser|francetravail|indeed|linkedin|randstad\.fr/emploi", re.I)


def designated_wttj(portal):
    """Le renvoi officiel autorise uniquement l'organisation explicitement désignée."""
    proof = portal.get("preuve") or {}
    scope = wttj_scope(portal["url"])
    return bool(scope and (portal.get("manuels")
                           or (proof.get("site_officiel") and wttj_scope(proof.get("lien", "")) == scope)))


def deferred(portal):
    if platform_name(host_of(portal["url"])) == "wttj":
        return not designated_wttj(portal)
    return bool(EXTERNE.search(portal["url"]))


def platform_of(url):
    """Adaptateur d'après l'hôte (domaine ou sous-domaine d'une plateforme), jamais d'après un mot du chemin."""
    if is_elsatis(url):  # chemin `/recruitment/public/offers` possible sur le domaine propre de l'entreprise
        return "elsatis"
    name = platform_name(host_of(url))
    return name if name in ADAPT or name == "icims" else "site de l'entreprise"


# ---------------------------------------------------------------- rattachement
def tokens(name):
    stop = set("sas sa sarl snc eurl sasu sci gie sca de du des le la les l et d au aux".split())
    n = re.sub(r"[^a-z0-9 ]", " ", re.sub(r"[àâä]", "a", re.sub(r"[éèêë]", "e", re.sub(r"[îï]", "i", re.sub(r"[ôö]", "o", re.sub(r"[ùûü]", "u", (name or "").lower()))))))
    return [t for t in n.split() if t not in stop]


def _entity_matches(fiches, phrase):
    """(complètes, partielles) : fiches dont le nom complet figure dans `phrase` (mots contigus) ; fiches dont seul un
    début de nom (au moins deux mots, ex. la branche « Eiffage Énergie Systèmes ») y figure, avec ce début."""
    exact, partial = [], []
    for i, n in fiches:
        words = tokens(n)
        if not words:
            continue
        if " " + " ".join(words) + " " in phrase:
            exact.append((i, n))
            continue
        for size in range(len(words) - 1, 1, -1):
            head = " ".join(words[:size])
            if " " + head + " " in phrase:
                partial.append((i, n, head))
                break
    return exact, partial


def _surface(text, head):
    """Libellé de `head` (mots normalisés) tel qu'écrit dans `text` : « Eiffage Énergie Systèmes »."""
    words = re.findall(r"[^\W_]+(?:[-'’][^\W_]+)*", text or "")
    target = head.split()
    for start in range(len(words)):
        seen, end = [], start
        while end < len(words) and len(seen) < len(target):
            seen += tokens(words[end])
            end += 1
        if seen == target:
            return " ".join(words[start:end])
    return head


def _verify(reason):
    return "à vérifier : " + reason


def identify(fiches, employer="", title="", text=""):
    """Employeur de l'annonce parmi les `fiches` [(id, nom)] qui partagent son portail. Rend un dictionnaire :
    entreprise_id (fiche certaine ou None), rattachement (motif « à vérifier : … » si ambigu), candidats (fiches
    possibles) et branche (entité nommée par l'annonce, telle qu'écrite). Une fiche n'est retenue que si son nom complet
    figure dans l'annonce, seul ; plusieurs fiches de même nom (établissements) ou une branche sans l'entité précise
    ne suffisent pas. Le lieu du poste n'identifie jamais l'établissement employeur."""
    source = f"{employer} {title} {text[:1200]}"
    ok, partial = _entity_matches(fiches, " " + " ".join(tokens(source)) + " ")
    names = ", ".join(n for _, n in (ok or [(i, n) for i, n, _ in partial] or fiches)[:4])
    if len(ok) == 1:
        return dict(entreprise_id=ok[0][0], entreprise=ok[0][1], rattachement="employeur identifié dans l'annonce",
                    candidats=[ok[0][0]], branche=_surface(source, " ".join(tokens(ok[0][1]))))
    if len(ok) > 1 and len({tuple(tokens(n)) for _, n in ok}) == 1:
        reason = f"établissement non précisé ({len(ok)} fiches « {ok[0][1]} » : {len(ok)} établissements)"
        return dict(entreprise_id=None, rattachement=_verify(reason), candidats=[i for i, _ in ok],
                    branche=_surface(source, " ".join(tokens(ok[0][1]))))
    if ok:
        common = [w for w, *rest in zip(*[tokens(n) for _, n in ok], strict=False) if all(r == w for r in rest)]
        return dict(entreprise_id=None, rattachement=_verify(f"plusieurs entités possibles ({names})"),
                    candidats=[i for i, _ in ok], branche=_surface(source, " ".join(common)) if common else "")
    if partial:
        head = max((h for _, _, h in partial), key=len)
        reason = f"entité précise non établie : l'annonce nomme « {_surface(source, head)} » (fiches possibles : {names})"
        return dict(entreprise_id=None, rattachement=_verify(reason), candidats=[i for i, _, h in partial if h == head],
                    branche=_surface(source, head))
    return dict(entreprise_id=None, rattachement=_verify(
        f"employeur non identifiable ({', '.join(n for _, n in fiches[:4])})"), candidats=[i for i, _ in fiches], branche="")


def attach(recs, fiches):
    """fiches : [(id, nom)] des fiches pointant vers le portail. Voir `identify` : rattachement uniquement si l'annonce
    identifie l'entité ; une fiche unique du portail est reprise (le relevé vérifie ensuite l'employeur affiché)."""
    for r in recs:
        found = identify(fiches, r["employeur_affiche"], r["titre"], r.get("_text", ""))
        r["candidats"], r["branche"] = found["candidats"], found["branche"]
        if len(fiches) == 1:
            i, n = fiches[0]
            ok = found["entreprise_id"] == i
            r["entreprise_id"], r["entreprise"] = i, n
            r["rattachement"] = "fiche unique du portail" + ("" if ok else " ; employeur affiché non confirmé")
            if ok and not set(tokens(n)) <= set(tokens(r["employeur_affiche"])):
                r["employeur_affiche"] = n; r["note"] = (r["note"] + " ; " if r["note"] else "") + "employeur déduit du texte de l'annonce"
        elif found["entreprise_id"] is not None:
            r["entreprise_id"], r["entreprise"], r["rattachement"] = found["entreprise_id"], found["entreprise"], found["rattachement"]
            if not r["employeur_affiche"] or not set(tokens(found["entreprise"])) <= set(tokens(r["employeur_affiche"])):
                r["employeur_affiche"] = found["entreprise"]; r["note"] = (r["note"] + " ; " if r["note"] else "") + "employeur déduit du texte de l'annonce"
        else:
            r["entreprise_id"], r["rattachement"] = None, found["rattachement"]
    return recs



# ---------------------------------------------------------------- inventaire des portails
_SEG = re.compile(r"^(carrieres?|careers?|recrutements?|recrute|nous-rejoindre|rejoignez-nous|rejoins-nous|join-us|offres?"
                  r"(-d-?emplois?)?|offres-demploi|nos-offres(-demploi)?|emplois?|jobs?|jobs-careers|"
                  r"emplois-et-carrieres(\.html)?|talents?|travailler-chez-nous|vos-opportunites|opportunites)$", re.I)
_OFFER_PAGE = re.compile(r"/(offre-emploi|offres?|jobs?)/[^/?#]{5,}", re.I)


def portal_from_offer(url):
    """Adresse ATS déduite d'une piste ; jamais une preuve de rattachement."""
    if not web_url(url):
        return ""
    found = detect_careers(url)
    if found:
        return found["public_url"]
    o = urlsplit(url)
    if o.scheme != "https" or not o.hostname or o.username or o.port not in (None, 443):
        return ""
    name = platform_name(host_of(url))
    return portal_root(url) if name and name != "wttj" else ""


def portals_from_database(db) -> list[dict]:
    """Liens Carrières / recrutement déjà enregistrés (portails, carrières déclarées, pages Carrières des sites,
    parents de pages d'offres connues), regroupés par adresse. Une candidature spontanée reste un canal de contact."""
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM companies")}
    sites = {r["company_id"]: r["url"] for r in db.query("SELECT company_id, url FROM company_sites")}
    groups: dict[str, dict] = {}

    def add(company_id, url, source, kind, row=None):
        g = groups.setdefault(url.rstrip("/"), dict(url=url, fiches={}, trouve_sur=set(), types=set()))
        g["fiches"][company_id] = names.get(company_id, ""); g["trouve_sur"].add(source or ""); g["types"].add(kind)
        if row and row["read_state"] == WRONG_LINK:  # dernière lecture : lien invalide, la fiche reste à découvrir
            g.setdefault("liens_errones", set()).add(company_id)
        if source == MANUAL_PORTAL_SOURCE:
            g.setdefault("manuels", set()).add(company_id)
        if wttj_scope(url):  # vitrine WTTJ : preuve propre à la fiche, valable tant que son site officiel est celui lu
            proof = dict(site_officiel=(row["proof_page"] if row else ""), lien=(row["proof_link"] if row else ""),
                         texte=(row["title"] if row else ""))
            if (proof["site_officiel"] and wttj_scope(proof["lien"]) == wttj_scope(url)
                    and host_of(proof["site_officiel"]) == host_of(sites.get(company_id, ""))):
                g.setdefault("preuves", {})[company_id] = proof
        elif source == DISCOVERY_SOURCE and sites.get(company_id):  # page découverte : son périmètre reste celui du site officiel
            g.setdefault("preuve", dict(site_officiel=sites[company_id], lien=url, texte=""))
        if source == DESIGNATED_SOURCE and row and row["proof_page"]:  # portail désigné : le domaine enregistré fait foi
            g.setdefault("designated_hosts", set()).update(h for h in (host_of(url), host_of(row["proof_link"])) if h)
            g.setdefault("preuve", dict(site_officiel=row["proof_page"], lien=row["proof_link"], texte=row["title"],
                                        designation=True))

    for r in db.query("SELECT company_id, kind, url, source, proof_page, proof_link, title, read_state FROM company_channels WHERE kind IN "
                      "('portal','declared_careers','form','declared_site','offer') "
                      "ORDER BY company_id, id"):
        url = (r["url"] or "").strip()
        if r["kind"] == "offer":
            if listing := portal_from_offer(url):
                add(r["company_id"], listing, "portail déduit d'une offre ATS", "deduit")
            continue
        parts = [s for s in urlsplit(url).path.split("/") if s]
        if r["kind"] in ("portal", "declared_careers"):
            add(r["company_id"], url, r["source"], r["kind"], r)
        elif parts and _SEG.match(parts[-1]) and "candidature-spontanee" not in url and len(parts) <= 3:
            add(r["company_id"], url, r["source"], r["kind"])
            site = sites.get(r["company_id"], "")
            if site and same_site(host_of(url), host_of(site)):
                # Page Carrières du site officiel connue par un autre canal (contact, site déclaré) : lue comme une page
                # découverte, elle devient le portail de la fiche et son issue s'affiche.
                g = groups[url.rstrip("/")]
                g["types"].add("decouvert")
                g.setdefault("preuve", dict(site_officiel=site, lien=url, texte=""))
        elif (m := _OFFER_PAGE.search(urlsplit(url).path)) and r["kind"] == "form":  # parent d'une page d'offre connue
            seg = m.group(1).lower(); sp = urlsplit(url)
            listing = f"{sp.scheme}://{sp.netloc}/" + ("offres-emploi" if seg == "offre-emploi" else seg + "/")
            add(r["company_id"], listing, "parent de pages d'offres déjà enregistrées", "deduit")
    portals = []
    for g in groups.values():
        portals.append(with_proofs(dict(
            url=g["url"], plateforme=platform_of(g["url"]), fiches=sorted(g["fiches"].items()),
            trouve_sur=sorted(g["trouve_sur"]), types=sorted(g["types"]),
            entreprise=" / ".join(g["fiches"].values()), manuels=sorted(g.get("manuels", ())),
            **({"preuve": g["preuve"]} if "preuve" in g else {}),
            **({"designated_hosts": sorted(g["designated_hosts"])} if "designated_hosts" in g else {}),
            **({"liens_errones": sorted(g["liens_errones"])} if "liens_errones" in g else {}),
            **({"preuves": g["preuves"]} if "preuves" in g else {}))))
    return merge_shared(portals)


def with_proofs(portal):
    """Vitrine WTTJ : seules les fiches dont le site officiel y renvoie en gardent l'attribution ; les autres, qui
    connaissent seulement l'adresse, sont écartées. `preuve` est celle de la première fiche prouvée."""
    proofs = portal.get("preuves") or {}
    if wttj_scope(portal["url"]) and (proofs or portal.get("manuels")):
        every = dict(tuple(portal.get("sans_preuve", ())) + tuple(portal["fiches"]))
        trusted = set(proofs) | set(portal.get("manuels", ()))
        portal["fiches"] = [f for f in sorted(every.items()) if f[0] in trusted]
        portal["sans_preuve"] = tuple(f for f in sorted(every.items()) if f[0] not in trusted)
        if proofs:
            portal["preuve"] = proofs[min(proofs)]
        portal["entreprise"] = " / ".join(n for _, n in portal["fiches"])
    return portal


def discover_portals(db, portals, progress=None, cancelled=None):
    """Fiches qui ont un site officiel (niveau confirmé ou probable) mais aucun portail exploitable : lit la page
    d'accueil du site et suit les liens « Carrières », « Recrutement », « Offres d'emploi » de son périmètre (le site et
    les plateformes de recrutement qu'il désigne). -> (portails découverts, bilan). Chaque portail garde la page
    d'accueil et le lien qui prouvent son rattachement (`preuve`). Rien n'est écrit en base.
    shortcut: aucune mémoire des sites déjà examinés sans lien ; un site sans lien est relu à chaque découverte."""
    covered = {i for p in portals if not deferred(p) and p["plateforme"] != "icims" and not link_problem(p["url"])
               for i, _ in p["fiches"] if i not in p.get("liens_errones", ())}  # un lien invalide ne couvre pas la fiche
    targets: dict[int, set[str]] = {}  # vitrine WTTJ connue dont la preuve manque ou a expiré : à redécouvrir
    for p in portals:
        if scope := wttj_scope(p["url"]):
            for i, _ in (tuple(p["fiches"]) if deferred(p) else ()) + tuple(p.get("sans_preuve", ())):
                targets.setdefault(i, set()).add(scope)
    rows = db.query("SELECT s.company_id AS id, c.name AS name, s.url AS url FROM company_sites s JOIN companies c "
                    "ON c.id=s.company_id WHERE s.level IN ('confirme','probable') AND s.url<>'' ORDER BY s.company_id")
    eligible = [r for r in rows if r["id"] not in covered or r["id"] in targets]
    todo = eligible  # aucune fiche écartée d'office ; chaque requête reste soumise au budget du relevé
    cancelled = cancelled or (lambda: False)
    stats = dict(sites=len(todo), pages=0, sans_lien=0, inaccessibles=0, hors_site=0, non_examines=len(eligible) - len(todo))

    lock, seen = threading.Lock(), [0]

    def host(u):
        return (urlsplit(u).hostname or "").removeprefix("www.")

    def one(r):
        if cancelled():
            return r, "annulee", "", []
        status, final, body, _ = get(r["url"], allow=lambda target: host(target) == host(r["url"]))
        with lock:
            seen[0] += 1
            survey_activity.searching(seen[0], len(todo))
            if progress:
                progress(seen[0] / max(1, len(todo)), f"{seen[0]}/{len(todo)} sites examinés")
        if status != 200:
            return r, "inaccessible", "", []
        if host(final) != host(r["url"]):  # redirection vers un autre site : hors périmètre
            return r, "hors_site", final, []
        # À défaut de page Carrières du site, un lien de recrutement vers un portail d'un autre domaine (portail de groupe,
        # plateforme) ; jamais un agrégateur ni un réseau social.
        external = [link for link in designated_links(body, final, host(final)) if link["kind"] in ("ats", "externe")
                    and (CAREER_TEXT.match(link["texte"]) or OFFER_LIST_TEXT.match(link["texte"]))]
        return r, "", final, career_links(body, final) or external[:2]

    found: dict[str, dict] = {}
    for r, problem, home, links in pmap(lambda row: _with_request_budget(one, row), todo):
        stats["inaccessibles"] += problem == "inaccessible"
        stats["hors_site"] += problem == "hors_site"
        stats["sans_lien"] += not problem and not links
        if r["id"] in covered:  # déjà un portail actif : seule la vitrine WTTJ connue est redécouverte
            links = [x for x in links if wttj_scope(x["url"]) in targets[r["id"]]]
        for link in links:
            # Portail d'un autre hôte de l'organisation (« x.jobs.groupe.fr ») : désigné par le site officiel, son
            # domaine fait foi et le périmètre d'employeur d'un portail de groupe s'applique.
            sister = not same_site(host_of(link["url"]), host(home)) and not is_ats(host_of(link["url"]))
            proof = dict(site_officiel=home, lien=link["url"], texte=link["texte"], **({"designation": True} if sister else {}))
            g = found.setdefault(link["url"].rstrip("/"), dict(
                url=link["url"], plateforme=platform_of(link["url"]), fiches=[], trouve_sur=[home], types=["decouvert"],
                preuve=dict(proof), preuves={}, **({"designated_hosts": [host_of(link["url"])]} if sister else {})))
            g["preuves"].setdefault(r["id"], dict(proof))
            if (r["id"], r["name"]) not in g["fiches"]:
                g["fiches"].append((r["id"], r["name"]))
    for g in found.values():
        g["entreprise"] = " / ".join(n for _, n in g["fiches"])
    stats["pages"] = len(found)
    return list(found.values()), stats


# ---------------------------------------------------------------- portails partagés
def portal_identity(portal):
    """Identité d'un portail : (plateforme, organisation) pour une plateforme de recrutement (toutes ses adresses
    désignent la même liste) ; adresse sans schéma, « www. », requête ni barre finale pour une page de site."""
    url, platform = portal["url"], portal.get("plateforme") or platform_of(portal["url"])
    if is_rte_listing(url):
        return ("rte", RTE_HOST)
    if platform in ADAPT and platform != "site de l'entreprise":
        return (platform, portal_scope(url))
    o = urlsplit(url)
    return ("url", (o.hostname or "").lower().removeprefix("www.") + o.path.rstrip("/"))


def merge_shared(portals):
    """Un seul portail par identité : les fiches (et leurs preuves) qui le partagent sont réunies, de sorte qu'il soit
    collecté une seule fois par relevé. L'adresse conservée est la plus précise (chemin le plus long)."""
    groups: dict[tuple, list[dict]] = {}
    for p in portals:
        groups.setdefault(portal_identity(p), []).append(p)
    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        best = max(group, key=lambda p: (bool(p.get("manuels")), len(urlsplit(p["url"]).path.rstrip("/"))))
        fiches = dict(f for p in group for f in p.get("fiches", ()))
        out = dict(best, fiches=sorted(fiches.items()),
                   trouve_sur=sorted({x for p in group for x in p.get("trouve_sur", ())}),
                   types=sorted({x for p in group for x in p.get("types", ())}),
                   manuels=sorted({i for p in group for i in p.get("manuels", ())}),
                   entreprise=" / ".join(n for _, n in sorted(fiches.items())),
                   adresses_fusionnees=sorted({u for p in group for u in (p["url"], *p.get("adresses_fusionnees", ()))}))
        if hosts := sorted({h for p in group for h in p.get("designated_hosts", ())}):
            out["designated_hosts"] = hosts
        proofs = {k: v for p in group for k, v in (p.get("preuves") or {}).items()}
        if proofs:
            out["preuves"] = proofs
        if not out.get("preuve"):
            out.update(next(({"preuve": p["preuve"]} for p in group if p.get("preuve")), {}))
        merged.append(with_proofs(out))
    return merged


# ---------------------------------------------------------------- collecte
def _run_portal(p):
    t0 = time.time()
    if deferred(p):
        return p, [], dict(statut_parcours="externe (différé)", remarque="site d'emploi externe : chantier ultérieur")
    if p["plateforme"] == "icims":
        s, _, _, _ = get(p["url"])
        return p, [], dict(statut_parcours="inaccessible" if s != 200 else "partiel",
                           remarque="iCIMS : liste rendue par JavaScript, sans flux public exploité ; aucune annonce relevée")
    p.pop("_designations", None)
    page = p  # vitrine WTTJ désignée par son profil : lue sur sa liste « /jobs »
    if wttj_scope(p["url"]) and WTTJ_PROFILE_PATH.fullmatch(urlsplit(p["url"]).path.rstrip("/")):
        page = dict(p, url=p["url"].rstrip("/") + "/jobs")
    try:
        adapter = a_rte if is_rte_listing(p["url"]) else ADAPT.get(p["plateforme"], a_generique)
        from . import career_ops_reader  # import tardif : le lecteur dépend de ce module
        recs, backup = None, ""
        from . import wttj_embed_reader  # import tardif : le lecteur dépend de ce module
        embedded = wttj_embed_reader.read(p)  # flux officiel d'une vitrine désignée ; None : lecture HTML d'origine
        if embedded is not None:
            recs, info = embedded
        elif career_ops_reader.handles(p["url"]):  # Greenhouse, Lever, Ashby : Career Ops d'abord, lecteur natif en secours
            try:
                recs, info = career_ops_reader.read_board(p)
                if info["statut_parcours"] == "partiel" and not recs:  # rien d'exploitable : le lecteur natif tente sa chance
                    backup, recs = f"lecture partielle sans annonce exploitable, {info['remarque']}", None
            except career_ops_reader.Unavailable as unavailable:
                backup = str(unavailable)
        if recs is None:
            recs, info = adapter(page)
            if backup:
                info["remarque"] = f"{info['remarque']} ; lecteur natif en secours (Career Ops : {backup})".lstrip(" ;")
                if not recs and info["statut_parcours"] == "complet":  # deux lectures insuffisantes : jamais « aucune offre »
                    info["statut_parcours"] = "partiel"
        if (embedded is None and host_of(page["url"]) == "welcometothejungle.com"
                and WTTJ_JOBS_PATH.fullmatch(urlsplit(page["url"]).path.rstrip("/"))
                and (info["statut_parcours"] == "sans liste d'annonces"
                     or (info["statut_parcours"] == "inaccessible" and "HTTP 403" in info["remarque"])
                     or any("HTTP 403" in ad.get("erreur", "") for ad in recs))):
            rendered, reading = _read_wttj_browser(page)
            if recs and reading["statut_parcours"] == "inaccessible":
                reading["statut_parcours"] = "partiel"
            recs, info = _keep_partial((recs, info, 1), rendered, reading), reading
        if adapter not in (a_generique, a_flatchr) and not recs and info["statut_parcours"] == "complet" and not urlsplit(page["url"]).query:
            info["statut_parcours"] = EMPTY_PORTAL
        if p.get("_designations"):  # portails désignés par cette page (lus ou non) : conservés avec le bilan
            info["designations"] = p["_designations"]
        if p.get("designated_hosts"):  # portail enregistré comme désigné : même périmètre d'employeur que lors de la désignation
            recs = [dict(r, designe=True) for r in recs]
    except Exception as ex:  # noqa: BLE001 - un portail en échec ne bloque pas les autres
        recs, info = [], dict(statut_parcours="inaccessible", remarque=f"erreur : {type(ex).__name__}: {str(ex)[:100]}")
    failed = sum(1 for r in recs if "détail illisible" in r["alertes"])
    if failed and info.get("statut_parcours") == "complet" and not is_rte_listing(p["url"]):
        info["statut_parcours"] = "partiel"
        info["remarque"] = (info.get("remarque", "") + f" ; {failed} détail(s) illisible(s)").lstrip(" ;")
    info["secondes"] = round(time.time() - t0)
    return p, recs, info


COMPLETE_PORTAL = ("complet", DIRECT_ONLY, EMPTY_PORTAL)
TEMPORARY_FAILURE = re.compile(
    r"\bHTTP (?:408|425|429|5\d\d)\b|timeout|timed out|délai dépassé|connectionreset|connectionerror",
    re.I,
)


def _retry_portal(result) -> bool:
    records, info, attempts = result
    if attempts != 1 or info["statut_parcours"] not in ("partiel", "inaccessible"):
        return False
    return bool(info.get("resume_pages") or TEMPORARY_FAILURE.search(info["remarque"]) or
                any(TEMPORARY_FAILURE.search(r.get("erreur") or "") for r in records))


def _offer_identity(row) -> tuple:
    if row["reference"] and row["plateforme"] != "site de l'entreprise":
        return row["plateforme"], portal_scope(row["portail"]), row["reference"]
    return "url", row["url_officielle"].rstrip("/")


def _keep_partial(previous, records, info):
    """Sur une autre tentative incomplète, garde les annonces obtenues avant l'échec."""
    if not previous or info["statut_parcours"] not in ("partiel", "inaccessible"):
        return records
    kept = {_offer_identity(r): r for r in previous[0]}
    for row in records:
        key = _offer_identity(row)
        if key not in kept or not row.get("erreur") or kept[key].get("erreur"):
            kept[key] = row
    return list(kept.values())


def _partial_after_block(previous, records, info):
    """Une reprise refusée (403, panne) après des annonces lues : le relevé reste partiel, avec le compteur de la
    première lecture, les annonces gardées et le motif du blocage. Sans annonce lue, il reste « inaccessible »."""
    read = sum(not row.get("erreur") for row in records)
    if info["statut_parcours"] != "inaccessible" or not previous or not read:
        return info
    total = previous[1].get("total_annonce") or read
    return dict(info, statut_parcours="partiel", liste_lue=read, total_annonce=max(total, read),
                remarque=f"{info['remarque']} ; {read} annonce(s) lue(s) avant le blocage, conservées")


def _dump_stall(stuck) -> None:
    """Pile de tous les fils au moment où des lectures sont abandonnées : la seule façon de savoir où elles bloquaient."""
    if STALL_DUMP_DIR is None:
        return
    try:
        STALL_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = STALL_DUMP_DIR / f"lecture-bloquee-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
        with path.open("w", encoding="utf-8") as stream:
            stream.write("Lectures abandonnées : " + ", ".join(stuck) + "\n\n")
            stream.flush()
            faulthandler.dump_traceback(file=stream, all_threads=True)
    except OSError:
        pass  # le diagnostic ne doit jamais empêcher la suite du relevé


def _collect_round(portals, results, cancelled):
    """`PORTAL_WORKERS` portails à la fois, au plus `PORTALS_PER_SITE` par site : un site déjà occupé laisse passer
    les suivants. Chaque lecture tourne dans son fil d'arrière-plan : une lecture encore bloquée après
    PORTAL_SECONDS + PORTAL_GRACE_SECONDS est abandonnée (portail « inaccessible », reprenable) et libère sa place ;
    son fil ne retient ni la ronde ni la fermeture de l'application. Les portails qui attendent restent disponibles
    à la reprise."""
    waiting = list(portals)
    reading: dict[str, int] = {}  # site -> portails en cours de lecture
    running: dict[str, tuple] = {}  # url -> (portail, site, début)
    finished: queue.Queue = queue.Queue()

    def read(portal, token):
        survey_activity.begin(portal["url"])
        try:
            result = _with_request_budget(_run_portal, portal)
        except BaseException as error:  # noqa: BLE001 - un fil qui meurt sans résultat bloquerait la ronde
            result = (portal, [], dict(statut_parcours="inaccessible",
                                       remarque=f"erreur : {type(error).__name__}: {str(error)[:100]}"))
        finished.put((token, result))

    def launch():
        while len(running) < PORTAL_WORKERS and not cancelled():
            index = next((i for i, p in enumerate(waiting)
                          if reading.get(site_key(p["url"]), 0) < PORTALS_PER_SITE), None)
            if index is None:
                return
            portal = waiting.pop(index)
            previous = results.get(portal["url"])
            if previous and previous[1].get("resume_pages"):
                portal = dict(portal, resume_pages=previous[1]["resume_pages"], previous_records=previous[0])
            site = site_key(portal["url"])
            reading[site] = reading.get(site, 0) + 1
            token = object()
            running[token] = (portal, site, time.monotonic())
            threading.Thread(target=copy_context().run, args=(read, portal, token), daemon=True,
                             name="portal-" + site).start()

    def release(token):
        portal, site, _ = running.pop(token)
        reading[site] -= 1
        survey_activity.end(portal["url"])
        return portal

    launch()
    while running:
        try:
            token, result = finished.get(timeout=POLL_SECONDS)
        except queue.Empty:
            limit = PORTAL_SECONDS + PORTAL_GRACE_SECONDS
            stuck = [t for t, (_, _, started) in running.items() if time.monotonic() - started > limit]
            if stuck:
                _dump_stall([running[t][0]["url"] for t in stuck])
                for t in stuck:
                    portal = release(t)
                    yield portal, [], dict(statut_parcours="inaccessible",
                                           remarque=f"délai du portail dépassé ({PORTAL_SECONDS + PORTAL_GRACE_SECONDS:.0f} s) : "
                                                    "lecture bloquée abandonnée")
            launch()
            continue
        if token not in running:  # résultat tardif d'une lecture déjà abandonnée
            continue
        release(token)
        yield result
        launch()


def portal_progress(results: dict, total: int) -> tuple[float, str]:
    complete = sum(info["statut_parcours"] in COMPLETE_PORTAL for _, info, _ in results.values())
    missing = total - len(results)
    to_check = len(results) - complete
    label = "portail complet" if complete == 1 else "portails complets"
    return len(results) / max(1, total), f"{complete} {label} sur {total} · {missing} à parcourir · {to_check} à vérifier"


def collect(portals, progress=None, cancelled=None, *, previous=None, checkpoint=None):
    """Relève les portails, conserve les résultats intermédiaires et retente les pannes passagères une fois."""
    global TODAY
    TODAY = today()
    _pdfs.clear()
    portals = [p for p in merge_shared(portals) if not STAFFING_AGENCIES.search(site_key(p["url"]))]
    results = dict(previous or {})
    cancelled = cancelled or (lambda: False)
    survey_activity.start(len(portals), results)

    def flush(batch):
        if batch and checkpoint:
            checkpoint(results, *_summarize(batch))
        batch.clear()

    def run(round_portals):
        batch = []
        next_save = time.monotonic()
        for portal, records, info in _collect_round(round_portals, results, cancelled):
            url = portal["url"]
            before = results.get(url)
            attempts = before[2] + 1 if before else 1
            records = _keep_partial(before, records, info)
            info = _partial_after_block(before, records, info)
            results[url] = (records, info, attempts)
            batch.append((portal, records, info))
            if len(batch) >= 12 or time.monotonic() >= next_save:
                flush(batch)
                next_save = time.monotonic() + CHECKPOINT_SECONDS
            if progress:
                progress(*portal_progress(results, len(portals)))
        flush(batch)

    run([p for p in portals if p["url"] not in results])
    run([p for p in portals if p["url"] in results and _retry_portal(results[p["url"]])])
    survey_activity.stop()
    return _summarize((p, *results[p["url"]][:2]) for p in portals if p["url"] in results)


def _summarize(results):
    """Rattachement et dédoublonnage dans l'ordre de l'inventaire, après les éventuelles reprises."""
    seen, records, bilan = {}, [], []
    for p, rows, info in results:
        recs = [dict(row) for row in rows]
        attach(recs, p["fiches"])
        recs, outside = _employer_perimeter(recs)
        for r in recs:
            if r["entreprise_id"] in (p.get("preuves") or {}):
                r["preuve"] = p["preuves"][r["entreprise_id"]]  # chaque fiche s'appuie sur la page de SON site officiel
            apply_end_date(r)
            key = _offer_identity(r)
            if key in seen:
                r["doublon_de"] = seen[key]
            else:
                seen[key] = r["url_officielle"]
            r["a_recontroler"] = (bool(r["alertes"]) or r["contrat_confirme"].startswith("indice")
                                  or r["statut"] != "ouverte" or not r["entreprise_id"])
            r["domaine"] = domain_of(r["titre"], r["details"].get("missions") or r["description"])
            r.pop("_text", None); r.pop("pays", None)
        sa = [r for r in recs if r["contrat_confirme"] in ("stage", "alternance") and not r["doublon_de"]]
        parcours, remarque = info["statut_parcours"], info["remarque"]
        pending = [r for r in recs if r["statut"] == "à recontrôler" and not r["doublon_de"]]
        lecture = parcours
        if parcours == "complet" and pending and not any(r["statut"] == "ouverte" for r in recs):
            # Annonces lues dont l'ouverture n'est pas prouvée (fiche PDF ancienne…) : la liste est lue en entier (elle
            # prouve les retraits) mais l'écran ne doit afficher ni « aucune offre active » ni offres confirmées.
            lecture = "partiel"
            remarque = f"{remarque} ; {len(pending)} annonce(s) dont l'ouverture n'est pas prouvée".lstrip(" ;")
        bilan.append(dict(portail=p["url"], plateforme=p["plateforme"], entreprises=[i for i, _ in p["fiches"]],
                          adresses_fusionnees=p.get("adresses_fusionnees", []),
                          perimetre=portal_scope(p["url"]),
                          perimetres=sorted(set(info.get("perimetres", ())) |
                                           {portal_scope(r["url_officielle"]) for r in recs if r["url_officielle"]}),
                          parcours=parcours, lecture=lecture, annonces_listees=len(recs), stages_alternances=len(sa),
                          liste_lue=info.get("liste_lue"), total_annonce=info.get("total_annonce"),
                          decouvert="decouvert" in p.get("types", []), preuve=p.get("preuve") or {},
                          preuves=p.get("preuves") or {},
                          spontanees=info.get("spontanees", 0),
                          pdf_lus=info.get("pdf_lus", 0), pdf_illisibles=info.get("pdf_illisibles", 0),
                          candidature=info.get("candidature"), remarque=remarque,
                          designations=info.get("designations", []), hors_perimetre=outside))
        records += recs
    return records, bilan


def survey_rows(records) -> list[dict]:
    """Annonces ouvertes de tout type, ou observations d'état d'une annonce existante."""
    rows = []
    for r in records:
        if r["statut"] != "ouverte" and r["contrat_confirme"] not in ("stage", "alternance") and not r["reference"]:
            continue
        o = apply_end_date(dict(r))
        o["plateforme"] = r["plateforme"] if r["plateforme"] in ADAPT else "site"
        o["page_portail"] = r.get("page_portail") or r["portail"]
        o["portail"] = portal_scope(r["portail"])
        rows.append(o)
    return rows
