"""Découverte du site officiel par DuckDuckGo dans Chromium (Playwright), module expérimental.

Un seul moteur, trois affichages : ``visible`` (fenêtre observable, panneau de journal et preuves
surlignées dans les pages), ``minimized`` (même navigateur réel, fenêtre réduite) et ``headless``.
L'identité d'un site résulte d'un faisceau d'indices sourcés (``site_evidence``) ; la preuve légale
reste ``association`` / ``siren_contradicted``. Défi anti-robot ou refus : signalé, jamais
contourné ; un refus de DuckDuckGo arrête la série. Aucune coordonnée n'est déduite : seules celles
publiées sur les pages lues sont reprises, chacune avec son URL source.
"""

import json
import os
import random
import re
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .control import Control
from .extraction import contact_form, extract_contacts, visible_soup
from .models import Config, utc_now
from .pages import Page, host, public_url
from .privacy import redact
from .ranking import deduplicate_contacts
from .site_evidence import (
    LEGAL_WORDS,
    LEVELS,
    classify_contact,
    collect_signals,
    domain_signal,
    profile_from_registry,
    rank_key,
    third_party_text,
    tva_number,
    verdict,
)
from .verification import foreign_email, normalized, verify_contact

VERSION = "ddg-2"
SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}&kl=fr-fr"
REGISTRY_URL = "https://recherche-entreprises.api.gouv.fr/search?q={query}&per_page=3"
MODES = ("visible", "minimized", "headless")

# Annuaires, registres, réseaux sociaux, plateformes d'emploi et agrégateurs : jamais le site officiel.
EXCLUDED_HOSTS = {
    "societe.com",
    "pappers.fr",
    "pappers.com",
    "verif.com",
    "infogreffe.fr",
    "data.gouv.fr",
    "annuaire-entreprises.data.gouv.fr",
    "manageo.fr",
    "kompass.com",
    "europages.fr",
    "europages.com",
    "pagesjaunes.fr",
    "pagesjaunes.com",
    "mappy.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "indeed.com",
    "indeed.fr",
    "glassdoor.fr",
    "glassdoor.com",
    "welcometothejungle.com",
    "francetravail.fr",
    "pole-emploi.fr",
    "hellowork.com",
    "jobijoba.com",
    "apec.fr",
    "wikipedia.org",
    "wikidata.org",
    "bing.com",
    "google.com",
    "duckduckgo.com",
    "tripadvisor.fr",
    "tripadvisor.com",
    "yelp.fr",
    "yelp.com",
    "bfmtv.com",
    "lesechos.fr",
    "lefigaro.fr",
    "leparisien.fr",
    "ouest-france.fr",
    "lamarseillaise.fr",
    "laprovence.com",
    "bilansgratuits.fr",
    "score3.fr",
    "infonet.fr",
    "lexpress.fr",
    "journaldunet.com",
    "ufc-quechoisir.org",
    "malt.fr",
    "cylex.fr",
    "cylex-locale.fr",
    "118000.fr",
    "118712.fr",
    "infobel.com",
    "infobel.fr",
    "emploi-territorial.fr",
    "legifrance.gouv.fr",
    "gouv.fr",
    "ellisphere.com",
    "pinterest.fr",
    "pinterest.com",
    "reddit.com",
    "quora.com",
    "doctrine.fr",
    "fichesociete.fr",
    "leguichetdesformalites.fr",
    "entreprises.lefigaro.fr",
    "corporama.com",
    "annuaire-entreprises.fr",
    "dun-bradstreet.fr",
    "dnb.com",
    "zoominfo.com",
    "rocketreach.co",
    "lusha.com",
    "societeinfo.com",
    "economie.gouv.fr",
    "bodacc.fr",
    "net-entreprises.fr",
    "numbers.finance",
    "scanfacture.fr",
    "annuaire.118712.fr",
    "hoodspot.fr",
    "justacote.com",
    "goafrica.fr",
    "firmania.fr",
    "b-reputation.com",
    "trustpilot.com",
    "avis-verifies.com",
}
BLOCK_MARKERS = (
    "anomaly-modal",
    "bots use duckduckgo",
    "unfortunately, bots",
    "are you a human",
    "verify you are human",
    "anonymized error code",
    "captcha",
)
CHALLENGE_TITLES = ("just a moment", "access denied", "verify you are human", "attention required")
PAGE_PATTERNS = (  # (nature, motif) : ordre = priorité d'identité
    (
        "légal",
        r"mentions?[-_ ]l[ée]gales?|legal|informations?[-_ ]l[ée]gales|cgu|cgv|imprint|impressum",
    ),
    ("contact", r"contact|nous[-_ ]joindre|coordonn[ée]es|nous[-_ ]trouver"),
    (
        "recrutement",
        r"recrut|carri[eè]res?|careers?|jobs?|rejoindre|alternance|candidature|talents?|nous[-_ ]rejoindre|offres[-_ ]d[-_ ]emploi",
    ),
    (
        "équipe",
        r"[ée]quipes?|team|a[-_ ]propos|about|qui[-_ ]sommes|soci[ée]t[ée]|entreprise|groupe",
    ),
    ("agences", r"agences?|implantations?|sites|bureaux|nos[-_ ]sites|offices?|r[ée]seau"),
    ("confidentialité", r"confidentialit|privacy|donn[ée]es[-_ ]personnelles|rgpd|politique"),
)
LEGAL_GUESSES = ("/mentions-legales", "/mentions-legales/", "/legal", "/informations-legales")
ATS_HOSTS = (
    "welcometothejungle.com",
    "lever.co",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "taleo.net",
    "teamtailor.com",
    "recruitee.com",
    "flatchr.io",
    "jobaffinity.fr",
    "werecruit.io",
    "softy.pro",
    "talentsoft.com",
    "successfactors.com",
    "icims.com",
    "jobteaser.com",
    "breezy.hr",
    "workable.com",
    "join.com",
    "digitalrecruiters.com",
    "beetween.com",
)

