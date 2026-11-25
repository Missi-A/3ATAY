"""Lecture des portails Greenhouse, Lever et Ashby par les connecteurs de Career Ops 1.40.0 (processus Node.js).

Career Ops ne fait que lire le flux public : les écritures SQLite et les règles d'import restent celles de FLEMME. Toute
indisponibilité (Node.js, dossier absent, délai, format inattendu) lève `Unavailable` : le lecteur natif prend le relais.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .company_portal_collector import EMPTY_PORTAL, entry_contract, record, text_fields
from .inline_offers import GENERIC_TITLE, SPONTANEOUS

ENV_ROOT = "FLEMME_CAREER_OPS_DIR"  # dossier de Career Ops : activation explicite, absente ou vide = désactivé


class Unavailable(RuntimeError):
    """Career Ops ne peut pas lire ce portail : le motif est repris dans le bilan du lecteur natif."""


HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
BOARD_PATH = re.compile(r"/([a-zA-Z0-9][a-zA-Z0-9._-]*)/?")  # Ashby : « mistral.ai »
POSTING_PATH = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
BRIDGE = Path(__file__).with_name("career_ops_provider.mjs").resolve()


def _address(url: str):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.netloc.lower() != parts.hostname
            or any(c.isspace() for c in url)):
        raise ValueError("Ce portail n'est pas pris en charge par le pilote Career Ops.")
    return parts


def _board(url: str) -> tuple[str, str, str]:
    parts = _address(url)
    match = BOARD_PATH.fullmatch(parts.path)
    if not match or parts.query or parts.fragment:
        raise ValueError("Indique l'URL de la liste d'offres d'une entreprise.")
    host = parts.hostname
    provider = HOSTS[host]
    scope_host = "job-boards.greenhouse.io" if provider == "greenhouse" else host
    return provider, scope_host, match[1]


def scan(root: Path, url: str, fixture: Path | None = None) -> dict:
    """Exécute le connecteur sans charger de profil Career Ops ni de secrets de la session."""
    provider, _, _ = _board(url)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 22.13 ou supérieur est requis pour lancer Career Ops.")
    command = [node, str(BRIDGE), str(root.resolve()), provider, url]
    if fixture is not None:
        command.append(str(fixture.resolve()))
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"}}
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
            env=env, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Career Ops a dépassé le délai de lecture du portail.") from exc
    except OSError as exc:
        raise RuntimeError("Impossible de démarrer Career Ops avec Node.js.") from exc
    if result.returncode:
        reason = result.stderr.strip().splitlines()[-1][:240] if result.stderr.strip() else "erreur inconnue"
        raise RuntimeError(f"Lecture Career Ops impossible ({reason}).")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("JSON invalide dans le résultat Career Ops.") from exc


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _date(value) -> str:
    if type(value) not in (int, float):
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, UTC).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _posting_id(job: dict, provider: str, scope_host: str, slug: str) -> str:
    try:
        parts = _address(_text(job.get("url")))
    except ValueError:
        return ""
    root, _, suffix = parts.path.strip("/").partition("/")
    if root.casefold() != slug.casefold():
        return ""
    if provider == "greenhouse":
        match = re.fullmatch(r"jobs/([0-9]+)", suffix)
        native_id = match[1] if HOSTS[parts.hostname] == provider and match else ""
    else:
        native_id = suffix if parts.hostname == scope_host and POSTING_PATH.fullmatch(suffix) else ""
    reported_id = job.get("externalId")
    return native_id if not reported_id or str(reported_id).casefold() == native_id.casefold() else ""


def to_survey(receipt: dict, url: str, company: str, known: dict | None = None) -> tuple[list[dict], dict]:
    """Traduit une lecture en relevé FLEMME, avec l'identité de l'offre et son périmètre."""
    provider, host, slug = _board(url)
    if not isinstance(receipt, dict) or receipt.get("provider") != provider or not isinstance(receipt.get("jobs"), list):
        raise ValueError("Résultat Career Ops incompatible avec le portail demandé.")
    scope = f"{host}/{slug.lower()}"
    portal = {"url": url, "plateforme": provider, "entreprise": company, "preuve": (known or {}).get("preuve") or {}}
    rows = {}
    counters = dict(spontanees=0, doublons=0, hors_perimetre=0)
    for job in receipt["jobs"]:
        if not isinstance(job, dict):
            counters["hors_perimetre"] += 1
            continue
        title, public_url = _text(job.get("title")), _text(job.get("url"))
        if SPONTANEOUS.search(title) or SPONTANEOUS.search(public_url):
            counters["spontanees"] += 1
            continue
        reference = _posting_id(job, provider, host, slug)
        if not title or GENERIC_TITLE.fullmatch(title) or not reference:
            counters["hors_perimetre"] += 1
            continue
        ident = f"{provider}:{scope}:{reference.lower()}"
        if ident in rows:
            counters["doublons"] += 1
            continue
        text, label = _text(job.get("description")), _text(job.get("contractLabel"))
        contract, proof, conflict = entry_contract(title, label, text, public_url)
        rows[ident] = record(
            portal, identifiant=ident, portail=scope, page_portail=url, titre=title,
            lieu=_text(job.get("location")), reference=reference, url_officielle=public_url,
            lien_candidature=public_url, date_publication=_date(job.get("postedAt")),
            type_contrat_declare=label, contrat_confirme=contract, preuve_contrat=proof,
            statut="ouverte", http=200, alertes=[conflict] if conflict else [], _text=text, **text_fields(text),
        )
    truncation = receipt.get("truncation") or {}
    partial = bool(truncation or counters["hors_perimetre"])
    reason = truncation.get("reason", "") if isinstance(truncation, dict) else ""
    if counters["hors_perimetre"] and not reason:
        reason = "Liens d'annonces illisibles ou hors du portail."
    return list(rows.values()), dict(etat="partiel" if partial else "complet", lues=len(receipt["jobs"]),
                                     retenues=len(rows), motif=reason, **counters)


def career_ops_root() -> Path | None:
    configured = os.environ.get(ENV_ROOT, "").strip()
    root = Path(configured) if configured else None
    return root if root and (root / "package.json").is_file() else None


def handles(url: str) -> bool:
    """Liste d'offres d'une entreprise sur Greenhouse, Lever ou Ashby (pas une annonce, ni une page intégrée)."""
    try:
        _board(url)
    except ValueError:
        return False
    return True


def read_board(portal: dict) -> tuple[list[dict], dict]:
    """(annonces au format du collecteur, bilan) d'un portail lu par Career Ops, tous contrats confondus ; les filtres
    Stage et Alternance s'appliquent ensuite dans FLEMME. Lève `Unavailable` si Career Ops ne peut pas le lire."""
    root = career_ops_root()
    if root is None:
        raise Unavailable("Career Ops désactivé ou introuvable")
    try:
        receipt = scan(root, portal["url"])
        rows, summary = to_survey(receipt, portal["url"], portal.get("entreprise", ""), portal)
    except (RuntimeError, ValueError) as error:
        raise Unavailable(str(error)) from error
    note = f"Career Ops ({receipt['provider']}) : {summary['retenues']} annonce(s) sur {summary['lues']} lue(s)"
    info = dict(statut_parcours=summary["etat"], remarque=f"{note} ; {summary['motif']}" if summary["motif"] else note,
                spontanees=summary["spontanees"])
    if summary["hors_perimetre"]:  # liens illisibles : la lecture reste partielle, avec son compteur
        info.update(liste_lue=summary["retenues"], total_annonce=summary["lues"] - summary["spontanees"])
    if not rows and summary["etat"] == "complet" and not summary["spontanees"]:
        info["statut_parcours"] = EMPTY_PORTAL  # liste publique lue en entier et vide
    return rows, info
