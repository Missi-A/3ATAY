"""Contrat d'échange v1 : données éphémères d'une recherche, jamais un annuaire."""

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .privacy import redact

SCHEMA_VERSION = "1.0"
# Budget propre de chaque étape (secondes) ; « analysis » mesure la classification finale.
STAGES = dict(discovery=60, identity=90, contacts=120, offers=120, analysis=30)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def web_url(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(
            parts.scheme in ("https", "http")
            and parts.hostname
            and not parts.username
            and not parts.password
            and parts.port in (None, 80, 443)
        )
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    city: str = ""
    location: str = ""
    sector: str = ""
    siren: str = ""
    siret: str = ""
    website: str = ""
    contacts: list[dict] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, row):
        if not isinstance(row, dict):
            raise ValueError("Chaque entreprise doit être un objet.")
        values = {}
        for key in cls.__dataclass_fields__:
            value = row.get(key, [] if key in ("contacts", "source_urls") else "")
            if key in ("contacts", "source_urls"):
                if not isinstance(value, list):
                    raise ValueError(f"{key} doit être une liste.")
            else:
                if not isinstance(value, (str, int)):
                    raise ValueError(f"{key} doit être du texte.")
                value = str(value).strip()
            values[key] = value
        if not values["id"] or not values["name"]:
            raise ValueError("id stable et name sont obligatoires.")
        for key, length in (("siren", 9), ("siret", 14)):
            if values[key] and not re.fullmatch(rf"\d{{{length}}}", values[key]):
                raise ValueError(f"{key} doit contenir {length} chiffres.")
        if values["siren"] and values["siret"] and not values["siret"].startswith(values["siren"]):
            raise ValueError("SIREN et SIRET contradictoires.")
        for url in [values["website"], *values["source_urls"]]:
            if url and not web_url(url):
                raise ValueError(f"URL HTTP(S) publique attendue : {redact(url)}")
        if not all(isinstance(c, dict) for c in values["contacts"]):
            raise ValueError("contacts doit contenir des objets.")
        return cls(**values)


@dataclass
class Config:
    max_pages: int = 40
    max_depth: int = 4
    timeout: float = 12
    attempts: int = 2
    interval: float = 1.2
    max_bytes: int = 5_000_000
    registry: bool = True
    web_discovery: bool = True
    contact_pages: int = 12
    offer_pages: int = 28
    initial_pages: int = 6
    identity_pages: int = 8
    # Filet de sécurité global par entreprise ; chaque étape a son budget (stage_seconds).
    company_seconds: float = 420
    stage_seconds: dict = field(default_factory=lambda: dict(STAGES))
    # Contacts fiables joignables (e-mail ou téléphone) suffisants : l'étape s'arrête plus tôt.
    enough_contacts: int = 6
    source_seconds: float = 12
    concurrency: int = 2
    browser: bool = True
    cache_seconds: int = 21600
    cache_dir: str = ""
    external_offers: bool = True

    def __post_init__(self):
        if (
            not 1 <= self.max_pages <= 500
            or not 1 <= self.contact_pages <= 500
            or not 1 <= self.offer_pages <= 500
            or not 1 <= self.initial_pages <= 100
            or not 1 <= self.identity_pages <= 100
            or not 1 <= self.company_seconds <= 1200
            or not 1 <= self.enough_contacts <= 100
            or not isinstance(self.stage_seconds, dict)
            or not set(self.stage_seconds) <= set(STAGES)
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 600
                for v in self.stage_seconds.values()
            )
            or not 1 <= self.source_seconds <= 30
            or not 1 <= self.concurrency <= 4
            or not 0 <= self.cache_seconds <= 86400
            or not 0 <= self.max_depth <= 10
            or not 1 <= self.timeout <= 60
            or not 1 <= self.attempts <= 3
            or not 0.2 <= self.interval <= 60
            or not 1024 <= self.max_bytes <= 10_000_000
        ):
            raise ValueError("Limites de recherche invalides.")


def new_result(company):
    return dict(
        schema_version=SCHEMA_VERSION,
        company_id=company.id,
        company=asdict(company),
        status="pending",
        reason="",
        coverage="partial",
        reason_codes=[],
        contacts=[],
        offers=[],
        sources=[],
        errors=[],
        official_site="",
        site_verified=False,
        site_evidence=[],
        updated_at=utc_now(),
        checkpoint=dict(initialized=False, queue=[], visited=[], completed_pages=[], failed=[]),
    )


def failure_codes(message):
    """Interprète aussi les erreurs des sessions 1.0, sans modifier le transport."""
    message = message.lower()
    codes = set()
    if "robots" in message:
        codes.add("robots")
    if re.search(r"\b(?:401|403|429)\b", message) or any(
        word in message for word in ("refus", "anti-robot", "source suspendue")
    ):
        codes.add("source_refused")
    if any(word in message for word in ("javascript", "chromium", "playwright")):
        codes.add("javascript")
    if any(word in message for word in ("budget", "limite d'exploration", "délai de l'entreprise")):
        codes.add("budget")
    if "crawl-delay" in message or "crawl_delay" in message:
        codes.add("crawl_delay")
    if re.search(r"\b404\b", message):
        codes.add("source_not_found")
    if not codes or any(word in message for word in ("timeout", "connexion", "connection")):
        codes.add("network")
    return codes


def coverage_details(result):
    """Couverture des sources distincte du statut historique et de la fiabilité."""
    status = result["status"]
    codes = set(result.get("checkpoint", {}).get("limit_reasons", []))
    for error in result.get("errors", []):
        if error.get("active", True):
            codes.update(error.get("reason_codes") or failure_codes(error.get("error", "")))
    for outcome in result.get("source_outcomes", []):
        if outcome.get("error"):
            codes.update(failure_codes(outcome["error"]))
    if status == "incomplete" and result.get("reason"):
        # Les motifs historiques sont génériques : seule la limite explicite est exploitable.
        if "budget" in failure_codes(result["reason"]):
            codes.add("budget")
    if not result.get("official_site") and not result["company"].get("website"):
        codes.add("no_domain")
    useful = result["contacts"] or result["offers"]
    if (result.get("official_site") and not result.get("site_verified")) or any(
        item.get("reliability") != "reliable" for item in [*result["contacts"], *result["offers"]]
    ):
        codes.add("uncertain")
    if status == "cancelled":
        codes.add("cancelled")
        coverage = "cancelled"
    elif status in ("pending", "running"):
        coverage = "partial"
    elif useful:
        coverage = (
            "complete"
            if status in ("reliable_contacts", "uncertain_contacts") and not codes - {"uncertain"}
            else "partial"
        )
    elif codes.intersection(("source_refused", "robots")):
        coverage = "blocked"
    elif status == "no_contacts" or "no_domain" in codes:
        coverage = "not_found"
    else:
        coverage = "partial"
    return dict(coverage=coverage, reason_codes=sorted(codes))