PANEL_JS = """(text) => {
  let el = document.getElementById('__cf_panel');
  if (!el) { el = document.createElement('div'); el.id = '__cf_panel'; document.documentElement.appendChild(el); }
  el.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;width:470px;max-height:46vh;'
    + 'overflow:hidden;background:rgba(18,22,30,.94);color:#e8eef5;font:12px/1.45 Consolas,monospace;'
    + 'padding:10px 12px;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.45);pointer-events:none;white-space:pre-wrap';
  el.textContent = text;
}"""
MARK_JS = """(patterns) => {
  const done = [];
  for (const [source, color] of patterns) {
    let re; try { re = new RegExp(source, 'i'); } catch (e) { continue; }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent || parent.closest('#__cf_panel,script,style,noscript,mark')) continue;
      const m = re.exec(node.nodeValue);
      if (!m || !m[0]) continue;
      const range = document.createRange();
      range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
      const mark = document.createElement('mark');
      mark.style.cssText = 'background:' + color + ';color:#000;outline:2px solid #333;border-radius:2px';
      try { range.surroundContents(mark); } catch (e) { break; }
      if (!done.length) mark.scrollIntoView({block: 'center'});
      done.push(source);
      break;
    }
  }
  return done;
}"""
ANNOTATE_JS = """(marks) => {
  for (const a of document.querySelectorAll('a.result__a')) {
    let target = a.href;
    try { const u = new URL(a.href); target = u.searchParams.get('uddg') || a.href; } catch (e) {}
    const mark = marks.find(m => target.includes(m[0]));
    if (!mark) continue;
    a.style.outline = '2px solid ' + mark[1];
    const tag = document.createElement('b');
    tag.style.cssText = 'color:' + mark[1] + ';margin-left:6px';
    tag.textContent = '[' + mark[2] + ']';
    a.appendChild(tag);
  }
}"""


class Blocked(Exception):
    """Refus du moteur de recherche (défi, détection) : série arrêtée, jamais contourné."""


class SiteBlocked(Exception):
    """Site d'entreprise protégé par un défi/refus : candidat ignoré, la série continue."""


class Stopped(Exception):
    pass


def closed(exc):
    """Fenêtre, onglet ou navigateur fermé par l'utilisateur : arrêt propre, pas une panne."""
    if re.search(r"target (?:page|context|browser)?.*closed|has been closed", str(exc), re.I):
        raise Stopped("Fenêtre Chromium fermée.") from None


