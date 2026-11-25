"""Conversion des coordonnées publiées en ``Item`` ; fonctions de l'itération 2 réutilisées."""

import re
from urllib.parse import urlsplit

from ...extraction import extract_contacts
from ...pages import host
from ...site_evidence import classify_contact
from ...site_search import name_tokens, other_coordinates, without_third_party_blocks
from ...verification import foreign_email, verify_contact
from ..connector import Item, Lead
from ..keys import TWO_LEVEL

OFFICIAL = "site_officiel"
# Portails à conditions d'utilisation restrictives : lien publié conservé, jamais exploré.
LINK_ONLY = (
    "welcometothejungle.com",
    "hellowork.com",
    "apec.fr",
    "indeed.com",
    "indeed.fr",
    "linkedin.com",
    "jobteaser.com",
    "francetravail.fr",
    "glassdoor.fr",
    "glassdoor.com",
)
LOCAL = re.compile(r"agence|implantation|site de |établissement|etablissement|antenne", re.I)


# Preuve RH explicite : adresse dédiée, ou texte qui la présente comme telle. Une simple
# publication sur une page carrières ne suffit pas (standard, accueil).
RH_LOCAL = re.compile(
    r"^(rh|drh|rrh|hr|recrut\w*|recruiting|recruitment|hiring|jobs?|careers?|carrieres?|"
    r"emplois?|candidat\w*|talents?|ressources?[._-]?humaines)([._-]|$)",
    re.I,
)
RH_TEXT = re.compile(
    r"ressources humaines|human resources|recrut\w*|recruiter|talent acquisition|"
    r"candidature|postuler|\bcv\b|curriculum|offres? d.emploi|\bdrh\b|\brrh\b|\brh\b|\bhr\b",
    re.I,
)
SCOPES = ("rh", "local", "technique", "general", "autre")


def rh_evidence(contact):
    """Raisons explicites de tenir une coordonnée pour un contact RH (liste vide sinon).

    Preuves fortes (propres à la coordonnée) : adresse dédiée, rôle publié, pertinence RH
    relevée par l'extracteur. Preuve faible : texte voisin (``context``)."""
    reasons = []
    if str(contact.get("relevance", "")).lower() in ("rh", "recrutement"):
        reasons.append("fort : contexte RH relevé sur la page")
    local = (contact.get("email") or "").split("@")[0]
    if local and RH_LOCAL.match(local):
        reasons.append(f"fort : adresse dédiée « {local}@ »")
    found = RH_TEXT.search(str(contact.get("role", "")))
    if found:
        reasons.append(f"fort : rôle publié « {found.group(0)} »")
    found = RH_TEXT.search(str(contact.get("context", "")))
    if found:
        reasons.append(f"faible : présentée comme « {found.group(0)} »")
    return reasons


def normalize_scope(raw, contact):
    """(portée canonique, détail, preuves RH) : « general (page recrutement) » → general."""
    raw = str(raw or "general")
    base = raw.split("(")[0].strip()
    detail = raw[len(base) :].strip(" ()")
    base = {"recrutement": "rh"}.get(base, base)
    base = base if base in SCOPES else "autre"
    reasons = rh_evidence(contact)
    if base == "rh" and not reasons:
        base, detail = "general", detail or "page recrutement, aucune mention RH"
    elif base in ("general", "local") and any(r.startswith("fort") for r in reasons):
        base = "rh"
    return base, detail, reasons if base == "rh" else []


def scope_of(contact, page_kind=""):
    """rh | local | technique | general | autre (portée la plus précise publiée)."""
    _, scope = classify_contact(contact, page_kind)
    if scope == "recrutement":
        return "rh"
    text = f"{contact.get('role', '')} {contact.get('context', '')}"
    if page_kind in ("local", "agences") or LOCAL.search(text):
        return "local"
    return scope


# Portails dont l'entreprise est dans le chemin : motif de la racine propre à l'entreprise.
# Éditeur absent de cette table et sans sous-domaine d'entreprise : page de l'éditeur, rejetée.
PATH_BOARDS = {
    "welcometothejungle.com": r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?companies/[^/]+",
    "jobteaser.com": r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?companies/[^/]+",
    "lever.co": r"^/[^/]+",
    "smartrecruiters.com": r"^/[^/]+",
    "ashbyhq.com": r"^/[^/]+",
    "greenhouse.io": r"^/(?!embed/)[^/]+",
    "workable.com": r"^/(?!j/)[^/]+",
    "join.com": r"^/companies/[^/]+",
    "flatchr.io": r"^/company/[^/]+",
}
# Liens courts d'une offre sans entreprise dans le chemin : l'identifiant est conservé.
ID_ROUTES = {"workable.com": r"^/j/[^/]+"}
GENERIC_SUBDOMAINS = {"www", "jobs", "careers", "career", "apply", "boards", "job-boards", "app"}


