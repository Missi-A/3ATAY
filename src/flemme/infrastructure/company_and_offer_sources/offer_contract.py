"""Contrat commun aux sources d'offres distantes ou locales."""

from dataclasses import dataclass
from typing import Protocol

from ..gmail_api.http_client import ConnectorError
from .rome_codes import parse_romes


@dataclass(frozen=True)
class OfferQuery:
    romes: str = ""
    latitude: str = ""
    longitude: str = ""
    radius: int = 30
    diploma: str = ""


@dataclass(frozen=True)
class SourceOffer:
    external_id: str
    source: str
    company: str
    city: str
    role: str
    rome: str
    url: str = ""
    description: str = ""
    siret: str = ""
    website: str = ""
    sector: str = ""
    domain: str = ""
    latitude: float | None = None
    longitude: float | None = None
    # Offres relevées sur les sites d'entreprises (tout en texte, comme exige LocalOfferConnector) :
    contract: str = ""  # stage, alternance, cdi, cdd, interim, autre
    apply_url: str = ""
    employer: str = ""  # employeur affiché par l'annonce
    reference: str = ""
    availability: str = ""  # "" (ouverte) | "fermée" | "à recontrôler"
    checked_at: str = ""
    published_at: str = ""
    provenance: str = ""
    attachment: str = ""  # "établi" | "probable" | "groupe" | "aucun" ; vide = source classique (rapprochement par nom)
    company_id: str = ""  # fiche choisie par le relevé (nombre en texte)
    legacy_id: str = ""  # ancienne identité de la même annonce (offres déjà importées)
    details: str = ""  # détails publiés (missions, profil, compétences, durée, début, télétravail) : JSON en texte
    detail_error: str = ""  # détail de l'annonce non récupéré à la dernière relève (ex. « HTTP 429 ») ; vide si lu
    job_domain: str = ""  # domaine du catalogue déduit de l'intitulé et des missions ; vide si indéterminé
    portal_url: str = ""  # page Carrières où l'annonce a été lue
    proof_url: str = ""  # page du site officiel dont le lien désigne cette page Carrières (preuve du rattachement)
    review_note: str = ""  # pourquoi l'annonce reste « à recontrôler » (alertes du relevé)
    confirmable: str = ""  # « 1 » : l'utilisateur peut la confirmer ouverte (disponibilité seule non établie)
    attachment_note: str = ""  # « à vérifier : … » : pourquoi l'offre reste sans fiche (employeur précis ambigu)


class OfferConnector(Protocol):
    def search(
        self, query: OfferQuery, cancel, progress
    ) -> tuple[list[SourceOffer], list[str]]: ...


def query_parameters(query):
    params = {}
    if query.romes.strip():
        codes = parse_romes(query.romes)
        params["romes"] = ",".join(codes)
    if bool(query.latitude.strip()) != bool(query.longitude.strip()):
        raise ConnectorError("Latitude et longitude doivent être renseignées ensemble.")
    if not 0 <= query.radius <= 200:
        raise ConnectorError("Rayon attendu entre 0 et 200 km.")
    if query.latitude.strip():
        try:
            latitude, longitude = float(query.latitude), float(query.longitude)
        except ValueError:
            raise ConnectorError("Coordonnées géographiques invalides.") from None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ConnectorError("Coordonnées géographiques hors limites.")
        params.update(latitude=latitude, longitude=longitude, radius=query.radius)
    if query.diploma:
        if query.diploma not in {"3", "4", "5", "6", "7"}:
            raise ConnectorError("Niveau de diplôme invalide.")
        params["target_diploma_level"] = query.diploma
    return params