class ActivityLog:
    """Journal JSONL durable, écho console et dernières lignes pour le panneau du navigateur."""

    def __init__(self, path, echo=print):
        self.path, self.echo = Path(path), echo
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recent = deque(maxlen=9)
        self.listener = None

    def __call__(self, step, message, company="", **data):
        event = dict(
            at=utc_now(), company=company, step=step, message=redact(message), **redact(data)
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        line = f"[{time.strftime('%H:%M:%S')}] {step:<10} {message}"
        self.recent.append(line[:150])
        if self.echo:
            self.echo(f"[{time.strftime('%H:%M:%S')}] {company[:28]:<28} {step:<10} {message}")
        if self.listener:
            self.listener()


def registrable(url):
    parts = host(url).split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host(url)


def excluded(url):
    name = host(url)
    return any(name == bad or name.endswith("." + bad) for bad in EXCLUDED_HOSTS)


def result_url(href):
    """URL réelle d'un résultat (DuckDuckGo peut passer par un lien de redirection ``uddg``)."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        values = parse_qs(urlsplit(href if "//" in href else "//" + href).query).get("uddg")
        return unquote(values[0]) if values else ""
    return href


def city_label(company):
    found = re.search(r"\b\d{5}\s+(.+)$", company.city or "")
    return (found.group(1) if found else re.sub(r"\d+", "", company.city or "")).strip().title()


def queries_for(company, profile=None):
    """Nom + ville, puis sigle/enseigne + ville, puis nom + SIREN (pages légales)."""
    place = city_label(company)
    queries = [f"{company.name} {place} site officiel".strip()]
    for alias in (profile or {}).get("names", []):
        if normalized(alias) != normalized(company.name) and 3 <= len(alias) <= 40:
            queries.append(f"{alias} {place}".strip())
            break
    if company.siren:
        queries.append(f"{company.name} {company.siren} mentions légales")
    return list(dict.fromkeys(queries))


def name_tokens(company, profile=None):
    words = []
    for name in [company.name, *(profile or {}).get("names", [])]:
        words += [w for w in normalized(name).split() if len(w) >= 3]
    return list(dict.fromkeys(words))


def related(company, url, title, profile=None):
    """Écarte avant visite un résultat dont ni le domaine ni le titre ne cite le nom."""
    haystack = normalized(f"{host(url)} {title}").replace(" ", "")
    return any(w in haystack for w in name_tokens(company, profile))


def internal_links(page, kinds=None, limit=6, place=""):
    """Liens internes classés par nature (libellé court ou segment entier du chemin)."""
    found, seen = [], {page.url.rstrip("/")}
    anchors = page.soup.find_all("a", href=True)
    patterns = [p for p in PAGE_PATTERNS if not kinds or p[0] in kinds]
    if place:
        patterns.append(("local", re.escape(normalized(place)).replace(r"\ ", "[-_ ]")))
    for kind, pattern in patterns:
        for anchor in anchors:
            target = anchor["href"].strip()
            if target.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urljoin(page.base_url, target).split("#")[0]
            label = anchor.get_text(" ", strip=True)
            segments = urlsplit(absolute).path.lower().split("/")
            if (
                host(absolute) == host(page.url)
                and absolute.rstrip("/") not in seen
                and (
                    (
                        len(label) <= 40
                        and re.search(
                            pattern, normalized(label) if kind == "local" else label, re.I
                        )
                    )
                    or any(re.match(rf"(?:{pattern})(?:[-_.]|$)", seg, re.I) for seg in segments)
                )
                and not re.search(r"\.(pdf|jpe?g|png|zip|docx?|xlsx?)$", absolute, re.I)
            ):
                seen.add(absolute.rstrip("/"))
                found.append((kind, absolute))
                break
    return found[:limit]


def candidate_pages(page, limit=3):
    """Compatibilité : pages d'identité prioritaires (légal, contact, recrutement)."""
    return internal_links(page, ("légal", "contact", "recrutement", "équipe"), limit)


def without_third_party_blocks(page, company):
    """Copie de la page sans les plus petits blocs prestataire/hébergeur ne nommant pas l'entreprise."""
    soup = BeautifulSoup(page.body, "html.parser")
    blocks = soup.find_all(["p", "li", "div", "span", "td", "section", "small", "address"])
    for block in sorted(blocks, key=lambda b: len(b.get_text(" ", strip=True))):
        if block.decomposed:
            continue
        text = block.get_text(" ", strip=True)
        if len(text) <= 400 and third_party_text(text, company):
            block.decompose()
    return Page(page.url, str(soup), page.content_type, page.headers)


def other_coordinates(page, kind, tokens=()):
    """Coordonnées non nominatives publiées : formulaire, portail de recrutement, page entreprise.

    Un lien externe (portail, LinkedIn) n'est repris que s'il cite le nom de l'entreprise."""
    found = []
    if contact_form(page):
        found.append(
            dict(
                type="formulaire",
                value=page.url,
                source_url=page.url,
                scope="recrutement" if kind == "recrutement" else "general",
            )
        )
    for anchor in page.soup.select("a[href]"):
        try:
            href = urljoin(page.base_url, anchor["href"].strip())
        except ValueError:
            continue
        name = host(href)
        flat = re.sub(r"[^a-z0-9]", "", href.lower())
        if tokens and not any(t in flat for t in tokens):
            continue
        if any(name == ats or name.endswith("." + ats) for ats in ATS_HOSTS):
            found.append(
                dict(
                    type="portail_recrutement",
                    value=href.split("?")[0],
                    source_url=page.url,
                    scope="recrutement",
                )
            )
        elif (name == "linkedin.com" or name.endswith(".linkedin.com")) and (
            profile := re.search(r"https?://[^/?#]+/company/[^/?#]+", href)
        ):
            found.append(
                dict(
                    type="page_linkedin",
                    value=profile.group(0),
                    source_url=page.url,
                    scope="general",
                )
            )
    return found


def evidence_patterns(company, profile):
    """Motifs JS de surlignage des preuves dans la page affichée (mode visible)."""
    siren = company.siren or company.siret[:9]
    patterns = []
    if siren:
        patterns.append((r"[\s.]?".join([siren[:3], siren[3:6], siren[6:]]), "#7CFC8A"))
        patterns.append((re.escape(tva_number(siren)[:4]) + r"\s?" + siren[:3], "#7CFC8A"))
    for name in [company.name, *profile.get("names", [])][:3]:
        if len(name) >= 3:
            patterns.append((re.escape(name).replace(r"\ ", r"\s+"), "#FFE14D"))
    for person in profile.get("people", [])[:4]:
        patterns.append((re.escape(person.split(" ", 1)[1]), "#9FD3FF"))
    return patterns


def write_json(path, payload):
    """Écriture atomique : un arrêt brutal ne laisse jamais un fichier de reprise tronqué."""
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, path)


