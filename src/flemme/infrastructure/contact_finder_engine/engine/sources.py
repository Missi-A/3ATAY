"""Connecteurs gratuits : registre public et recherche RSS publique sans clé."""

import json
import re
from urllib.parse import quote, urlencode, urljoin
from xml.etree import ElementTree

from .models import utc_now, web_url
from .verification import normalized

REGISTRY = "https://recherche-entreprises.api.gouv.fr/search"


def registry_contacts(company, web, names=None):
    """Dirigeants et sites déclarés ; ``names`` reçoit sigle et enseignes (noms commerciaux)."""
    query = company.siren or company.siret or company.name
    url = REGISTRY + "?" + urlencode({"q": query, "per_page": 5})
    page = web.fetch(url, "registre public")
    data = json.loads(page.body)
    candidates, websites = [], []
    for item in data.get("results", []):
        siren = str(item.get("siren", ""))
        exact_id = bool(
            (company.siren or company.siret) and siren == (company.siren or company.siret[:9])
        )
        if (company.siren or company.siret) and not exact_id:
            continue
        headquarters = item.get("siege") or {}
        legal_name = re.sub(r"\([^)]*\)", "", str(item.get("nom_complet", "")))
        legal_name = re.sub(r"\s+(?:sas|sasu|sarl|sa)\s*$", "", legal_name, flags=re.I)
        name_match = normalized(legal_name) == normalized(company.name)
        city_match = bool(
            company.city
            and normalized(company.city) == normalized(headquarters.get("libelle_commune", ""))
        )
        if not exact_id and not name_match:
            continue
        associated = exact_id or name_match and city_match
        for person in item.get("dirigeants", []):
            role = str(person.get("qualite") or "Dirigeant")
            if re.search(r"commissaire|auditeur|liquidateur|mandataire", normalized(role)):
                continue
            name = " ".join(str(person.get(k) or "") for k in ("prenoms", "nom")).strip()
            if not name:
                continue
            candidates.append(
                dict(
                    name=name,
                    role=role,
                    company_id=company.id,
                    company=company.name,
                    email="",
                    profile_url="",
                    source_url=page.url,
                    discovered_at=utc_now(),
                    reliability="uncertain",
                    association_verified=exact_id,
                    registry_siren=siren,
                    registry_city=str(headquarters.get("libelle_commune", "")),
                    evidence=[
                        f"Registre public : SIREN {siren}",
                        "SIREN identique"
                        if exact_id
                        else "Nom et ville identiques"
                        if associated
                        else "Homonyme possible",
                    ],
                )
            )
        if associated and names is not None:
            brands = [item.get("sigle"), *(headquarters.get("liste_enseignes") or [])]
            names.extend(
                str(b) for b in brands if isinstance(b, str) and b.strip() and str(b) not in names
            )
        if associated:
            for key in ("site_internet", "website"):
                if web_url(item.get(key, "")):
                    websites.append(item[key])
    return candidates, websites


def search_web(company, web):
    """Une recherche bornée ; si le moteur la refuse, aucun contournement."""
    query = f'"{company.name}" "{company.city}" site officiel recrutement'
    page = web.fetch(
        "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"}),
        "découverte web publique",
    )
    root = ElementTree.fromstring(page.body)
    results = []
    for item in root.findall(".//item")[:8]:
        link = item.findtext("link", "")
        if web_url(link):
            results.append(link)
    return results


def encyclopedia_sites(company, web, checkpoint=None):
    """Lien officiel déclaré dans une fiche encyclopédique ; l'identité reste à vérifier."""
    from .web import FetchError

    checkpoint = checkpoint if checkpoint is not None else {}
    for index, (language, title) in enumerate(title_variants(company.name)):
        if index < checkpoint.get("next_page", 0):
            continue
        try:
            page = web.fetch(
                f"https://{language}.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                "annuaire encyclopédique public",
            )
        except FetchError as exc:
            if "HTTP 404" in str(exc):
                checkpoint["next_page"] = index + 1
                continue
            raise
        links = _encyclopedia_links(company, page)
        if links:
            item = page.soup.select_one("#t-wikibase a[href]")
            found = re.search(r"\b(Q\d+)\b", item["href"]) if item else None
            if found:
                checkpoint["item"] = found.group(1)
            return links
        checkpoint["next_page"] = index + 1
    return []


