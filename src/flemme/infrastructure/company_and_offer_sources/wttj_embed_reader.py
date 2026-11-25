"""Lecture d'une vitrine Welcome to the Jungle par ses flux publics, sans clé, sans navigateur ni ouverture d'annonce.

1. `api.welcometothejungle.com/api/v1/organizations/{slug}` donne la référence de l'organisation ;
2. `www.welcomekit.co/api/v1/embed?organization_reference=…` liste toutes ses annonces, détail compris.

Une organisation inconnue du service (404) rend `None` : la lecture HTML d'origine reprend. Tout autre échec (403, délai,
format) reste un échec ou une lecture partielle : jamais un repli silencieux ni « aucune offre ».
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from . import company_portal_collector as collector
from .inline_offers import SPONTANEOUS, WTTJ_POSTING, host_of, wttj_scope
from .offer_text import html_to_text

PROFILE = "https://api.welcometothejungle.com/api/v1/organizations/"
EMBED = "https://www.welcomekit.co/api/v1/embed?organization_reference="
MAX_BYTES = 40_000_000  # Crédit Agricole ou B-HIVE : plusieurs Mo ; une réponse plus longue est refusée, jamais tronquée
TIMEOUT = 45
SLUG = re.compile(r"(?:welcometothejungle\.com|welcomekit\.co)/companies/([\w-]+)")
REFERENCE = re.compile(r"[A-Za-z0-9]{3,20}")


def slug_of(url: str) -> str:
    """Slug public d'une vitrine `…/companies/{slug}` ; vide pour un autre portail ou une vitrine à sous-domaine."""
    match = SLUG.fullmatch(wttj_scope(url))
    return match[1] if match else ""


def _call(url: str):
    """-> (JSON ou None, statut HTTP, motif)."""
    host = host_of(url)
    status, _, raw, _, error = collector._fetch(
        url, headers={"Accept": "application/json"}, timeout=TIMEOUT, limit=MAX_BYTES, strict=True,
        allow=lambda target: host_of(target) == host)
    if status != 200:
        return None, status, error or f"HTTP {status}"
    try:
        return json.loads(raw), 200, ""
    except ValueError:
        return None, 200, "réponse illisible (JSON invalide)"


def _failed(step: str, status: int, reason: str):
    """Aucune donnée : « inaccessible » si le service n'a pas répondu, « partiel » s'il a répondu sans format exploitable."""
    return [], dict(statut_parcours="inaccessible" if status != 200 else "partiel", remarque=f"Welcome to the Jungle ({step}) : {reason}")


def _label(job: dict) -> str:
    kind = job.get("contract_type")
    return next((str(kind[k]) for k in ("fr", "en") if isinstance(kind, dict) and kind.get(k)), "")


def _place(job: dict) -> str:
    offices = [o for o in job.get("offices") or [job.get("office")] if isinstance(o, dict)]
    return " ; ".join(dict.fromkeys(str(o.get("city") or o.get("name")) for o in offices if o.get("city") or o.get("name")))


def _offer(portal: dict, employer: str, job: dict, url: str):
    title = " ".join(str(job["name"]).split())[:140]
    mission, profile = html_to_text(job.get("description") or ""), html_to_text(job.get("profile") or "")
    full = "\n\n".join(p for p in (f"Missions\n{mission}" if mission else "", f"Profil\n{profile}" if profile else "") if p)
    label = _label(job)
    contract, proof, warning = collector.entry_contract(title, label, f"{mission} {profile}", url)
    published = str(job.get("published_at") or "")[:10]
    return collector.record(
        portal, titre=title, employeur_affiche=employer, lieu=_place(job), type_contrat_declare=label,
        contrat_confirme=contract, preuve_contrat=proof, reference=str(job["reference"]), url_officielle=url,
        lien_candidature=url, date_publication=published, statut="ouverte", http=200,
        alertes=collector.date_flags(published, "", full, contract) + ([warning] if warning else []),
        _text=f"{mission} {profile}"[:1500], **collector.text_fields(full, missions=mission, profil=profile))


def read(portal: dict):
    """-> (annonces, bilan), ou None si la vitrine n'a pas de flux (organisation inconnue ou adresse à sous-domaine)."""
    slug = slug_of(portal["url"])
    if not slug:
        return None
    data, status, reason = _call(PROFILE + slug)
    if status == 404:
        return None
    if data is None:
        return _failed("profil", status, reason)
    organisation = data.get("organization") if isinstance(data, dict) else None
    reference = organisation.get("reference") if isinstance(organisation, dict) else None
    if not isinstance(reference, str) or not REFERENCE.fullmatch(reference):
        return _failed("profil", 200, "référence d'organisation absente ou invalide")
    data, status, reason = _call(EMBED + reference)
    if data is None:
        return _failed("flux officiel", status, reason)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return _failed("flux officiel", 200, "liste d'annonces absente (format non reconnu)")

    # Le slug renvoyé peut différer du slug public (Antea) : les deux désignent la même organisation.
    scopes = {wttj_scope(f"https://www.welcometothejungle.com/companies/{s}") for s in (slug, organisation.get("slug"))
              if isinstance(s, str) and re.fullmatch(r"[\w-]+", s)}
    scopes |= {wttj_scope(w.get("organization_url") or "") for w in data.get("websites") or [] if isinstance(w, dict)}
    scopes = {s for s in scopes if SLUG.fullmatch(s)}  # jamais un sous-domaine Welcome Kit : il peut regrouper plusieurs organisations
    own = wttj_scope(portal["url"])
    employer = str(data.get("name") or "")
    records, seen, spontaneous, invalid, foreign = [], set(), 0, 0, 0
    for job in jobs:
        try:  # une entrée mal formée (type inattendu) est ignorée : les annonces valides restent lues
            if not isinstance(job, dict) or not job.get("reference") or not str(job.get("name") or "").strip():
                invalid += 1
                continue
            links = [w["url"] for w in job.get("websites_urls") or [] if isinstance(w, dict) and isinstance(w.get("url"), str)]
            # une annonce publiée sur plusieurs sites : l'adresse de la vitrine lue d'abord ; jamais la page de liste
            url = next((u for u in sorted(links, key=lambda u: wttj_scope(u) != own)
                        if wttj_scope(u) in scopes and WTTJ_POSTING.fullmatch(urlsplit(u).path)), "")
            if SPONTANEOUS.search(f"{job['name']} {job.get('slug') or ''} {url}"):
                spontaneous += 1
            elif not url:
                foreign += 1  # sans lien individuel de cette organisation : jamais attribuée sans preuve
            elif job["reference"] not in seen:
                record = _offer(portal, employer, job, url)
                seen.add(job["reference"])
                records.append(record)
        except (TypeError, AttributeError, KeyError, ValueError):
            invalid += 1
    notes = [f"{n} annonce(s) {why}" for n, why in ((invalid, "illisible(s) ignorée(s)"),
                                                    (foreign, "sans lien individuel WTTJ de l'organisation ignorée(s)")) if n]
    status = "partiel" if invalid or foreign else collector.EMPTY_PORTAL if not records else "complet"
    note = f"Welcome to the Jungle (flux officiel) : {len(jobs)} annonce(s) reçue(s)" + "".join(f" ; {n}" for n in notes)
    extra = dict(liste_lue=len(records), total_annonce=len(jobs) - spontaneous) if status == "partiel" else {}
    return records, dict(statut_parcours=status, remarque=note, spontanees=spontaneous, **extra)
