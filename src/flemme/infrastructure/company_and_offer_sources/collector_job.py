"""Contrat d'échange entre l'application et le lecteur 3ATAY exécuté à part (version 1).

Trois fichiers JSON dans un dossier de travail : `job.json` (tâche), `results.jsonl` (une ligne par portail terminé),
`progress.json` (avancement). Les fichiers lus viennent d'un autre processus, voire de GitHub : tailles bornées,
version vérifiée, aucune ligne hors schéma acceptée."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

VERSION = 1
MAX_BYTES = 200_000_000
JOB, RESULTS, PROGRESS = "job.json", "results.jsonl", "progress.json"
JOB_KEYS = ("version", "job_id", "shard", "portals", "previous", "options")


class JobFormatError(ValueError):
    pass


def _replace(temporary: Path, destination: Path) -> None:
    """Renommage atomique ; sous Windows il échoue tant qu'un lecteur tient le fichier ouvert : quelques essais."""
    for attempt in range(100):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.01)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _replace(temporary, path)


def _read_text(path: Path) -> str:
    for attempt in range(100):  # Windows : le fichier est brièvement inaccessible pendant son remplacement
        try:
            if path.stat().st_size > MAX_BYTES:
                raise JobFormatError(f"{path.name} dépasse {MAX_BYTES // 1_000_000} Mo")
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.01)


def job_bytes(*, job_id: str, portals: list[dict], previous_attempts: dict[str, int], options: dict,
              shard: tuple[int, int] = (0, 1)) -> bytes:
    """Contenu de `job.json` (envoyé tel quel, chiffré, à GitHub)."""
    return json.dumps(dict(version=VERSION, job_id=job_id, shard=list(shard), portals=portals,
                           previous=previous_attempts, options=options), ensure_ascii=False).encode("utf-8")


def write_job(directory: Path, **fields) -> None:
    path = Path(directory) / JOB
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(job_bytes(**fields))
    _replace(temporary, path)


def read_job(directory: Path) -> dict:
    path = Path(directory) / JOB
    try:
        data = json.loads(_read_text(path))
    except FileNotFoundError:
        raise JobFormatError("tâche absente") from None
    except (OSError, ValueError) as error:
        if isinstance(error, JobFormatError):
            raise
        raise JobFormatError("tâche illisible") from None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise JobFormatError(f"version de tâche non prise en charge ({data.get('version') if isinstance(data, dict) else '?'})")
    missing = [key for key in JOB_KEYS if key not in data]
    if missing:
        raise JobFormatError("tâche incomplète : " + ", ".join(missing))
    return data


def append_result(path: Path, url: str, records: list, info: dict, attempts: int) -> None:
    line = json.dumps(dict(url=url, records=records, info=info, attempts=attempts), ensure_ascii=False)
    if len(line) > MAX_BYTES:
        raise JobFormatError("résultat trop volumineux")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_results(path: Path) -> dict[str, tuple[list, dict, int]]:
    """Portail -> (annonces, bilan, tentatives). Une ligne finale coupée par un arrêt brutal est ignorée ; une ligne
    invalide ailleurs refuse tout le fichier. Une ligne plus récente remplace la précédente du même portail."""
    path = Path(path)
    return parse_results(_read_text(path)) if path.exists() else {}


def parse_results(text: str) -> dict[str, tuple[list, dict, int]]:
    """Même lecture que `read_results`, sur un texte déjà en mémoire (résultat déchiffré d'un lot GitHub)."""
    if len(text) > MAX_BYTES:
        raise JobFormatError("résultats trop volumineux")
    lines = text.split("\n")
    if lines and lines[-1] != "":
        lines.pop()  # dernière ligne sans saut de ligne : écriture interrompue
    results: dict[str, tuple[list, dict, int]] = {}
    for number, line in enumerate(filter(None, lines), 1):
        try:
            row = json.loads(line)
            url, records, info, attempts = row["url"], row["records"], row["info"], row["attempts"]
        except (ValueError, KeyError, TypeError):
            raise JobFormatError(f"ligne {number} des résultats invalide") from None
        if not (isinstance(url, str) and isinstance(records, list) and isinstance(info, dict)
                and isinstance(attempts, int)):
            raise JobFormatError(f"ligne {number} des résultats hors schéma")
        results[url] = (records, info, attempts)
    return results


def write_progress(directory: Path, *, done: int, total: int, running: list[str], requests: int,
                   activity: dict, finished: bool = False, error: str = "") -> None:
    _write_json(Path(directory) / PROGRESS, dict(done=done, total=total, running=running, requests=requests,
                                                 activity=activity, finished=finished, error=error,
                                                 updated_at=datetime.now(UTC).isoformat()))


def read_progress(directory: Path) -> dict:
    try:
        data = json.loads(_read_text(Path(directory) / PROGRESS))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
