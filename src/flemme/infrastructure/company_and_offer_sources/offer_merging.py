"""Recherche séquentielle explicite et rapprochement conservateur des sources."""

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit, urlunsplit

from ...shared.text_normalization import normalized
from ..gmail_api.http_client import ConnectorError
from .inline_offers import flatchr_scope
from .offer_contract import SourceOffer


def canonical_url(value):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return ""
        host = parts.hostname.lower()
        port = parts.port
        if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            host += ":" + str(port)
        return urlunsplit(
            (
                parts.scheme,
                host,
                parts.path or "/",
                parts.query,
                "",
            )
        )
    except ValueError:
        return ""


def flatchr_republication_key(url, title, city, contract, description):
    """Republication si l'organisation, le poste, le lieu, le contrat et le texte complet coïncident."""
    scope = flatchr_scope(url)
    if not scope or "/vacancy/" not in urlsplit(url).path:
        return None
    fields = tuple(" ".join(normalized(value or "").split()) for value in (title, city, contract, description))
    return (scope, *fields) if all(fields) else None


def distinct_publications(offers):
    """Une ligne par republication. Une candidature ou un favori reste accessible depuis l'annonce affichée."""
    selected = {}
    for offer in offers:
        key = flatchr_republication_key(offer.url, offer.title, offer.city, offer.contract, offer.description)
        key = (offer.source, *key) if key else (offer.id,)
        candidate = selected.get(key)
        rank = (not offer.availability, offer.has_application, offer.favorite, offer.published_at, offer.id)
        if candidate is None or rank > candidate[0]:
            selected[key] = (rank, offer)
    return [offer for _, offer in selected.values()]


def offer_identities(offer):
    return [
        list(identity)
        for identity in dict.fromkeys(
            (row.get("source", ""), row["external_id"])
            for row in [offer, *offer.get("source_observations", [])]
            if row.get("external_id")
        )
    ]


def same_offer(first, second):
    url = canonical_url(first.get("url", ""))
    if (
        not url
        or url != canonical_url(second.get("url", ""))
        or normalized(first["role"]) != normalized(second["role"])
    ):
        return False
    if first.get("siret") and second.get("siret"):
        return first["siret"] == second["siret"]
    return (normalized(first["company"]), normalized(first["city"])) == (
        normalized(second["company"]),
        normalized(second["city"]),
    )


@dataclass(frozen=True)
class MergedOffer(SourceOffer):
    observations: tuple[SourceOffer, ...] = ()


def merge_results(offers):
    groups = []
    for item in offers:
        matches = [
            group
            for group in groups
            if any(
                (old.source, old.external_id) == (item.source, item.external_id)
                or (old.source != item.source and same_offer(asdict(old), asdict(item)))
                for old in group
            )
        ]
        if len(matches) == 1:
            if not any(
                (old.source, old.external_id) == (item.source, item.external_id)
                for old in matches[0]
            ):
                matches[0].append(item)
        else:
            groups.append([item])
    return [MergedOffer(**asdict(group[0]), observations=tuple(group)) for group in groups]


def search_sources(connectors, query, cancel=lambda: False, progress=lambda n: None):
    offers, recruiters, warnings = [], [], []
    successes = 0
    for name, connector in connectors:
        if cancel():
            raise ConnectorError("Recherche annulée.")
        try:
            if hasattr(connector, "search_opportunities"):
                found, companies, notes = connector.search_opportunities(query, cancel, progress)
            else:
                found, notes = connector.search(query, cancel, progress)
                companies = []
            offers.extend(found)
            recruiters.extend(companies)
            warnings.extend(name + " : " + note for note in notes)
            successes += 1
        except (ValueError, OSError):
            if cancel():
                raise ConnectorError("Recherche annulée.") from None
            warnings.append(name + " : source indisponible ; les autres résultats sont conservés.")
    if not successes:
        raise ConnectorError("Aucune source sélectionnée n'a pu terminer la recherche.")
    return merge_results(offers), recruiters, warnings
