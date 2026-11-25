"""Accès SQLite unique (DATA_API) : transactions, verrou d'écriture, migrations de schéma.

Une connexion par thread (SQLite l'exige) ; toutes les écritures passent par `transaction()`,
qui sérialise les écrivains du processus et ouvre une transaction `BEGIN IMMEDIATE`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_PACKAGE = "flemme.infrastructure.database.schema_migrations"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # -- connexions -----------------------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                  check_same_thread=True)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("PRAGMA busy_timeout = 30000")
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    @contextmanager
    def transaction(self):
        """Transaction d'écriture ; annulée entièrement en cas d'exception."""
        with self._write_lock:
            con = self.connection()
            if con.in_transaction:  # transaction imbriquée : la plus externe décide
                yield con
                return
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            else:
                try:
                    con.execute("COMMIT")
                except BaseException:
                    # Validation impossible (E/S, disque plein) : sans annulation la connexion resterait en
                    # transaction, et les écritures suivantes, prises pour imbriquées, ne seraient jamais validées.
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.connection().execute(sql, params).fetchone()

    # -- schéma ---------------------------------------------------------------------------
    def migrate(self, backup_dir: Path | None = None) -> list[str]:
        """Applique dans l'ordre les fichiers `NNN_nom.sql` non encore appliqués.

        Sur une base déjà utilisée, une sauvegarde vérifiée est faite dans `backup_dir` avant la première
        migration en attente ; si elle n'est pas exploitable, rien n'est migré.
        """
        con = self.connection()
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {row[0] for row in con.execute("SELECT version FROM schema_migrations")}
        files = sorted(
            (f for f in resources.files(SCHEMA_PACKAGE).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        pending = [f for f in files if f.name.removesuffix(".sql") not in done]
        if pending and done and backup_dir is not None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.verified_backup(Path(backup_dir) / f"avant-migration-{pending[0].name[:3]}-{stamp}.sqlite3")
            prune_backups(Path(backup_dir), "avant-migration-", keep=MIGRATION_BACKUPS_KEPT)
        applied = []
        for file in pending:
            version = file.name.removesuffix(".sql")
            script = file.read_text(encoding="utf-8")
            with self.transaction() as tx:
                for statement in _statements(script):
                    tx.execute(statement)
                tx.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, utc_now()))
            applied.append(version)
        return applied

    def verified_backup(self, target: Path) -> Path:
        """Sauvegarde puis relecture : intégrité et mêmes tables / migrations que la base source."""
        self.backup_to(target)
        try:
            check_backup(target, expected=self.fingerprint())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def fingerprint(self) -> tuple[int, int]:
        """(nombre de tables, nombre de migrations appliquées) : contrôle léger d'une sauvegarde."""
        con = self.connection()
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        return tables, con.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]

    def restore_from(self, backup: Path, safety_dir: Path | None = None) -> Path | None:
        """Remplace la base par une sauvegarde vérifiée (application fermée). Renvoie la copie de sécurité."""
        check_backup(Path(backup))
        safety = None
        self.close()
        if self.path.exists():
            safety = Path(safety_dir or self.path.parent) / f"avant-restauration-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
            self.backup_to(safety)
            self.close()
        try:
            for suffix in ("-wal", "-shm"):  # journaux de l'ancienne base ; verrouillés si FLEMME tourne encore
                Path(str(self.path) + suffix).unlink(missing_ok=True)
        except PermissionError as exc:
            raise ValueError("FLEMME semble encore ouvert : fermez l'application avant de restaurer.") from exc
        source = sqlite3.connect(f"file:{Path(backup).as_posix()}?mode=ro", uri=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            destination = sqlite3.connect(self.path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return safety

    def backup_to(self, target: Path) -> Path:
        """Copie cohérente (API de sauvegarde SQLite), y compris pendant l'utilisation."""
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self.connection().backup(destination)
        finally:
            destination.close()
        return target


MIGRATION_BACKUPS_KEPT = 5


def check_backup(path: Path, expected: tuple[int, int] | None = None) -> None:
    """Lève ValueError si la sauvegarde est absente, corrompue ou ne ressemble pas à la source."""
    if not Path(path).is_file():
        raise ValueError(f"Sauvegarde introuvable : {Path(path).name}")
    try:
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("contrôle d'intégrité en échec")
            found = (con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
                     con.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise ValueError(f"Sauvegarde illisible : {Path(path).name} ({type(exc).__name__})") from exc
    if expected is not None and found != expected:
        raise ValueError(f"Sauvegarde incomplète : {Path(path).name}")


def prune_backups(directory: Path, prefix: str, keep: int) -> None:
    """Ne garde que les `keep` sauvegardes automatiques les plus récentes d'un même préfixe."""
    found = sorted(directory.glob(f"{prefix}*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in found[keep:]:
        for suffix in ("", "-wal", "-shm"):
            Path(str(old) + suffix).unlink(missing_ok=True)


def _statements(script: str):
    """Découpe un script SQL en instructions complètes (gère les triggers BEGIN…END)."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        if line.strip().startswith("--") and not buffer.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer.strip()
            buffer = ""
    if buffer.strip():
        raise ValueError("Instruction SQL incomplète dans la migration de schéma.")
