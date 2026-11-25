"""Un fichier de session atomique, sans données d'accès ni base d'entreprises."""

import errno
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .models import SCHEMA_VERSION
from .privacy import redact


@contextmanager
def file_lock(path: Path, wait=None):
    """Verrou de fichier libéré par le système même après un arrêt du processus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if wait is None:
                    raise ValueError("Cette session est déjà utilisée par une autre recherche.") from exc
                wait()
            else:
                break
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(redact(data), stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(3):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.03 * (attempt + 1))


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "results": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("results"), dict)
        ):
            raise ValueError("Session incompatible : format 1.0 attendu.")
        for key, result in data["results"].items():
            if (
                not isinstance(result, dict)
                or result.get("company_id") != key
                or not isinstance(result.get("company"), dict)
                or not isinstance(result.get("checkpoint"), dict)
                or not all(
                    isinstance(result.get(k), list)
                    for k in ("contacts", "offers", "sources", "errors")
                )
            ):
                raise ValueError("Session endommagée : structure des résultats invalide.")
        parts = self.path.with_suffix(self.path.suffix + ".parts")
        for path in parts.glob("*.json"):
            result = json.loads(path.read_text(encoding="utf-8"))
            key = result.get("company_id")
            old = data["results"].get(key)
            if (
                old
                and result.get("company") == old["company"]
                and result.get("updated_at", "") > old.get("updated_at", "")
            ):
                data["results"][key] = result
        return data

    def save(self, data):
        atomic_json(self.path, data)

    def save_company(self, result):
        parts = self.path.with_suffix(self.path.suffix + ".parts")
        atomic_json(
            parts / (hashlib.sha256(result["company_id"].encode()).hexdigest() + ".json"), result
        )

    @contextmanager
    def lock(self):
        """Un seul rédacteur ; le système libère le verrou même après un crash."""
        with file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            yield
