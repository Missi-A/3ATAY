"""Contrat commun des connecteurs de la cascade.

Un connecteur lit une source, sans connaître les autres : il reçoit une piste (``Lead``) et
retourne des informations sourcées (``Item``) et de nouvelles pistes. L'orchestrateur décide de
l'ordre, des limites, de la fusion et de la sauvegarde. Ajouter ou retirer une source revient à
ajouter ou retirer un connecteur de la liste.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Nature des pistes : company (identité seule), site (racine retenue), page (page à lire),
# careers (page carrière), ats (portail d'un éditeur de recrutement à API publique),
# careers_portal (site carrières hébergé : flux RSS Teamtailor, page publique Flatchr).
LEAD_KINDS = ("company", "site", "page", "careers", "ats", "careers_portal")


@dataclass
class Lead:
    kind: str
    url: str = ""
    origin: str = ""
    via: str = ""
    depth: int = 0
    data: dict = field(default_factory=dict)


@dataclass
class Item:
    """Information publiée : ``url`` est la page qui la publie, ``via`` ce qui l'a fait trouver."""

    kind: str
    value: str
    url: str
    confidence: str = "incertain"
    scope: str = "general"
    label: str = ""
    excerpt: str = ""
    via: str = ""
    # Source comptabilisée si elle diffère du connecteur (ex. contact lu sur le site officiel
    # par le connecteur DuckDuckGo : source « site_officiel », via « duckduckgo »).
    source: str = ""
    key_extra: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Outcome:
    items: list = field(default_factory=list)
    leads: list = field(default_factory=list)
    queries: int = 0
    # Verdict de site éventuel : dict(url=..., level=confirme|probable|groupe, basis=...).
    site: dict = None
    error: str = ""
    raw: dict = None


class SourceUnavailable(Exception):
    """Source indisponible pour cette série (refus, quota, identifiants absents) : bascule."""


class Limits:
    """Cadence et quota d'une source, persistés pour survivre à une reprise.

    ``min_interval`` : secondes entre deux appels ; ``max_calls`` : plafond par série (None = aucun).
    """

    def __init__(self, name, min_interval=0.0, max_calls=None, state_dir=None):
        self.name, self.min_interval, self.max_calls = name, min_interval, max_calls
        self.path = Path(state_dir) / f"limits-{name}.json" if state_dir else None
        self.calls, self.last = 0, 0.0
        # Dernier appel persisté en heure murale : la cadence est respectée après une reprise.
        self.last_wall = 0.0
        if self.path and self.path.exists():
            try:
                state = json.loads(self.path.read_text("utf-8"))
                self.calls, self.last_wall = state.get("calls", 0), state.get("last", 0.0)
            except (OSError, ValueError) as exc:
                # Quota ou cadence illisibles : refus explicite plutôt qu'une remise à zéro.
                raise RuntimeError(f"{self.path} illisible : {exc}") from None

    def exhausted(self):
        return self.max_calls is not None and self.calls >= self.max_calls

    def acquire(self, sleep=time.sleep, clock=time.monotonic, wall=time.time):
        if self.exhausted():
            raise SourceUnavailable(f"quota de {self.max_calls} appel(s) atteint")
        if self.last:
            wait = self.min_interval - (clock() - self.last)
        else:
            wait = self.min_interval - (wall() - self.last_wall) if self.last_wall else 0
        if wait > 0:
            sleep(min(wait, self.min_interval))
        self.last, self.last_wall = clock(), wall()
        self.calls += 1
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"calls": self.calls, "last": self.last_wall}), encoding="utf-8"
            )
            os.replace(tmp, self.path)


class Connector:
    """Base : ``name`` (source comptabilisée), ``phase`` (site | contacts), ``accepts``."""

    name = ""
    phase = "contacts"
    accepts = ("site",)
    min_interval = 0.0
    max_calls = None
    # Différé : appelé quand la file de pistes est vide (ex. recherche de pistes si le site n'a
    # donné aucun contact RH une fois toutes ses pages explorées).
    deferred = False
    # Groupe de désactivation : sources partageant un même service (refus commun).
    group = ""

    @property
    def group_name(self):
        return self.group or self.name

    def idle(self):
        """Libère les ressources lourdes (navigateur) dès que le connecteur a fini son passage.

        Playwright (API synchrone) n'admet qu'une instance par thread : un navigateur laissé ouvert
        empêche un autre connecteur de démarrer le sien (rendu de page, recherche DuckDuckGo)."""

    def attempts(self):
        """Tentatives hors client HTTP partagé (moteur de recherche…), compteur cumulatif."""
        return 0

    def available(self):
        """(bool, motif) : identifiants absents, dépendance manquante, etc."""
        return True, ""

    def wants(self, ctx, lead):
        """Filtre fin : par défaut toute piste d'un type accepté."""
        return lead.kind in self.accepts

    def run(self, ctx, lead):  # pragma: no cover - contrat
        raise NotImplementedError

    def close(self):
        pass
