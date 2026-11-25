"""Lecteur 3ATAY exécuté à part : `python -m flemme.infrastructure.company_and_offer_sources.remote_runner DOSSIER`.

Lit `job.json`, relève les portails avec le même `collect()` que l'application, écrit `results.jsonl` au fil de l'eau
et `progress.json` chaque seconde. Un fichier `CANCEL` dans le dossier demande l'arrêt.
Codes de sortie : 0 terminé, 1 erreur (tâche refusée ou erreur inattendue), 2 annulé, 3 terminé avec portails partiels."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from . import collector_job as cj
from . import company_portal_collector

PARTIAL = ("partiel", "inaccessible")
HEARTBEAT_SECONDS = 1.0


def run(directory: Path, collector=company_portal_collector) -> int:
    directory = Path(directory)
    try:
        job = cj.read_job(directory)
    except cj.JobFormatError as error:
        print(f"Tâche refusée : {error}", file=sys.stderr)
        return 1
    portals, results_path = job["portals"], directory / cj.RESULTS
    if workers := job["options"].get("portal_workers"):
        collector.PORTAL_WORKERS = int(workers)
    cancelled = lambda: (directory / "CANCEL").exists()  # noqa: E731
    written: dict[str, int] = {}
    state = dict(finished=False, error="")

    def write_progress() -> None:
        activity = collector.survey_activity.snapshot()
        running = [] if state["finished"] else [item["host"] for item in activity.get("running", [])]
        cj.write_progress(directory, done=len(written), total=len(portals), running=running,
                          requests=collector.requests_made(), activity=activity, **state)

    def checkpoint(results, records, readings) -> None:
        for url, (found, info, attempts) in results.items():
            if written.get(url) != attempts or url not in written:
                cj.append_result(results_path, url, found, info, attempts)
                written[url] = attempts

    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            write_progress()

    thread = threading.Thread(target=heartbeat, daemon=True, name="runner-heartbeat")
    results: dict = {}
    try:
        getattr(collector, "begin_survey", lambda: None)()
        write_progress()
        thread.start()
        collector.collect(portals, lambda ratio, message: write_progress(), cancelled, checkpoint=checkpoint)
        results = cj.read_results(results_path)
    except Exception as error:  # noqa: BLE001 - jamais de message de l'erreur : il peut citer une page ou une adresse
        stop.set()
        state["error"] = type(error).__name__
        write_progress()
        print(f"Erreur inattendue : {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        stop.set()
    state["finished"] = True
    write_progress()
    if cancelled():
        return 2
    return 3 if any(info.get("statut_parcours") in PARTIAL for _, info, _ in results.values()) else 0


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage : remote_runner DOSSIER", file=sys.stderr)
        return 1
    return run(Path(arguments[0]))


if __name__ == "__main__":
    sys.exit(main())