class SiteSearch:
    def __init__(
        self,
        log,
        mode="visible",
        pause=None,
        control=None,
        stop_file=None,
        company_seconds=300,
        candidates=6,
        contact_pages=16,
        web_factory=None,
    ):
        if mode not in MODES:
            raise ValueError(f"mode inconnu : {mode}")
        self.log, self.mode = log, mode
        self.pause = (2.0 if mode == "visible" else 0.0) if pause is None else pause
        self.control = control or Control()
        self.stop_file = Path(stop_file) if stop_file else None
        self.company_seconds, self.max_candidates = company_seconds, candidates
        self.contact_pages = contact_pages
        self.web_factory = web_factory
        self.manager = self.browser = self.context = self.page = None
        self.last_search = 0.0
        self.current = ""
        self.web = None

    # --- cycle de vie ------------------------------------------------------------------------
    def start(self):
        from playwright.sync_api import sync_playwright

        self.manager = sync_playwright().start()
        self.browser = self.manager.chromium.launch(
            headless=self.mode == "headless", slow_mo=200 if self.mode == "visible" else 0
        )
        self.context = self.browser.new_context(
            locale="fr-FR",
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1280, "height": 860},
        )
        if self.mode != "visible":  # rien à regarder : ni image, ni média, ni police
            self.context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_()
                ),
            )
        self.page = self.context.new_page()
        self.page.set_default_timeout(20000)
        if self.mode == "minimized":
            # Fenêtre réelle réduite par l'API de Chromium (aucune modification d'identité).
            cdp = self.context.new_cdp_session(self.page)
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window, "bounds": {"windowState": "minimized"}},
            )
        if self.web_factory:
            self.web = self.web_factory()
        self.log.listener = self.refresh_panel

    def close(self):
        self.log.listener = None
        for item in (self.context, self.browser):
            try:
                item and item.close()
            except Exception:
                pass
        try:
            self.manager and self.manager.stop()
        except Exception:
            pass
        if self.web:
            self.web.close()
        self.context = self.browser = self.manager = self.page = self.web = None

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.close()  # navigateur à moitié lancé : rien ne reste ouvert
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    # --- contrôle et visualisation -----------------------------------------------------------
    def check(self, deadline=None):
        if self.control.cancelled.is_set() or (self.stop_file and self.stop_file.exists()):
            raise Stopped("Arrêt demandé.")
        if deadline and time.monotonic() > deadline:
            raise TimeoutError("budget de temps de l'entreprise atteint")

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.2, max(0, end - time.monotonic())))

    def refresh_panel(self):
        if self.mode != "visible" or not self.page:
            return
        try:
            self.page.evaluate(
                PANEL_JS, f"Contact Finder — {self.current}\n" + "\n".join(self.log.recent)
            )
        except Exception:
            pass  # page en cours de chargement : le panneau revient au prochain événement

    def highlight(self, patterns):
        if self.mode != "visible" or not patterns:
            return []
        try:
            return self.page.evaluate(MARK_JS, [list(p) for p in patterns])
        except Exception:
            return []

    @contextmanager
    def timed(self, name, timings):
        started = time.monotonic()
        try:
            yield
        finally:
            timings[name] = round(timings.get(name, 0) + time.monotonic() - started, 2)

    # --- navigation --------------------------------------------------------------------------
    def goto(self, url, deadline=None, patterns=()):
        self.check(deadline)
        public_url(url)  # refuse réseau privé / identifiants / ports exotiques
        if self.web is not None:
            self.web._allowed(url)  # robots.txt du site visité, comme pour une lecture HTTP
        remaining = (deadline - time.monotonic()) if deadline else 20
        try:
            response = self.page.goto(
                url, wait_until="domcontentloaded", timeout=max(1000, min(20000, remaining * 1000))
            )
        except Exception as exc:
            closed(exc)
            raise
        try:
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception as exc:
            closed(exc)  # sites bavards : le DOM chargé suffit
        self.check(deadline)
        html = self.page.content()
        title = (self.page.title() or "").lower()
        # Un reCAPTCHA de formulaire n'est pas un blocage : seuls statut et titre de défi comptent.
        if (response and response.status in (403, 429)) or any(
            m in title for m in CHALLENGE_TITLES
        ):
            status = response.status if response else "?"
            raise SiteBlocked(f"protégé par un défi/refus (HTTP {status}) ; non contourné")
        if response and response.status >= 400:
            raise LookupError(f"HTTP {response.status}")
        self.refresh_panel()
        self.highlight(patterns)
        if self.pause:
            self.wait(self.pause)
        return Page(self.page.url, html)

    def search(self, query, deadline):
        """Résultats DuckDuckGo (ordre d'affichage) : [(url, titre)]."""
        gap = 5.0 + random.uniform(0, 2.0)  # cadence modérée et constante
        self.wait(max(0.0, self.last_search + gap - time.monotonic()))
        self.check(deadline)
        try:
            response = self.page.goto(
                SEARCH_URL.format(query=quote_plus(query)),
                wait_until="domcontentloaded",
                timeout=25000,
            )
        except Exception as exc:
            closed(exc)
            raise
        self.last_search = time.monotonic()
        try:
            self.page.wait_for_selector(
                "a.result__a, .anomaly-modal__modal, .no-results", timeout=12000
            )
        except Exception as exc:
            closed(exc)
        html = self.page.content().lower()
        if (
            (response and (response.status >= 400 or response.status == 202))
            or "static-pages" in self.page.url
            or any(m in html for m in BLOCK_MARKERS)
        ):
            raise Blocked(
                f"DuckDuckGo a refusé la requête (mode {self.mode}) ; aucun contournement."
            )
        results = []
        for link in self.page.query_selector_all("a.result__a"):
            href = result_url(link.get_attribute("href") or "")
            if href.startswith("http"):
                results.append((href, (link.inner_text() or "").strip()[:120]))
        if not results and not self.page.query_selector(".no-results"):
            # HTTP 200 vide, sans message « aucun résultat » : ni refus prouvé ni recherche réussie.
            self.log("anomalie", f"réponse DuckDuckGo vide sans message pour « {query} »")
        self.refresh_panel()
        return results

    def annotate(self, marks):
        if self.mode == "visible" and marks:
            try:
                self.page.evaluate(ANNOTATE_JS, marks)
                self.wait(self.pause)
            except Stopped:
                raise
            except Exception:
                pass

    # --- registre et références --------------------------------------------------------------
    def registry(self, company, label):
        """Profil d'identité (registre public) et sites déclarés par Wikidata pour ce SIREN."""
        profile = dict(names=[], people=[], addresses=[], sigle="", commune="", postal="")
        references = []
        if not self.web or not (company.siren or company.siret):
            return profile, references
        from .sources import wikidata_item_sites

        siren = company.siren or company.siret[:9]
        try:
            # Le SIRET renvoie aussi l'établissement visé (adresse locale), en plus du siège.
            query = company.siret or siren
            page = self.web.fetch(REGISTRY_URL.format(query=query), "registre public")
            for item in json.loads(page.body).get("results", []):
                if str(item.get("siren")) == siren:
                    profile = profile_from_registry(item, company)
            self.log(
                "registre",
                f"raison sociale/enseignes {profile['names'][:3]}, {len(profile['people'])} dirigeant(s), "
                f"{len(profile['addresses'])} adresse(s), TVA {tva_number(siren)}",
                label,
                source=page.url,
            )
        except Exception as exc:
            self.log("registre", f"indisponible : {str(exc)[:120]}", label)
        try:
            checkpoint = {}
            declared = encyclopedia_sites(company, self.web, checkpoint)
            references += [dict(kind="encyclopedia", url=u) for u in declared]
            if checkpoint.get("item"):
                references += [
                    dict(kind="wikidata_siren", url=u)
                    for u in wikidata_item_sites(checkpoint["item"], company, self.web)
                ]
            if references:
                self.log(
                    "référence", ", ".join(f"{r['kind']}: {r['url']}" for r in references), label
                )
        except Exception as exc:
            self.log("référence", f"encyclopédie indisponible : {str(exc)[:100]}", label)
        return profile, references

    # --- traitement d'une entreprise ----------------------------------------------------------
    def discover(self, company):
        """Résultat structuré ; ne lève que ``Stopped`` / ``Blocked`` (arrêt de la série)."""
        label = company.name
        self.current = f"{company.name} ({company.city}) SIREN {company.siren or '—'}"
        started = time.monotonic()
        deadline = started + self.company_seconds
        identity_deadline = started + self.company_seconds * 0.65  # le reste : contacts
        timings = {}
        result = dict(
            id=company.id,
            name=company.name,
            city=company.city,
            siren=company.siren,
            version=VERSION,
            queries=[],
            candidates=[],
            rejected=[],
            official_site="",
            level="non_fiable",
            basis="",
            signals=[],
            contacts=[],
            other_coordinates=[],
            pages_read=[],
            errors=[],
            status="no_site",
            timings=timings,
        )
        self.log("entreprise", f"Analyse : {self.current}", label)
        try:
            with self.timed("registre", timings):
                profile, references = self.registry(company, label)
            result["profile"] = profile
            result["references"] = references
            ranked = list(
                {
                    registrable(r["url"]): (
                        f"https://{urlsplit(r['url']).netloc}/",
                        "référence " + r["kind"],
                        "référence",
                    )
                    for r in reversed(references)
                    if not excluded(r["url"])
                }.values()
            )
            with self.timed("recherche", timings):
                ranked = self.collect_candidates(
                    company, profile, ranked, result, identity_deadline, label
                )
            # Références d'abord, puis domaine cohérent, puis ordre DuckDuckGo (tri stable).
            ranked.sort(
                key=lambda c: (
                    c[2] != "référence",
                    domain_signal(company, profile, c[0]) not in ("domain",),
                )
            )
            self.log(
                "candidats",
                " > ".join(host(u) for u, _, _ in ranked[: self.max_candidates]) or "aucun",
                label,
            )
            best = None
            try:
                for root, title, origin in ranked[: self.max_candidates]:
                    entry = self.examine(
                        company,
                        profile,
                        references,
                        root,
                        title,
                        origin,
                        result,
                        identity_deadline,
                        timings,
                        label,
                    )
                    if entry and (
                        best is None
                        or rank_key(entry["level"], entry["signals"])
                        < rank_key(best["level"], best["signals"])
                    ):
                        best = entry
                    if best and best["level"] == "confirme":
                        break
            except TimeoutError as exc:
                # Le meilleur candidat déjà évalué reste valable ; les contacts ont leur budget.
                result["errors"].append(f"identification : {exc}")
                self.log("budget", f"identification interrompue : {exc}", label)
            if best and best["level"] != "non_fiable":
                result.update(level=best["level"], basis=best["basis"], signals=best["signals"])
                result["official_site" if best["level"] == "confirme" else "best_site"] = best[
                    "url"
                ]
                self.log(
                    "retenu" if best["level"] == "confirme" else best["level"],
                    f"{best['url']} — {best['basis']}",
                    label,
                )
                with self.timed("contacts", timings):
                    self.contacts(company, profile, best, result, deadline, label)
            else:
                self.log("aucun", "aucun site fiable parmi les candidats examinés", label)
        except TimeoutError as exc:
            result["errors"].append(str(exc))
            self.log("budget", str(exc), label)
        except (Blocked, Stopped):
            self.finish(company, result, started)
            raise
        except Exception as exc:  # défaut interne : tracé, la série continue
            closed(exc)
            result["errors"].append(f"erreur interne : {type(exc).__name__}: {str(exc)[:160]}")
            self.log("erreur", f"erreur interne : {type(exc).__name__}: {str(exc)[:160]}", label)
        return self.finish(company, result, started)

    def collect_candidates(self, company, profile, ranked, result, deadline, label):
        seen = {registrable(u) for u, _, _ in ranked}
        for query in queries_for(company, profile):
            self.log("requête", query, label, query=query)
            result["queries"].append(query)
            found = self.search(query, deadline)
            self.log(
                "résultats",
                f"{len(found)} résultat(s) DuckDuckGo",
                label,
                urls=[u for u, _ in found[:10]],
            )
            marks = []
            for url, title in found:
                root = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
                if excluded(url):
                    marks.append([host(url), "#999", "annuaire exclu"])
                    if registrable(url) not in seen:
                        result["rejected"].append(
                            dict(url=url, reason="annuaire / réseau / plateforme exclu")
                        )
                    continue
                if registrable(url) in seen:
                    continue
                if not related(company, url, title, profile):
                    marks.append([host(url), "#d33", "sans rapport"])
                    result["rejected"].append(
                        dict(url=url, reason="ni domaine ni titre ne citent le nom")
                    )
                    self.log("rejet", f"{host(url)} : ni domaine ni titre ne citent le nom", label)
                    continue
                own_domain = any(
                    w in normalized(host(url)).replace(" ", "")
                    for w in name_tokens(company, profile)
                )
                depth = len([s for s in urlsplit(url).path.split("/") if s])
                if not own_domain and depth > 1:
                    # Article ou fiche d'un tiers citant l'entreprise : pas un site officiel.
                    marks.append([host(url), "#d33", "article tiers"])
                    result["rejected"].append(dict(url=url, reason="page profonde d'un tiers"))
                    self.log("rejet", f"{host(url)} : page profonde d'un tiers ({url})", label)
                    continue
                seen.add(registrable(url))
                marks.append([host(url), "#1a8f3a", "candidat"])
                ranked.append((root, title, "duckduckgo"))
            self.annotate(marks)
        return ranked

    def identity_pages(self, root, home, deadline, patterns, label):
        """Accueil + mentions légales (lien ou chemin usuel) + contact, recrutement, équipe."""
        pages = [("accueil", home, visible_soup(home).get_text(" ", strip=True))]
        links = internal_links(
            home, ("légal", "contact", "recrutement", "équipe", "confidentialité"), 5
        )
        legal_found = False
        for kind, url in links:
            self.log("visite", f"Page {kind} : {url}", label)
            try:
                page = self.goto(url, deadline, patterns)
                text = visible_soup(page).get_text(" ", strip=True)
                if kind == "légal" and not self.legal_page(root, page, text):
                    kind = "autre"  # lien « légal » sans contenu légal effectif
                    self.log("contrôle", f"{page.url} : pas de contenu légal effectif", label)
                legal_found |= kind == "légal"
                pages.append((kind, page, text))
            except (Stopped, Blocked, TimeoutError):
                raise
            except Exception as exc:
                self.log("erreur", f"{url} : {str(exc)[:100]}", label)
        for path in () if legal_found else LEGAL_GUESSES:
            try:
                self.log("visite", f"Mentions légales (chemin usuel) : {path}", label)
                page = self.goto(urljoin(root, path), deadline, patterns)
            except (Stopped, Blocked, TimeoutError):
                raise
            except Exception:
                continue
            text = visible_soup(page).get_text(" ", strip=True)
            if self.legal_page(root, page, text):
                pages.append(("légal", page, text))
                break
            self.log("contrôle", f"{page.url} : page non légale (accueil ou 404 déguisée)", label)
        return pages

    @staticmethod
    def legal_page(root, page, text):
        """Page légale effective : même site, adresse finale « légale », vocabulaire légal."""
        path = urlsplit(page.url).path.lower()
        return bool(
            registrable(page.url) == registrable(root)
            and re.search(r"l[ée]gal|mention|imprint|impressum|cgu|cgv|informations", path)
            and LEGAL_WORDS.search(text)
        )

    def examine(
        self, company, profile, references, root, title, origin, result, deadline, timings, label
    ):
        """Lit le candidat, rassemble les indices et rend son niveau avec leurs sources."""
        entry = dict(
            url=root, title=title, origin=origin, level="non_fiable", basis="", signals=[], pages=[]
        )
        result["candidates"].append(entry)
        patterns = evidence_patterns(company, profile)
        try:
            with self.timed("vérification", timings):
                self.log("visite", f"Accueil {root} ({origin})", label)
                home = self.goto(root, deadline, patterns)
                if registrable(home.url) != registrable(root):
                    self.log("redirection", f"{host(root)} → {host(home.url)}", label)
                    if excluded(home.url):
                        entry["basis"] = "redirige vers un annuaire/plateforme"
                        return entry
                    root = f"{urlsplit(home.url).scheme}://{urlsplit(home.url).netloc}/"
                    entry["url"] = root
                pages = self.identity_pages(root, home, deadline, patterns, label)
        except (Stopped, Blocked):
            raise
        except SiteBlocked as exc:
            entry["basis"] = str(exc)
            result["errors"].append(f"{host(root)} : {exc}")
            self.log("blocage", f"{host(root)} : {exc}", label)
            return entry
        except TimeoutError:
            entry["basis"] = "budget de temps atteint avant vérification"
            raise
        except Exception as exc:
            entry["basis"] = f"navigation impossible : {str(exc)[:120]}"
            result["errors"].append(f"{host(root)} : {str(exc)[:160]}")
            self.log("erreur", entry["basis"], label)
            return entry
        entry["pages"] = [(kind, page.url) for kind, page, _ in pages]
        result["pages_read"] += [page.url for _, page, _ in pages]
        full_signals = collect_signals(company, profile, pages, root, references, full_excerpt=True)
        signals = [dict(s, excerpt=s["excerpt"][:180]) for s in full_signals]
        level, basis = verdict(signals)
        entry.update(level=level, basis=basis, signals=signals)
        entry["_full_signals"] = full_signals
        entry["identity_pages"] = [
            dict(url=page.url, title=page.soup.title.get_text(" ", strip=True) if page.soup.title else "",
                 text=visible_soup(page).get_text(" ", strip=True),
                 kind="identite" if kind in ("accueil", "légal", "équipe") else
                 "contact" if kind in ("contact", "recrutement") else "autre",
                 fetched_at=utc_now())
            for kind, page, _ in pages
        ]
        entry["_pages"] = pages  # réutilisées par la recherche de contacts, jamais sauvegardées
        for signal in signals:
            self.log("indice", f"{host(root)} · {signal['label']} ({signal['url']})", label)
        self.log("verdict", f"{host(root)} → {level.upper()} : {basis}", label)
        if level == "non_fiable":
            result["rejected"].append(dict(url=root, reason=basis))
        return entry

    def contacts(self, company, profile, best, result, deadline, label):
        """Exploration en largeur des pages utiles du site retenu ; coordonnées publiées seulement."""
        site, level = best["url"], best["level"]
        pages = list(best.pop("_pages", []))
        for candidate in result["candidates"]:
            candidate.pop("_pages", None)
        queue = deque()
        seen = {p.url.rstrip("/") for _, p, _ in pages}
        for _, page, _ in pages:
            for kind, url in internal_links(page, None, 8, city_label(company)):
                if url.rstrip("/") not in seen:
                    seen.add(url.rstrip("/"))
                    queue.append((kind, url, 1))
        order = ("contact", "recrutement", "local", "équipe", "agences", "légal", "confidentialité")
        queue = deque(sorted(queue, key=lambda q: order.index(q[0]) if q[0] in order else 9))
        read = len(pages)
        try:
            while queue and read < self.contact_pages:
                kind, url, depth = queue.popleft()
                self.log("contacts", f"Page {kind} : {url}", label)
                try:
                    page = self.goto(url, deadline, ())
                except (Stopped, Blocked, TimeoutError):
                    raise
                except Exception as exc:
                    self.log("erreur", f"{url} : {str(exc)[:100]}", label)
                    continue
                read += 1
                pages.append((kind, page, ""))
                result["pages_read"].append(page.url)
                if depth < 2:
                    for sub_kind, sub in internal_links(
                        page, ("contact", "recrutement", "local", "agences"), 4, city_label(company)
                    ):
                        if sub.rstrip("/") not in seen:
                            seen.add(sub.rstrip("/"))
                            queue.append((sub_kind, sub, depth + 1))
        except TimeoutError as exc:
            result["errors"].append(f"contacts : {exc}")
            self.log("budget", f"exploration des contacts interrompue : {exc}", label)
        found, others = [], []
        tokens = [re.sub(r"[^a-z0-9]", "", t) for t in name_tokens(company, profile)]
        for kind, page, _ in pages:
            try:
                for contact in extract_contacts(without_third_party_blocks(page, company), company):
                    contact.setdefault("source_url", page.url)
                    contact = verify_contact(contact, company, site, level == "confirme", level)
                    contact["type"], contact["scope"] = classify_contact(contact, kind)
                    contact["site_level"] = level
                    found.append(contact)
                others += other_coordinates(page, kind, tokens)
            except Exception as exc:  # une page malformée n'efface pas les autres
                result["errors"].append(f"extraction {page.url} : {str(exc)[:120]}")
        # Adresse d'un autre domaine (hébergeur, agence web, prestataire) : écartée et tracée.
        foreign = [c for c in found if foreign_email(c, company, site)]
        result["foreign_contacts"] = [
            dict(email=c["email"], source_url=c["source_url"]) for c in foreign
        ]
        for contact in foreign:
            self.log(
                "écarté", f"{contact['email']} : domaine tiers ← {contact['source_url']}", label
            )
        result["contacts"] = deduplicate_contacts([c for c in found if c not in foreign])
        result["other_coordinates"] = list({(o["type"], o["value"]): o for o in others}.values())
        for contact in result["contacts"]:
            value = contact.get("email") or contact.get("phone") or contact.get("name")
            self.log(
                "contact",
                f"{contact['type']} {value} [{contact['scope']}, {contact['confidence']}] ← {contact['source_url']}",
                label,
            )
            if self.mode == "visible" and contact.get("email"):
                self.highlight([(re.escape(contact["email"]), "#FFB3D9")])
        for other in result["other_coordinates"]:
            self.log(
                "contact",
                f"{other['type']} {other['value']} [{other['scope']}] ← {other['source_url']}",
                label,
            )
        if not result["contacts"] and not result["other_coordinates"]:
            self.log("contact", f"aucune coordonnée publiée sur {read} page(s) lue(s)", label)

    def finish(self, company, result, started):
        for candidate in result["candidates"]:
            candidate.pop("_pages", None)
            candidate["signals"] = candidate.pop("_full_signals", candidate.get("signals", []))
        selected = result.get("official_site") or result.get("best_site")
        best = next((c for c in result["candidates"] if c["url"] == selected), None)
        if best:
            result["signals"] = best["signals"]
        result["identity_pages"] = [p for c in result["candidates"]
                                    for p in c.get("identity_pages", [])]
        result["contradictions"] = [
            dict(code=s["code"], detail=s["label"], url=s["url"])
            for c in result["candidates"] for s in c["signals"]
            if s["code"] in ("conflict", "multiple_ids")
        ]
        result["status"] = (
            "official"
            if result["official_site"]
            else result["level"]
            if result["level"] in ("probable", "groupe")
            else "no_site"
        )
        result["seconds"] = round(time.monotonic() - started, 2)
        self.log(
            "fin",
            f"{result['status']} en {result['seconds']} s — étapes {result['timings']}",
            company.name,
            official=result["official_site"],
        )
        return result


