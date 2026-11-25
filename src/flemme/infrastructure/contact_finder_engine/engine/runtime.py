"""Délais, exclusion par domaine et cache HTTP local partagé, indépendant de Qt."""

import gzip
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .control import Control
from .privacy import redact
from .storage import atomic_json, file_lock


class DeadlineExceeded(TimeoutError):
    pass


class StageExceeded(TimeoutError):
    """Budget d'une étape atteint : résultats conservés, l'étape suivante démarre."""


class DomainSuspended(PermissionError):
    """Délai de reprise encore en cours pour le domaine ou le service."""


class CompanyControl(Control):
    def __init__(self, parent, seconds):
        self.parent = parent
        self.cancelled, self.running = parent.cancelled, parent.running
        self.deadline = time.monotonic() + seconds
        self.stage_deadline = None
        self.paused = 0.0  # temps passé en pause, exclu des temps d'étape

    def check(self):
        paused = not self.running.is_set()
        before = time.monotonic()
        self.parent.check()
        if paused:
            pause = time.monotonic() - before
            self.paused += pause
            self.deadline += pause
            if self.stage_deadline is not None:
                self.stage_deadline += pause
        now = time.monotonic()
        if now >= self.deadline:
            raise DeadlineExceeded("Délai de l'entreprise atteint ; reprise contrôlée disponible.")
        if self.stage_deadline is not None and now >= self.stage_deadline:
            raise StageExceeded("Budget de l'étape atteint ; résultats conservés.")

    def remaining(self):
        self.check()
        end = min(self.deadline, self.stage_deadline or self.deadline)
        return max(0.1, end - time.monotonic())

    @contextmanager
    def stage(self, seconds):
        previous = self.stage_deadline
        self.stage_deadline = time.monotonic() + seconds
        try:
            yield
        finally:
            self.stage_deadline = previous


class DomainPolicy:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.mutex = threading.Lock()
        self.locks, self.last, self.delay, self.blocked = {}, {}, {}, {}

    def _path(self, origin):
        return self.directory / hashlib.sha256(origin.encode()).hexdigest()

    @staticmethod
    def _read(path, default):
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return default

    @contextmanager
    def request(self, origin, control, interval):
        if self.directory is not None:
            path = self._path(origin)
            with file_lock(path.with_suffix(".lock"), lambda: control.wait(0.1)):
                control.check()
                if self._read(path.with_suffix(".blocked"), 0.0) > time.time():
                    raise DomainSuspended("Source suspendue après refus ; aucune nouvelle requête.")
                pace = path.with_suffix(".pace")
                last, last_delay = self._read(pace, (0.0, 0.0))
                delay = max(interval, self.delay.get(origin, last_delay))
                elapsed = max(0, time.time() - last)
                control.wait(max(0, max(delay, last_delay) - elapsed))
                atomic_json(pace, (time.time(), delay))
                yield
            return
        with self.mutex:
            lock = self.locks.setdefault(origin, threading.Lock())
        while not lock.acquire(timeout=0.1):
            control.check()
        try:
            control.check()
            if self.blocked.get(origin, 0) > time.time():
                raise DomainSuspended("Source suspendue après refus ; aucune nouvelle requête.")
            delay = max(interval, self.delay.get(origin, 0))
            control.wait(max(0, self.last.get(origin, 0) + delay - time.monotonic()))
            self.last[origin] = time.monotonic()
            yield
        finally:
            lock.release()

    def block(self, origin, seconds=3600):
        until = time.time() + seconds
        if self.directory is not None:
            path = self._path(origin).with_suffix(".blocked")
            with file_lock(path.with_suffix(".block-lock"), lambda: time.sleep(0.05)):
                atomic_json(path, max(until, self._read(path, 0.0)))
        else:
            self.blocked[origin] = until


class HttpCache:
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, directory=None):
        self.path = Path(directory) / "http.sqlite" if directory else None
        self.mutex = threading.Lock()
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("PRAGMA auto_vacuum = FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, expires REAL NOT NULL, payload BLOB NOT NULL)"
            )
            connection.execute("DELETE FROM responses WHERE expires < ?", (time.time(),))
            self._trim(connection)

    @classmethod
    def _trim(cls, connection):
        rows = connection.execute(
            "SELECT key, length(payload) FROM responses ORDER BY expires, key"
        ).fetchall()
        size = sum(row[1] for row in rows)
        for key, length in rows:
            if size <= cls.MAX_BYTES:
                break
            connection.execute("DELETE FROM responses WHERE key=?", (key,))
            size -= length

    def get(self, key):
        if self.path is None:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        with sqlite3.connect(self.path, timeout=10) as connection:
            row = connection.execute(
                "SELECT payload FROM responses WHERE key=? AND expires>?", (digest, time.time())
            ).fetchone()
        return redact(json.loads(gzip.decompress(row[0]))) if row else None

    def put(self, key, payload, ttl):
        if self.path is None or redact(key) != key:
            return
        data = gzip.compress(json.dumps(redact(payload), ensure_ascii=False).encode())
        if len(data) > self.MAX_BYTES:
            return
        with self.mutex, sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("DELETE FROM responses WHERE expires < ?", (time.time(),))
            connection.execute(
                "INSERT OR REPLACE INTO responses VALUES(?,?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), time.time() + ttl, data),
            )
            self._trim(connection)


def purge(directory):
    """Efface uniquement le cache Finder explicitement désigné, même déjà ouvert."""
    if not directory:
        return
    path = Path(directory) / "http.sqlite"
    if path.is_file():
        with sqlite3.connect(path, timeout=10) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("DELETE FROM responses")
            connection.commit()
            connection.execute("VACUUM")
