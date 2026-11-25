"""Annonces intégrées dans une page HTML (liste d'entrées, avec ou sans fiche PDF) et pages Carrières d'un site officiel.

Fonctions pures : le réseau et la lecture des PDF sont injectés par `company_portal_collector`.
- `career_links` : liens « Carrières », « Recrutement », « Offres d'emploi » d'une page du site officiel (périmètre : le
  site lui-même et les plateformes de recrutement qu'il désigne).
- `page_entries` : une entrée par annonce de la page. Une annonce est un titre répété avec, dans son bloc, un type de
  contrat annoncé ou un lien PDF ; une candidature spontanée est signalée, jamais comptée comme annonce.
- `pdf_facts` : contrat, lieux et date lus dans le texte d'une fiche PDF ; `place_alerts` signale les contradictions.
shortcut: blocs repérés par la répétition des titres (même balise et mêmes classes), une page à titres tous différents
n'est pas découpée ; à reprendre avec un site réel concerné.
"""

from __future__ import annotations

import datetime as dt
import html as H
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..contact_finder_engine.engine.models import web_url
from .offer_text import html_to_text

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
CONTRACT_WORDS = (r"CDI|CDD|stage|stagiaire|alternance|apprentissage|contrat (?:d['’]apprentissage|de professionnalisation|pro)"
                  r"|int[ée]rim|freelance|ind[ée]pendant|saisonnier|VIE")
CARD_TITLE_CLASS = re.compile(r"font-bold|title|titre|intitule", re.I)
LABEL_CLASS = re.compile(r"job[-_ ]?type|contract|contrat|employment|type[-_ ]?emploi", re.I)
PLACE_CLASS = re.compile(r"address|location|lieu|city|ville|localisation", re.I)
EMPLOYER_CLASS = re.compile(r"company|employer|entreprise|societe", re.I)
PLACE_LINE = re.compile(r"^(?:localisation|lieu(?: de travail)?|ville|site)\s*(?:/|:|-|–)?\s*(.{2,80})$", re.I)
META_LINE = re.compile(rf"^(?:(\d{{1,2}}/\d{{1,2}}/\d{{4}})\s*[–—\-·|•]\s*)?({CONTRACT_WORDS})\s*(?:[–—\-·|•]\s*(.{{2,60}}))?$", re.I)
CONTRACT_LINE = re.compile(rf"^(?:type de contrat|contrat|nature du contrat)\s*:\s*({CONTRACT_WORDS})\b", re.I)
SPONTANEOUS = re.compile(
    r"candidatures?[\s_-]+spontan|postuler[\s_-]+spontan|(?:spontaneous|unsolicited|open)[\s_-]+application|"
    r"initiativbewerbung|candidatura[\s_-]+espont", re.I)
# Rubrique d'une page Carrières, jamais l'intitulé d'une annonce.
GENERIC_TITLE = re.compile(
    r"^(?:nos |les |toutes nos |voir nos )?(?:offres?(?: d['’ ]?emplois?| de stages?| d['’ ]?alternances?)?|"
    r"job offers?|jobs?|open positions?|postes? (?:ouverts?|à pourvoir|disponibles?)|opportunit[ée]s|emplois?|"
    r"nous rejoindre|rejoignez[- ]nous|rejoindre l['’][ée]quipe|carri[eè]res?|recrutements?|join us|careers?)\s*[:!.]?$",
    re.I)
PLACEHOLDER = re.compile(r"\blorem ipsum\b", re.I)  # gabarit de site non rempli
FILLED = re.compile(  # annonce close publiée comme telle
    r"\b(?:offre|poste|annonce|mission)s?\s+(?:a\s+été\s+|ont\s+été\s+|est\s+(?:déjà\s+)?|sont\s+(?:déjà\s+)?)?"
    r"(?:pourvue?s?|clôturée?s?|expirée?s?)\b|\bposition (?:has been )?filled\b|\bno longer (?:available|accepting)\b|"
    r"\bthis (?:job|position) (?:is|has) (?:been )?(?:closed|filled|expired)\b", re.I)
JOB_MARKERS = re.compile(r"\b(?:CDI|CDD|stage|stagiaire|alternance|apprentissage|missions?|profil|candidat\w*|postuler)\b", re.I)
PDF_LINK = re.compile(r"\.pdf(?:$|[?#])", re.I)
MAX_BLOCK = 15_000  # un bloc d'annonce plus long est un conteneur de page, pas une annonce
NOISE_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "svg", "aside")