def to_finder_result(result):
    """Vue compatible avec les champs du résultat Contact Finder (intégration future)."""
    site = result.get("official_site") or result.get("best_site", "")
    return dict(
        official_site=site,
        site_verified=result["level"] == "confirme",
        site_confidence={"confirme": "confirme", "probable": "probable"}.get(
            result["level"], "incertain"
        ),
        site_candidates=[
            dict(
                url=c["url"],
                confidence={"confirme": "confirme", "probable": "probable"}.get(
                    c["level"], "incertain"
                ),
                hints=[s["code"] for s in c["signals"]],
                origin=c["origin"],
            )
            for c in result["candidates"]
        ][:6],
        contacts=result["contacts"],
        rejected_candidates=[
            dict(kind="site", reason=r["reason"], source_url=r["url"]) for r in result["rejected"]
        ],
        source_outcomes=[dict(source="duckduckgo", queries=result["queries"])],
    )


def run_batch(
    companies,
    out_dir,
    mode="visible",
    pause=None,
    limit=None,
    company_seconds=300,
    echo=print,
    control=None,
    web_factory=None,
):
    """File d'attente avec sauvegarde atomique et reprise : seules les entreprises finies sont gardées."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "results.json"
    log = ActivityLog(out / "journal.jsonl", echo)
    try:
        results = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    except (OSError, ValueError) as exc:
        # Fichier de reprise illisible : conservé tel quel pour diagnostic, jamais écrasé.
        raise RuntimeError(f"Reprise impossible, {state_path} illisible : {exc}") from None
    status, stop = "complete", None
    if web_factory is None:
        from .web import Web

        web_factory = lambda: Web(Config(), Control(), [])  # noqa: E731
    with SiteSearch(
        log, mode, pause, control, out / "STOP", company_seconds, web_factory=web_factory
    ) as engine:
        log(
            "lot",
            f"mode {mode} — {len(companies[:limit])} entreprise(s) ; arrêt : Ctrl+C, fermeture "
            f"de Chromium ou fichier {out / 'STOP'}",
        )
        try:
            for company in companies[:limit]:
                previous = results.get(company.id)
                if previous and not any(
                    e.startswith("erreur interne") for e in previous.get("errors", [])
                ):
                    log("reprise", "déjà traitée, ignorée", company.name)
                    continue  # un échec technique est retenté, une recherche terminée non
                results[company.id] = engine.discover(company)
                write_json(state_path, results)
        except Blocked as exc:
            status, stop = "blocked", str(exc)
            log("BLOCAGE", f"{exc} — série interrompue, reprise possible plus tard.")
        except (Stopped, KeyboardInterrupt) as exc:
            status, stop = "stopped", str(exc) or "interruption"
            log("arrêt", f"Arrêt propre : {stop}")
    return results, status, stop


__all__ = ["LEVELS", "MODES", "SiteSearch", "run_batch", "to_finder_result"]


_ENCYCLOPEDIA_SEEN = {}


def encyclopedia_sites(company, web, checkpoint=None):
    """V5 : recherche encyclopédique faite une fois par entreprise et par processus. La recherche de site
    puis le connecteur Wikidata essaient les mêmes titres ; seul un résultat obtenu sans exception est gardé."""
    from .sources import encyclopedia_sites as search

    checkpoint = checkpoint if checkpoint is not None else {}
    key = (company.name, company.siren)
    if not checkpoint and key in _ENCYCLOPEDIA_SEEN:
        saved, links = _ENCYCLOPEDIA_SEEN[key]
        checkpoint.update(saved)
        return list(links)
    links = search(company, web, checkpoint)
    _ENCYCLOPEDIA_SEEN[key] = (dict(checkpoint), list(links))
    return links