def title_variants(name):
    """Titres Wikipédia : casse réelle des articles (« Orange (entreprise) », pas « ORANGE »)."""
    name = re.sub(r"\s+", " ", name).strip()
    if not name.isupper():  # casse déjà réelle : le nom tel quel
        return [
            ("fr", name),
            ("fr", name + " (entreprise)"),
            ("en", name),
            ("en", name + " (company)"),
        ]
    first = name[:1].upper() + name[1:].lower()
    title = re.sub(r"[\w']+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), name)
    variants = [
        ("fr", name),
        ("fr", first),
        ("fr", first + " (entreprise)"),
        ("fr", title),
        ("en", first),
        ("en", first + " (company)"),
    ]
    return list(dict.fromkeys(variants))


def wikidata_item_sites(item, company, web):
    """Sites (P856) de la page publique d'un élément Wikidata, si son SIREN (P1616) concorde.

    Les API Wikidata (SPARQL, Action API, REST, EntityData) sont interdites aux robots par
    robots.txt ; seule la page HTML de l'élément est autorisée, lue comme une page."""
    siren = company.siren or company.siret[:9]
    if not re.fullmatch(r"Q\d+", item or "") or not re.fullmatch(r"\d{9}", siren or ""):
        return []
    page = web.fetch(f"https://www.wikidata.org/wiki/{item}", "page Wikidata publique")
    sirens = {
        re.sub(r"\D", "", node.get_text(" ", strip=True))
        for node in page.soup.select(
            "#P1616 .wikibase-statementview-mainsnak .wikibase-snakview-value"
        )
    }
    if siren not in sirens:
        return []
    links = [
        a["href"]
        for a in page.soup.select(
            "#P856 .wikibase-statementview-mainsnak .wikibase-snakview-value a[href]"
        )
    ]
    return list(dict.fromkeys(url for url in links if web_url(url)))[:3]


def _encyclopedia_links(company, page):
    soup = page.soup
    heading = soup.find("h1")
    name = re.sub(r"\s*\([^)]*\)\s*$", "", heading.get_text(" ", strip=True)) if heading else ""
    # Introduction réelle : les bandeaux (homonymie, avertissements) et l'infobox sont ignorés.
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in soup.select("p")
        if p.get_text(strip=True)
        and not p.find_parent(class_=re.compile(r"bandeau|homonymie|hatnote|infobox|metadata"))
    ]
    intro = normalized(" ".join(paragraphs[:4]))
    acronym = (
        company.name.isupper()
        and 2 <= len(company.name) <= 5
        and re.search(rf"\b{re.escape(normalized(company.name))}\b", intro)
    )
    if normalized(name) != normalized(company.name) and not acronym:
        return []
    if not re.search(
        r"\b(?:entreprise|societe|editeur|groupe|company|corporation|business|software)\b", intro
    ):
        return []
    links = []
    for row in soup.select(".infobox tr, .infobox_v2 tr"):
        label = row.find("th")
        if label and normalized(label.get_text(" ", strip=True)) in (
            "site web",
            "site internet",
            "website",
        ):
            links.extend(urljoin(page.url, a["href"]) for a in row.select("a.external[href]"))
    for anchor in soup.select("a.external[href]"):
        if normalized(anchor.get_text(" ", strip=True)) in ("site officiel", "official website"):
            links.append(urljoin(page.url, anchor["href"]))
    return list(dict.fromkeys(url for url in links if web_url(url)))[:3]
