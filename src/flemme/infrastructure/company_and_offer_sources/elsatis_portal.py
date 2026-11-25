"""Portails Carrières Elsatis (`<client>-mrs-prod.elsatis.fr/recruitment/public/offers/...`).

La page est une application Angular : le HTML ne contient aucune annonce. Une fois ouverte, elle s'initialise elle-même
(accès anonyme du visiteur), puis charge les annonces par pages de dix (`.../recruitment/offers/search?page=N`) à
chaque « Afficher plus ». On lit ce que la page charge, comme un visiteur : on n'appelle jamais l'API ni le service
d'authentification nous-mêmes. Le compteur affiché (« 10 sur 16 ») sert de contrôle de complétude.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from .listing_expander import BrowserUnavailable, browser_page

PATH = re.compile(r"^/recruitment/public/offers(?:/|$)")
RESOURCE = re.compile(r"<(?:script|iframe|link)\s[^>]*(?:src|href)=[\"'][^\"']*elsatis\.fr", re.I)
SEARCH = "/recruitment/offers/search"
MORE = re.compile(r"afficher plus|voir plus|charger plus|show more|load more", re.I)
COUNTER = re.compile(r"(\d+)\s+sur\s+(\d+)")
TRANSIENT_QUERY = {"q", "search", "page"}  # le reste de la requête (idJobboard…) appartient au lien de l'entreprise
MAX_CLICKS = 60
LOAD_SECONDS = 15


def elsatis_host(url: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return host == "elsatis.fr" or host.endswith(".elsatis.fr")


def is_elsatis(url: str, body: str = "") -> bool:
    """Portail Elsatis d'après l'adresse (hôte elsatis.fr, ou chemin `/recruitment/public/offers` sur le domaine de
    l'entreprise) ou d'après la page fournie (elle charge elle-même ses scripts depuis elsatis.fr). Une page qui renvoie
    seulement par un lien vers un portail Elsatis n'en est pas un : ce lien est suivi à part."""
    return elsatis_host(url) or bool(PATH.match(urlsplit(url or "").path)) or bool(RESOURCE.search(body or ""))


def offer_url(portal_url: str, code: str) -> str:
    """Page de l'annonce, avec les paramètres propres à l'entreprise (idJobboard…) du lien enregistré."""
    o = urlsplit(portal_url)
    # Les paramètres sont repris tels qu'écrits (« Welcome%20to%20the%20jungle » ne devient pas « Welcome+to+… »).
    query = "&".join(part for part in o.query.split("&") if part and unquote(part.split("=", 1)[0]).lower() not in TRANSIENT_QUERY)
    return f"{o.scheme}://{o.netloc}/recruitment/public/offers/offerbycode/{code}" + (f"?{query}" if query else "")


@dataclass
class ElsatisList:
    offers: list[dict] = field(default_factory=list)  # annonces uniques, par code
    name: str = ""  # nom du client affiché par la page (« arep | Offres d'emploi »)
    visible: int | None = None  # total « N sur M » affiché
    complete: bool = False
    reason: str = ""
    empty: bool = False  # liste vide CONFIRMÉE par la lecture complète


def read_list(url: str, allow, take=lambda: True, *, max_clicks: int = MAX_CLICKS) -> ElsatisList:
    pages: list[list] = []
    failures: list[str] = []

    def on_response(response):
        if SEARCH not in response.url:
            return
        if response.status not in (200, 206):
            failures.append(f"HTTP {response.status}")
            return
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - corps illisible
            failures.append("réponse illisible")
            return
        pages.append(data) if isinstance(data, list) else failures.append("réponse inattendue")

    def post_ok(target: str) -> bool:  # l'initialisation anonyme de la page : jeton émis par le service du même éditeur
        o = urlsplit(target)
        return elsatis_host(target) and o.path.endswith("/protocol/openid-connect/token")

    result = ElsatisList()
    try:
        with browser_page(url, lambda target: allow(target) or elsatis_host(target), take, post_ok, on_response) as page:
            def wait(seen: int, seconds: float) -> bool:  # attend une nouvelle page de résultats (ou une erreur)
                for _ in range(int(seconds / 0.3)):
                    if len(pages) + len(failures) > seen:
                        return True
                    page.wait_for_timeout(300)
                return False

            def settle():
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:  # noqa: BLE001
                    pass

            if not wait(0, LOAD_SECONDS):
                result.reason = "la page n'a pas chargé sa liste d'annonces"
                return result
            clicks = 0
            while clicks < max_clicks and not failures:
                settle()
                button = page.get_by_text(MORE)
                if not button.count():
                    break
                seen = len(pages)
                button.first.click(timeout=4000)
                clicks += 1
                if not wait(seen, 10):
                    result.reason = "« Afficher plus » n'a chargé aucune page supplémentaire"
                    break
            else:
                if clicks >= max_clicks:
                    result.reason = "limite de pages atteinte avant la fin de la liste"
            settle()
            result.name = (page.title() or "").split("|")[0].strip()  # le titre n'existe qu'une fois la page chargée
            counter = COUNTER.findall(page.inner_text("body") or "")
            result.visible = int(counter[-1][1]) if counter else None
            more_left = bool(page.get_by_text(MORE).count())
    except BrowserUnavailable as error:
        result.reason = result.reason or str(error)
        more_left = False
    codes: dict[str, dict] = {}
    for item in (offer for batch in pages for offer in batch if isinstance(offer, dict)):
        if code := str(item.get("code") or "").strip():
            codes.setdefault(code, item)
    result.offers = list(codes.values())
    if failures:
        result.reason = result.reason or f"échec de lecture de la liste ({failures[0]})"
    elif not result.reason and more_left:
        result.reason = "« Afficher plus » encore proposé après la dernière page lue"
    elif not result.reason and result.visible is not None and len(codes) < result.visible:
        result.reason = f"{len(codes)} annonces lues sur {result.visible} affichées par le portail"
    result.complete = bool(pages) and not result.reason
    result.empty = result.complete and not codes and result.visible in (None, 0)
    return result