def portal_root(url):
    """Racine du portail d'une entreprise (sous-domaine ou route connue), sinon "" pour une
    page de l'éditeur lui-même (accueil, « propulsé par », témoignages clients)."""
    parts = urlsplit(url)
    name = (parts.hostname or "").lower()
    labels = name.split(".")
    # Domaine de l'éditeur (ex. teamtailor.com), même s'il héberge des sous-domaines clients.
    vendor = ".".join(labels[-3:] if ".".join(labels[-2:]) in TWO_LEVEL else labels[-2:])
    label = name[: -len(vendor)].rstrip(".") if name.endswith(vendor) else ""
    # Compte client après d'éventuels sous-domaines génériques (careers.endel.icims.com).
    if label and any(part not in GENERIC_SUBDOMAINS for part in label.split(".")):
        return f"https://{name}/"
    path = parts.path.rstrip("/")
    for table in (PATH_BOARDS, ID_ROUTES):
        found = re.match(table[vendor], path) if vendor in table else None
        if found:
            return f"https://{name}{found.group(0)}"
    return ""


# Mots trop génériques pour rattacher un portail à une entreprise.
TENANT_STOP = {
    "groupe", "group", "societe", "france", "sas", "sarl", "etablissements", "compagnie",
    "grand", "port", "services", "service", "industrie", "industry", "engineering", "conseil",
    "solutions", "international", "the", "des", "les", "careers", "career", "jobs", "emploi",
}  # fmt: skip


# Sous-domaines techniques ou génériques d'un éditeur : jamais le compte d'une entreprise.
TECHNICAL_LABELS = GENERIC_SUBDOMAINS | {
    "tt", "support", "help", "status", "api", "cdn", "assets", "images", "static", "docs",
    "developer", "developers", "go", "partners", "partner", "marketplace", "blog", "news",
    "connect", "login", "auth", "mail", "info",
}  # fmt: skip


def portal_belongs(url, company, profile=None, site_url=""):
    """Le portail est celui de l'entreprise : domaine du site retenu, ou compte (sous-domaines
    non techniques, segment de route) dont un mot entier est un mot significatif du nom, le sigle
    déclaré, le nom complet ou le libellé du site retenu (égalité, jamais un fragment).

    Refusés : ``tt.teamtailor.com`` (technique), ``altradio`` pour Altrad (fragment), liens
    courts d'offre ``/j/<id>`` qui ne nomment aucune entreprise."""
    from ...site_evidence import site_label
    from ...verification import normalized
    from ..keys import domain_key

    if site_url and domain_key(url) and domain_key(url) == domain_key(site_url):
        return True
    root = portal_root(url)
    if not root:
        return False
    parts = urlsplit(root)
    host_name = (parts.hostname or "").lower()
    labels = host_name.split(".")
    vendor = ".".join(labels[-3:] if ".".join(labels[-2:]) in TWO_LEVEL else labels[-2:])
    path = parts.path.strip("/")
    if path:
        if any(re.match(route, parts.path) for route in ID_ROUTES.values()):
            return False  # identifiant d'offre, sans compte d'entreprise
        accounts = [path.split("/")[-1]]
    else:
        accounts = host_name[: -len(vendor)].rstrip(".").split(".")
    pieces, flats = set(), set()
    for account in accounts:
        words = [w for w in re.split(r"[^a-z0-9]+", normalized(account)) if w]
        if not words or account in TECHNICAL_LABELS:
            continue
        pieces |= {w for w in words if w not in TECHNICAL_LABELS}
        flats.add("".join(words))
    profile = profile or {}
    tokens = set()
    for name in (company.name, *profile.get("names", [])):
        words = [re.sub(r"[^a-z0-9]", "", w) for w in normalized(name or "").split()]
        tokens |= {w for w in words if len(w) >= 3 and w not in TENANT_STOP}
        tokens.add("".join(words))  # nom complet : égalité stricte avec le compte
    sigle = re.sub(r"[^a-z0-9]", "", normalized(profile.get("sigle", "") or ""))
    label = site_label(site_url) if site_url else ""
    tokens |= {t for t in (sigle, label) if len(t) >= 2}  # égalité stricte, même courts
    tokens.discard("")
    return bool(tokens & (pieces | flats))


