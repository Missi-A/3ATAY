"""Liens de recrutement publiés par un site officiel vers un portail d'un autre domaine.

Une page Carrières d'un site officiel renvoie souvent vers un portail externe (« Découvrez nos offres » vers Workday, un
site de recrutement de groupe, une plateforme propre). Ce lien est la désignation du portail par l'employeur : le
collecteur le suit, lit le portail avec les collecteurs existants et garde la page officielle qui le désigne.

Ce module ne fait aucune requête : il repère les liens. Ni les agrégateurs d'offres (HelloWork, APEC, LinkedIn…), ni les
réseaux sociaux ne sont suivis : ils sont signalés (`kind="differe"`). Une vitrine WTTJ est seulement signalée
(`kind="wttj"`) : sa lecture reste soumise à la règle de désignation par le site officiel.
"""
from __future__ import annotations

import html as H
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .inline_offers import (
    CAREER_TEXT,
    DEFERRED_HOST,
    OFFER_LIST_TEXT,
    PDF_LINK,
    SPONTANEOUS,
    base_url,
    flatchr_scope,
    host_of,
    is_ats,
    link_problem,
    platform_name,
    same_site,
    vendor_site,
    wttj_scope,
)

# Texte d'un lien qui mène à la liste des offres (court : un paragraphe ou un pied de page n'en est pas un).
LINK_TEXT = re.compile(
    r"offres?|emplois?|jobs?|postes?|positions?|vacanc|carri[eè]res?|careers?|opportunit|recrutement|recruitment|"
    r"rejoindre|rejoignez|join us|apply|postuler|candidat", re.I)
# Pages d'information qui parlent de recrutement sans mener aux offres.
NOISE = re.compile(r"confidential|privacy|cookie|mentions?[-\s]l[ée]gales|legal|rgpd|gdpr|charte|politique|newsletter|"
                   r"conditions|accessibilit|sitemap|plan du site|blog|actualit|presse", re.I)
AGGREGATOR_PATH = re.compile(r"emploi|offre|job|career|recrut", re.I)  # lien d'agrégateur qui mène à des annonces
MAX_TEXT = 80
FOLLOWED_PER_PAGE = 3  # liens suivis par page (un par domaine) ; le collecteur applique cette limite
MAX_LINKS = 12


def _canonical(url: str) -> str:
    o = urlsplit(url)
    return f"{host_of(url)}{o.path.rstrip('/')}" + (f"?{o.query}" if o.query else "")


def designated_links(body: str, page_url: str, home: str = "") -> list[dict]:
    """[{url, texte, kind, plateforme}] : liens de la page vers un autre domaine, classés `ats` (plateforme connue),
    `externe` (autre portail), `wttj` (vitrine d'organisation) ou `differe` (agrégateur ou réseau social : non suivi).
    `home` : domaine du site officiel (par défaut celui de la page) ; ses sous-domaines ne sont jamais « externes »."""
    home = home or host_of(page_url)
    base = base_url(body, page_url)
    found: dict[str, tuple[int, dict]] = {}
    for a in BeautifulSoup(body or "", "html.parser").select("a[href]"):
        href = H.unescape(a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")) or PDF_LINK.search(href):
            continue
        url = urljoin(base, href).split("#")[0]
        if wttj_scope(url):  # alias « /companies-v1/x » : adresse canonique de la vitrine
            url = re.sub(r"/companies-v\d+/", "/companies/", url, count=1)
        o = urlsplit(url)
        host = host_of(url)
        if o.scheme not in ("http", "https") or not host or same_site(host, home) or o.username:
            continue
        if flatchr_scope(url) or platform_name(host) == "elsatis":
            continue  # déjà suivis par la lecture de la page (Flatchr, Elsatis)
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        segment = next((s for s in reversed(o.path.split("/")) if s), "")
        if (SPONTANEOUS.search(text) or SPONTANEOUS.search(segment) or NOISE.search(text) or NOISE.search(o.path)
                or link_problem(url) or vendor_site(url)):
            continue  # article de blog, site commercial d'un éditeur (« Powered by … ») : pas un portail
        scope = wttj_scope(url)
        known = is_ats(host)
        aggregator = bool(DEFERRED_HOST.search(host)) and not scope
        if (not scope and not known and not (LINK_TEXT.search(text) and 0 < len(text) <= MAX_TEXT)
                and not (aggregator and AGGREGATOR_PATH.search(o.path))):
            continue
        if scope:
            kind = "wttj"
        elif platform_name(host) == "wttj" or DEFERRED_HOST.search(host):
            kind = "differe"
        else:
            kind = "ats" if known else "externe"
        score = 2 if CAREER_TEXT.match(text) or OFFER_LIST_TEXT.match(text) or known else 1
        key = scope or host  # un lien par domaine (et par organisation WTTJ)
        item = (score, dict(url=url, texte=text, kind=kind, plateforme=host if kind == "differe" else platform_name(host) or "site"))
        depth = len([p for p in o.path.split("/") if p])
        # À score égal, l'adresse la moins profonde : « Toutes nos offres » plutôt que l'aperçu d'une annonce du portail.
        if key not in found or (score, -depth) > (found[key][0], -found[key][2]):
            found[key] = (*item, depth)
    ranked = sorted(found.values(), key=lambda item: -item[0])
    return [item for _, item, _ in ranked[:MAX_LINKS]]