CAREER_TEXT = re.compile(
    r"^(?:nos |les )?(?:carri[eè]res?|recrutements?|offres?(?: d['’ ]?(?:emploi|stage)s?)?|emplois?|jobs?|careers?|"
    r"nous rejoindre|rejoignez[- ]nous|travailler chez nous|rejoindre l['’][ée]quipe|join us|work with us|"
    r"rejoi(?:ndre|gnez) (?:nos|notre) [ée]quipes?)$", re.I)
CAREER_SEGMENT = re.compile(
    r"^(carrieres?|careers?|recrutements?|recrute|nous-rejoindre|rejoignez-nous|rejoins-nous|join-us|"
    r"rejoi(?:ndre|gnez)-(?:nos|notre)-equipes?|offres?(-d-?emplois?)?|"
    r"offres-demploi|nos-offres(-demploi)?|emplois?|jobs?|jobs-careers|travailler-chez-nous|vos-opportunites)(\.html?)?$", re.I)
OFFER_LIST_TEXT = re.compile(
    r"^(?:voir|d[eé]couvr(?:ez|ir)|consult(?:ez|er)|retrouv(?:ez|er)) (?:toutes? )?(?:nos |les )?offres\b", re.I)
PLATFORMS = {  # plateformes de recrutement : domaines (l'hôte doit être le domaine ou l'un de ses sous-domaines)
    "teamtailor": ("teamtailor.com", "jobs.ekium.eu", "carriere.altradendel.com"),
    "recruitee": ("recruitee.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workday": ("myworkdayjobs.com",),
    "icims": ("icims.com",),
    "wttj": ("welcometothejungle.com", "welcomekit.co"),
    "flatchr": ("flatchr.io",),
    "elsatis": ("elsatis.fr",),
    "oracle_hcm": ("fa.ocs.oraclecloud.com",),
    "bpce": ("recrutement.bpce.fr",),
    "autre": ("lever.co", "greenhouse.io", "flatchr.com", "workable.com",
              "ashbyhq.com", "jobaffinity.com"),
}
DEFERRED_HOST = re.compile(r"welcometothejungle|jobteaser|francetravail|indeed|linkedin|facebook|instagram|twitter|x\.com|"
                           r"youtube|apec\.fr|hellowork|hw-recruteur|monster|glassdoor", re.I)
REGIONS = {"occitanie", "bretagne", "normandie", "île-de-france", "ile-de-france", "grand est", "hauts-de-france",
           "auvergne-rhône-alpes", "nouvelle-aquitaine", "pays de la loire", "centre-val de loire", "paca",
           "bourgogne-franche-comté", "corse", "provence-alpes-côte d'azur", "provence-alpes-côte d’azur"}
PLACE_PHRASE = re.compile(
    r"(?i:en mission|mission|positionn\w+|(?:est|sera|serez|êtes|sont|être)\s+bas[ée]e?s?|situ[ée]e?s?|localis[ée]e?s?)\s+(?:à|a|au|en|dans)\s+"
    r"(?:l[ea]s?\s+|l['’])?([A-ZÉÈÂÎ][\w'’\-]+(?:\s+[A-ZÉÈÂÎ][\w'’\-]+)?"
    r"(?:\s*(?:,|ou|et)\s+[A-ZÉÈÂÎ][\w'’\-]+(?:\s+[A-ZÉÈÂÎ][\w'’\-]+)?)*)")


@dataclass
class Entry:
    title: str
    label: str = ""  # contrat annoncé par la page (« CDI », « Stage »…)
    places: list[str] = field(default_factory=list)  # tous les lieux affichés
    employer: str = ""
    pdf_url: str = ""
    date: str = ""  # ISO
    text: str = ""  # texte de l'entrée dans la page (hors titre)
    spontaneous: bool = False
    href: str = ""  # page de détail liée à l'entrée (hors PDF) : cette annonce y est lue à part
    closed: bool = False  # la page la dit pourvue ou close


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def platform_name(host: str) -> str:
    """Nom de l'adaptateur de la plateforme de recrutement qui héberge `host` (domaine ou sous-domaine), sinon « »."""
    host = host.lower().removeprefix("www.")
    return next((name for name, domains in PLATFORMS.items() if any(_under(host, d) for d in domains)), "")


def is_ats(host: str) -> bool:
    return bool(platform_name(host))


def vendor_site(url: str) -> bool:
    """Site commercial de l'éditeur (« https://www.teamtailor.com/fr/ ») : domaine nu d'une plateforme, sans organisation
    dans le chemin ; jamais le portail d'une entreprise. Les vitrines WTTJ et Flatchr ont leur propre règle."""
    roots = {d for name, domains in PLATFORMS.items() if name not in ("wttj", "flatchr") for d in domains if d.count(".") == 1}
    return host_of(url) in roots and len([p for p in urlsplit(url).path.split("/") if p]) <= 1


GREENHOUSE_HOSTS = (("boards.greenhouse.io", "job-boards.greenhouse.io"),
                    ("boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"))  # deux adresses d'une même région
GREENHOUSE_POSTING = re.compile(r"/([A-Za-z0-9][A-Za-z0-9._-]*)/jobs/([0-9]+)/?")


def greenhouse_posting(url: str) -> tuple[str, str] | None:
    """(organisation en minuscules, numéro d'annonce) d'une annonce Greenhouse, quel que soit l'hôte de la région."""
    o = urlsplit(url or "")
    host = (o.hostname or "").lower()
    match = GREENHOUSE_POSTING.fullmatch(o.path)
    if o.scheme != "https" or not match or not any(host in pair for pair in GREENHOUSE_HOSTS):
        return None
    return match[1].lower(), match[2]


def greenhouse_aliases(url: str) -> list[str]:
    """Adresses de la même annonce sur les deux hôtes Greenhouse de sa région (organisation et numéro identiques)."""
    key = greenhouse_posting(url)
    if key is None:
        return []
    host = urlsplit(url).hostname.lower()
    pair = next(pair for pair in GREENHOUSE_HOSTS if host in pair)
    path = urlsplit(url).path.rstrip("/")
    return [f"https://{h}{path}{tail}" for h in pair for tail in ("", "/")]


@lru_cache(maxsize=32768)  # fonction pure de l'URL, appelée pour chaque offre à chaque filtre
def flatchr_scope(url: str) -> str:
    """Organisation Flatchr commune aux URL du widget, de la vitrine et des annonces."""
    if not web_url(url):
        return ""
    o = urlsplit(url)
    host = host_of(url)
    if platform_name(host) != "flatchr":
        return ""
    path = unquote(o.path)
    if "\\" in path or any(part in (".", "..") for part in path.split("/")):
        return ""
    tenant = host.removesuffix(".flatchr.io") if host.endswith(".flatchr.io") else ""
    if tenant in ("careers", "flatchr", "www"):
        tenant = ""
    match = re.fullmatch(r"/(?:[a-z]{2}/)?company/([a-z0-9-]+)(?:/vacancy/[a-z0-9_-]+)?/?", path)
    if match and (not tenant or tenant == match[1]):
        return f"flatchr.io/company/{match[1]}"
    if not path.strip("/") and re.fullmatch(r"[a-z0-9-]+", tenant):
        return f"flatchr.io/company/{tenant}"
    return ""


WTTJ_POSTING = re.compile(r"/(?:[a-z]{2}/)?companies(?:-v\d+)?/([\w-]+)/jobs/([^/?#]+)/?")


def wttj_aliases(url: str) -> list[str]:
    """Adresses de la même annonce WTTJ (organisation et annonce identiques) avec ou sans langue, avec ou sans « / » final."""
    match = WTTJ_POSTING.fullmatch(urlsplit(url or "").path) if wttj_scope(url or "") else None
    if not match or host_of(url) != "welcometothejungle.com":
        return []
    return [f"https://www.welcometothejungle.com{lang}/companies/{match[1]}/jobs/{match[2]}{tail}"
            for lang in ("", "/fr", "/en") for tail in ("", "/")]


def wttj_scope(url: str) -> str:
    """Organisation WTTJ désignée, indépendante de la langue ; une recherche n'en désigne aucune."""
    if not web_url(url):
        return ""
    o = urlsplit(url)
    host = host_of(url)
    if (platform_name(host) != "wttj" or o.scheme != "https" or o.username
            or o.port not in (None, 443)):
        return ""
    path = unquote(o.path)
    if "\\" in path or any(p in (".", "..") for p in path.split("/")):
        return ""
    if host in ("welcometothejungle.com", "welcomekit.co"):
        match = re.match(r"^/(?:[a-z]{2}/)?companies(?:-v\d+)?/([\w-]+)(?:/|$)", o.path)  # « companies-v1 » : alias
        return f"{host}/companies/{match[1]}" if match else ""
    if host.endswith(".welcomekit.co") and host.split(".")[0] not in {
        "api", "developers", "app", "www",
    }:
        return host
    return ""


def same_site(host: str, home: str) -> bool:
    """`host` est le site `home` ou l'un de ses sous-domaines (« jobs.acme.fr » pour « acme.fr »)."""
    host, home = host.lower().removeprefix("www."), home.lower().removeprefix("www.")
    return bool(home) and _under(host, home)


def registrable(host: str) -> str:
    """Domaine enregistrable de `host` (« jobs.acme.fr » -> « acme.fr »).
    shortcut: deux derniers libellés, faux pour les suffixes à deux niveaux (co.uk, com.au) ; liste publique si besoin."""
    labels = host.lower().removeprefix("www.").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ""


CAREER_LABELS = {"job", "jobs", "emploi", "emplois", "career", "careers", "carriere", "carrieres", "recrutement",
                 "recrutements", "recrute", "recrutons", "talent", "talents", "joinus"}


def career_host(host: str, home: str) -> bool:
    """`host` est un site de recrutement de la même organisation que `home` : même domaine enregistrable, et un libellé
    de recrutement dans la partie propre de l'hôte (« job.acme.fr », « acmebat.jobs.groupe.fr »)."""
    host = host.lower().removeprefix("www.")
    domain = registrable(host)
    if not domain or domain != registrable(home):
        return False
    own = host.removesuffix(domain).strip(".").split(".")
    return any(label in CAREER_LABELS or label.split("-")[0] in CAREER_LABELS for label in own if label)


def base_url(body: str, page_url: str) -> str:
    """Adresse de référence des liens relatifs : balise `<base href>` de la page, sinon la page elle-même."""
    m = re.search(r"<base\s[^>]*href\s*=\s*[\"']([^\"']+)[\"']", (body or "")[:20000], re.I)
    base = urljoin(page_url, H.unescape(m.group(1).strip())) if m else page_url
    return base if web_url(base) else page_url


# ---------------------------------------------------------------- validation d'un lien de recrutement
BLOG_SEGMENT = re.compile(r"^(?:blog|blogs|actualites?|actus?|news|articles?|categorie|category|tag|tags|presse|magazine)$",
                          re.I)
STRONG_EMPLOYMENT = re.compile(r"recrut|offres? d['’ ]?emplois?|\bemplois?\b|\bpostes?\b|\bjobs?\b|careers?|hiring|"
                               r"vacanc|\bcdi\b|\bcdd\b", re.I)
EMPLOYMENT = re.compile(STRONG_EMPLOYMENT.pattern + r"|candidat|postuler|\bcv\b|\bstages?\b|stagiaire|alternan|apprenti|"
                        r"talents?|rejoi(?:gnez|ndre)", re.I)
ADMISSION = re.compile(r"admission|dipl[ôo]me|inscri(?:ption|vez|re)|[ée]tudiants?|scolarit|parcoursup|p[ée]dagogi|"
                       r"rentr[ée]e|[ée]quivalence|bachelor|mast[eè]re", re.I)
MIN_JUDGED_TEXT = 200  # en deçà, la page est une coquille (rendu JavaScript) : rien à juger avant le rendu


APP_ROOT = re.compile(r"<(?:div|main|app-root)[^>]+id=[\"'](?:app|root|__next|__nuxt|q-app|application)[\"']|<app-root\b", re.I)
NOSCRIPT_JS = re.compile(r"<noscript[^>]*>[^<]{0,300}(?:javascript|js)\b", re.I)


MOUNT = re.compile(r"listing|job-?list|offers?|offres|jobs|vacanc|careers?|recrut|postes", re.I)
SHELL_TEXT = 600  # texte visible d'une coquille : menus et pied de page seulement


def javascript_shell(body: str) -> bool:
    """La liste ne s'affiche qu'après exécution du JavaScript : coquille d'application (racine d'application ou
    avertissement <noscript>, presque aucun texte visible) ou emplacement de liste vide qu'un script remplit
    (`<div data-widget="listing"></div>`). Une page statique, même courte, n'en est pas une."""
    soup = BeautifulSoup(body or "", "html.parser")
    scripted = bool(soup.find("script", src=True))
    mount = scripted and any(
        not el.get_text(strip=True) and not el.find(True)
        and any(MOUNT.search(" ".join(v) if isinstance(v, list) else str(v)) for k, v in el.attrs.items()
                if k in ("id", "class") or k.startswith("data-"))
        for el in soup.find_all(["div", "section", "ul"]))
    if mount:
        return True
    if not (APP_ROOT.search(body or "") or NOSCRIPT_JS.search(body or "")):
        return False
    for tag in soup.find_all(("script", "style", "noscript", "template", "svg")):
        tag.decompose()
    return len(_text(soup.body or soup)) < SHELL_TEXT


DATED_PATH = re.compile(r"/(?:19|20)\d{2}/[01]?\d/")  # article daté (« /2021/07/19/… »)


def link_problem(url: str) -> str:
    """Motif lisible dans l'adresse seule : page de blog ou d'actualités (« /blog/categorie/recrutement/ », article
    daté « /2021/07/19/… »)."""
    path = urlsplit(url or "").path
    if any(BLOG_SEGMENT.match(part) for part in path.split("/")) or DATED_PATH.search(path):
        return "page de blog ou d'actualités, pas une page de recrutement"
    return ""


def recruitment_problem(requested: str, final: str, body: str) -> str:
    """Motif pour lequel la page atteinte n'est pas une page de recrutement, sinon « » : renvoi vers l'accueil du site,
    blog ou actualités, page d'admission d'une école (vocabulaire des études sans vocabulaire de l'emploi) ou page
    sans aucun mot du recrutement (« carrière » seul est ambigu : carrière de sable). Une coquille JavaScript n'est
    pas jugée."""
    if urlsplit(final).path.strip("/") == "" and urlsplit(requested).path.strip("/"):
        return "le lien renvoie à la page d'accueil du site"
    if problem := link_problem(final) or link_problem(requested):
        return problem
    soup = BeautifulSoup(body or "", "html.parser")
    title = _text(soup.title) if soup.title else ""
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    text = f"{title}\n{html_to_text(str(soup), limit=20_000)}"
    if len(text) < MIN_JUDGED_TEXT:
        return ""
    strong, admission = len(STRONG_EMPLOYMENT.findall(text)), len(ADMISSION.findall(text))
    if admission >= 4 and admission >= 2 * max(strong, 1) and strong <= 1:
        return "page d'admission d'une école, pas une page de recrutement"
    if not EMPLOYMENT.search(text):
        return "aucun vocabulaire de recrutement sur la page"
    return ""


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def norm(value: str) -> str:
    value = re.sub(r"\s+", " ", H.unescape(value or "").lower()).strip(" .,;:-–—")
    return re.sub(r"^(?:le|la|les|l['’])\s*", "", value)


def split_places(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|/|\bet\b|\bou\b)\s*", value.replace("\xa0", " ").strip())
    seen: dict[str, str] = {}
    for part in parts:
        part = part.strip(" .:-–—")
        if part and len(part) <= 60:
            seen.setdefault(norm(part), part)
    return list(seen.values())


def site_name(body: str) -> str:
    """Nom de l'entreprise donné par la page : `og:site_name`, sinon dernier segment du titre (« Offres | Acme »)."""
    soup = BeautifulSoup(body or "", "html.parser")
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and meta.get("content", "").strip():
        return H.unescape(meta["content"]).strip()
    title = _text(soup.title) if soup.title else ""
    parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+", title) if p.strip()]
    return parts[-1] if len(parts) > 1 else ""


# ---------------------------------------------------------------- pages Carrières du site officiel
def _canonical(url: str) -> str:
    o = urlsplit(url)
    return f"{o.scheme}://{(o.hostname or '').removeprefix('www.')}{o.path.rstrip('/')}" + (f"?{o.query}" if o.query else "")


def career_links(body: str, page_url: str, limit: int = 2) -> list[dict]:
    """[{url, texte}] : liens Carrières / Recrutement / Offres d'emploi de la page, sur le même site ou sur une plateforme
    de recrutement ; à défaut de page Carrières, un lien de candidature spontanée peut être suivi."""
    host = (urlsplit(page_url).hostname or "").removeprefix("www.")
    base = base_url(body, page_url)
    found: dict[str, tuple[int, dict]] = {}
    direct: dict[str, tuple[int, dict]] = {}
    for a in BeautifulSoup(body or "", "html.parser").select("a[href], iframe[src]"):
        href = H.unescape(a.get("href") or a.get("src") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")) or PDF_LINK.search(href):
            continue
        url = urljoin(base, href).split("#")[0]
        o = urlsplit(url)
        wttj = wttj_scope(url)
        flatchr = flatchr_scope(url)
        if flatchr and "/vacancy/" in o.path:
            continue
        if (o.scheme not in ("http", "https") or not o.hostname
                or (DEFERRED_HOST.search(o.hostname) and not wttj)
                or (platform_name(host_of(url)) == "wttj" and not wttj)):
            continue
        # « job.acme.fr », portail de groupe « x.jobs.groupe.fr » : autre hôte de recrutement de la même organisation
        recruiting_host = host_of(url) != host and career_host(o.hostname, host)
        if not same_site(o.hostname, host) and not is_ats(o.hostname) and not recruiting_host:
            continue
        if a.name == "iframe" and not (wttj or flatchr):
            continue
        text = _text(a) if a.name == "a" else "widget de recrutement"
        segment = next((s for s in reversed(o.path.split("/")) if s), "")
        if recruiting_host and not SPONTANEOUS.search(text) and len(text) <= 60 and _canonical(url) != _canonical(page_url):
            found.setdefault(_canonical(url), (2, dict(url=url, texte=text)))
            continue
        if SPONTANEOUS.search(text) or SPONTANEOUS.search(segment):
            if _canonical(url) != _canonical(page_url):
                direct.setdefault(_canonical(url), (1, dict(url=url, texte=text)))
            continue
        offer_listing = OFFER_LIST_TEXT.match(text) and (
            is_ats(o.hostname) or any(CAREER_SEGMENT.fullmatch(part) for part in o.path.split("/"))
            or re.search(r"offres? d['’ ]emplois?", text, re.I))
        score = (2 if CAREER_TEXT.match(text) or offer_listing else 1 if wttj or flatchr or
                 CAREER_SEGMENT.match(segment) and len(text) <= 60 else 0)
        if score and _canonical(url) != _canonical(page_url):
            found.setdefault(_canonical(url), (score, dict(url=url, texte=text)))
    ranked = sorted((found or direct).values(), key=lambda x: -x[0])
    return [item for _, item in ranked[:limit]]


# ---------------------------------------------------------------- entrées d'une page
def _anchors(node: Tag) -> int:
    """Signes d'annonce du bloc : liens PDF et contrat annoncé (balise à classe « type de contrat »)."""
    return (sum(1 for a in node.find_all("a", href=True) if PDF_LINK.search(a["href"]))
            + sum(1 for el in node.find_all(True, class_=LABEL_CLASS) if re.fullmatch(CONTRACT_WORDS, _text(el), re.I)))


def _same_kind(head: Tag):
    """Autres titres comparables à `head` : même balise de titre ; pour un titre sans balise de titre (bloc en gras
    d'une carte), même balise et mêmes classes."""
    if head.name in HEADINGS:
        return lambda x: x is not head
    return lambda x: x is not head and x.get("class") == head.get("class")


def _container(head: Tag) -> Tag:
    """Bloc de l'annonce : le plus haut ancêtre du titre qui ne contient aucun autre titre de même balise ni signe d'une autre annonce."""
    node, other = head, _same_kind(head)
    while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
        if (any(other(x) for x in node.parent.find_all(head.name)) or len(_text(node.parent)) > MAX_BLOCK
                or 0 < _anchors(node) < _anchors(node.parent)):
            break
        node = node.parent
    return node


def _lines(container: Tag, title: str) -> list[str]:
    lines = [ln.strip() for ln in html_to_text(str(container), limit=12_000).split("\n")]
    return [ln for ln in lines if ln and ln != title]


def _iso(day: str) -> str:
    try:
        d, m, y = (int(x) for x in day.split("/"))
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return ""


def _entry(head: Tag, container: Tag, url: str) -> Entry | None:
    title = _text(head)
    lines = _lines(container, title)
    if GENERIC_TITLE.match(title) or PLACEHOLDER.search("\n".join(lines)):
        return None  # rubrique de la page (« Nos offres », « Offre d'emploi ») ou gabarit non rempli : jamais une annonce
    entry = Entry(title=title, spontaneous=bool(SPONTANEOUS.search(title)))
    entry.closed = bool(FILLED.search("\n".join(lines)))
    for a in container.find_all("a", href=True):
        href = H.unescape(a["href"]).strip()
        if PDF_LINK.search(href):
            entry.pdf_url = entry.pdf_url or urljoin(url, href)
        elif not entry.href and href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            entry.href = urljoin(url, href).split("#")[0]
    wrapper = container.find_parent("a", href=True)  # carte entièrement cliquable : son lien est celui de l'annonce
    if not entry.href and wrapper and not H.unescape(wrapper["href"]).strip().startswith(("#", "mailto:", "javascript:")):
        entry.href = urljoin(url, H.unescape(wrapper["href"]).strip()).split("#")[0]
    for el in container.find_all(True, class_=LABEL_CLASS):
        if len(_text(el)) <= 30 and re.fullmatch(CONTRACT_WORDS, _text(el), re.I):
            entry.label = _text(el)
            break
    for el in container.find_all(True, class_=PLACE_CLASS):
        if _text(el) and len(_text(el)) <= 80 and not el.find(True, class_=PLACE_CLASS):
            entry.places += [p for p in split_places(_text(el)) if p not in entry.places]
    for el in container.find_all(True, class_=EMPLOYER_CLASS):
        if _text(el) and len(_text(el)) <= 80 and not el.find(True, class_=EMPLOYER_CLASS):
            entry.employer = _text(el)
            break
    listed = {m.group(2).lower() for line in lines[:12] if (m := META_LINE.match(line))}
    if (len(listed) >= 2 or title.endswith(":")) and not entry.pdf_url:
        return None  # liste de contrats (« Filtrer les offres par : Alternance, CDD, CDI ») : un filtre, pas une annonce
    for line in lines[:12]:  # lignes de l'en-tête de l'annonce
        if m := META_LINE.match(line):
            entry.label = entry.label or m.group(2)
            entry.date = entry.date or _iso(m.group(1) or "")
            entry.places += [p for p in split_places(m.group(3) or "") if p not in entry.places]
        elif m := CONTRACT_LINE.match(line):
            entry.label = entry.label or m.group(1)
        elif (m := PLACE_LINE.match(line)) and not entry.places:
            entry.places = split_places(m.group(1))
    entry.text = "\n".join(lines)
    if entry.spontaneous or entry.label or entry.closed:
        return entry
    return entry if entry.pdf_url and entry.places else None  # un PDF sans contrat ni lieu (plaquette) n'est pas une annonce


def page_entries(body: str, page_url: str) -> list[Entry]:
    """Annonces de la page, dans l'ordre. Les blocs d'un même titre répété (même balise, mêmes classes) sont comparés ;
    ceux qui portent un contrat annoncé ou un lien PDF sont des entrées. Un bloc qui en contient un autre est écarté."""
    soup = BeautifulSoup(body or "", "html.parser")
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for form in soup.find_all("form"):  # formulaire de candidature (liste de contrats souhaités) : jamais une annonce
        if form.find(["textarea", "select"]) or form.find("input", attrs={"type": "file"}):
            form.decompose()
    base = base_url(body, page_url)
    groups: dict[tuple, list[Tag]] = {}
    for head in soup.find_all(HEADINGS):
        if 2 <= len(_text(head)) <= 160:
            groups.setdefault((head.name, tuple(head.get("class", []))), []).append(head)
    cards: dict[tuple, list[Tag]] = {}  # titres de cartes sans balise de titre : blocs en gras ou « title » répétés
    for head in soup.find_all(["div", "span", "p", "strong", "b"], class_=CARD_TITLE_CLASS):
        if 2 <= len(_text(head)) <= 120 and not head.find(["div", "p", "ul", "table", *HEADINGS]):
            cards.setdefault((head.name, tuple(head.get("class", []))), []).append(head)
    groups.update({key: heads for key, heads in cards.items() if len(heads) >= 2})
    order = {id(h): i for i, h in enumerate(soup.find_all(True))}
    found: list[tuple[int, Tag, Entry]] = []
    for group in groups.values():
        for head in group:
            container = _container(head)
            entry = _entry(head, container, base)
            if entry:
                found.append((order[id(head)], container, entry))
    return [e for _, _, e in sorted(_outermost_single(found), key=lambda item: item[0])]


def _inside(inner: Tag, outer: Tag) -> bool:
    return inner is not outer and any(p is outer for p in inner.parents)


def _outermost_single(found: list[tuple[int, Tag, Entry]]) -> list[tuple[int, Tag, Entry]]:
    """Blocs imbriqués : un bloc qui en contient plusieurs est un conteneur de page (écarté) ; s'il n'en contient qu'un
    (sous-rubrique « Vos missions » d'une annonce), le titre le plus haut dans la page fait foi."""
    keep = [x for x in found if sum(_inside(o[1], x[1]) for o in found) < 2]
    keep = [x for x in keep if not any(o is not x and (o[1] is x[1] or _inside(o[1], x[1]) or _inside(x[1], o[1]))
                                       and o[0] < x[0] for o in keep)]
    return keep


def merge_shared_pdf(entries: list[Entry]) -> list[Entry]:
    """Deux entrées du même PDF et du même titre sont une seule annonce : tous les lieux affichés sont conservés."""
    merged: dict[tuple, Entry] = {}
    for e in entries:
        key = (e.pdf_url, norm(e.title)) if e.pdf_url else (id(e),)
        if key in merged:
            merged[key].places += [p for p in e.places if norm(p) not in {norm(x) for x in merged[key].places}]
        else:
            merged[key] = e
    return list(merged.values())


# ---------------------------------------------------------------- faits lus dans un PDF
def pdf_facts(text: str) -> dict:
    """{contrat, lieux, date} : le contrat nommé (CDI, CDD, intérim), les lieux cités par une phrase de mission ou de
    poste (jamais l'adresse de l'en-tête) et la date de publication ; absent = non cité."""
    head = text[:4000]
    contract = next((m.group(1).upper() for m in re.finditer(r"\b(CDI|CDD)\b", head)), "")
    places: list[str] = []
    for m in PLACE_PHRASE.finditer(head):
        places += [p for p in split_places(m.group(1)) if p not in places]
    day = re.search(r"(?:publi\w+ le|mise en ligne le|date de publication)\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", head, re.I)
    return dict(contrat=contract, lieux=[p for p in places if not (p.isupper() and len(p) <= 4)],  # « IA », « NLP » : sigles
                date=_iso(day.group(1)) if day else "")


def place_alerts(shown: list[str], cited: list[str]) -> list[str]:
    """Contradictions entre les lieux affichés par la page et ceux du document."""
    cities = [p for p in cited if norm(p) not in REGIONS]
    regions = [p for p in cited if norm(p) in REGIONS]
    if cities and shown:
        missing = [p for p in shown if norm(p) not in {norm(c) for c in cities}]
        if missing:
            return [f"lieu du document ({', '.join(cities)}) différent du lieu affiché ({', '.join(missing)})"]
    elif regions and shown:
        return [f"le document ne cite que la région {', '.join(regions)} : lieu(x) affiché(s) non confirmé(s) ({', '.join(shown)})"]
    return []


# ---------------------------------------------------------------- signes d'une annonce individuelle
NO_AD_SIGN = "aucun signe propre à une annonce individuelle"  # début de l'alerte d'une page sans signe d'annonce
AD_HEADING = re.compile(
    r"^\W*(?:(?:les?|la|vos|votre|nos|notre|ses|son|sa|tes|ton|your|the)\s+)?(missions?|profils?|comp[ée]tences|contexte|dur[ée]e|lieu|localisation|location|r[ée]mun[ée]ration|salaire|"
    r"date de (?:d[ée]but|prise de poste)|type de contrat|nature du contrat|responsabilit[ée]s|pr[ée]requis|avantages)\b[^\n]{0,60}$",
    re.I | re.M)
AD_REFERENCE = re.compile(r"^\W*r[ée]f(?:[ée]rence)?\.?(?: de l['’]offre)?\s*:?\s*([A-Z0-9][\w\-/.]{2,})\s*$", re.I | re.M)


def ad_signs(text: str, structured: bool = False, apply_url: str = "") -> list[str]:
    """Signes propres à UNE annonce, lus dans le texte visible de la page (jamais dans les scripts ni les filtres d'une
    liste) : données structurées JobPosting, lien de candidature propre à la page, référence de l'offre, ou au moins deux
    rubriques de poste (missions, profil, lieu, durée…). Un mot comme « postuler » ne suffit pas : une page de
    présentation (« Votre alternance chez… ») en contient aussi."""
    signs = ["données structurées JobPosting"] if structured else []
    if apply_url:
        signs.append("lien de candidature propre à la page")
    if AD_REFERENCE.search(text or ""):
        signs.append("référence de l'offre")
    headings = {m.group(1).lower() for m in AD_HEADING.finditer(text or "")}
    if len(headings) >= 2:
        signs.append("rubriques de poste (" + ", ".join(sorted(headings)[:4]) + ")")
    return signs