def contact_items(raw_contacts, source=OFFICIAL, via="", page_kind=""):
    items = []
    for c in raw_contacts:
        raw_scope = scope_of(c, page_kind) if "scope" not in c else _scope(c["scope"])
        # Preuve propre à chaque coordonnée : un « jobs@ » voisin ne fait pas du standard un
        # contact RH.
        scope, detail, reasons = normalize_scope(raw_scope, c)
        phone_scope = normalize_scope(raw_scope, dict(c, email=""))
        extra = {k: v for k, v in (("scope_detail", detail), ("rh_evidence", reasons)) if v}
        base = dict(
            url=c.get("source_url", ""),
            confidence=c.get("confidence", "incertain"),
            scope=scope,
            label=c.get("role") or c.get("name") or "",
            excerpt=c.get("context", "") or c.get("role", ""),
            via=via,
            source=source,
            extra=extra,
        )
        if c.get("email"):
            items.append(Item(kind="email", value=c["email"], **base))
        if c.get("phone"):
            scope_p, detail_p, reasons_p = phone_scope
            extra_p = {
                k: v for k, v in (("scope_detail", detail_p), ("rh_evidence", reasons_p)) if v
            }
            items.append(
                Item(kind="phone", value=c["phone"], **dict(base, scope=scope_p, extra=extra_p))
            )
        if c.get("name") and (c.get("email") or c.get("phone")):
            items.append(
                Item(
                    kind="person",
                    value=c["name"],
                    key_extra=dict(
                        email=c.get("email", ""), phone=c.get("phone", ""), role=c.get("role", "")
                    ),
                    **base,
                )
            )
    return items


def _scope(scope):
    return {"recrutement": "rh"}.get(scope, scope)


def coordinate_items_and_leads(others, source=OFFICIAL, via=""):
    """Formulaires, portails, pages LinkedIn ; un portail à API publique devient une piste."""
    from .ats import detect_ats  # module ATS (connecteurs de recrutement)
    from .careers import detect_careers

    items, leads = [], []
    for o in others:
        url, scope = o["value"], _scope(o.get("scope", "general"))
        if o["type"] == "formulaire":
            items.append(
                Item(
                    kind="form",
                    value=url,
                    url=o["source_url"],
                    confidence="probable",
                    scope=scope,
                    via=via,
                    source=source,
                )
            )
        elif o["type"] == "page_linkedin":
            items.append(
                Item(
                    kind="profile",
                    value=url,
                    url=o["source_url"],
                    confidence="probable",
                    scope="general",
                    label="linkedin",
                    via=via,
                    source=source,
                )
            )
        else:
            found = detect_careers(url)
            board = found["public_url"] if found else portal_root(url)
            if not board:
                continue  # page de l'éditeur (« propulsé par ») : pas le portail de l'entreprise
            items.append(
                Item(
                    kind="portal",
                    value=board,
                    url=o["source_url"],
                    confidence="probable",
                    scope="rh",
                    label=host(url),
                    via=via,
                    source=source,
                )
            )
            if found:
                leads.append(
                    Lead("careers_portal", found["public_url"], origin=source, via=via, data=found)
                )
            found = detect_ats(url)
            if found:
                leads.append(Lead("ats", found["public_url"], origin=source, via=via, data=found))
    return items, leads


def read_page(ctx, page, kind, via=""):
    """Coordonnées publiées sur une page du site retenu, avec les règles de l'itération 2."""
    company, site = ctx.company, ctx.site_url
    level = ctx.site_level
    found, foreign = [], []
    for contact in extract_contacts(without_third_party_blocks(page, company), company):
        contact.setdefault("source_url", page.url)
        contact = verify_contact(contact, company, site, level == "confirme", level)
        contact["scope"] = scope_of(contact, kind)
        (foreign if foreign_email(contact, company, site) else found).append(contact)
    tokens = [re.sub(r"[^a-z0-9]", "", t) for t in name_tokens(company, ctx.profile)]
    items = contact_items(found, via=via, page_kind=kind)
    more, leads = coordinate_items_and_leads(other_coordinates(page, kind, tokens), via=via)
    for c in foreign:
        ctx.log("écarté", f"{c['email']} : domaine tiers ← {c['source_url']}", company.name)
    return items + more, leads, foreign
