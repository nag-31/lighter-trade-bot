"""Non-destructive backup, integrity, and rollback evidence helpers."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BackupEvidence:
    source: Path
    backup: Path
    sha256: str
    integrity: str


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_integrity(path: str | Path) -> str:
    with sqlite3.connect(path) as con:
        result = str(con.execute("PRAGMA integrity_check").fetchone()[0])
    return result


def backup_sqlite(source: str | Path, destination: str | Path) -> BackupEvidence:
    """Create a consistent SQLite backup without modifying the source."""
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup destination must differ from source")
    with sqlite3.connect(source_path) as source_con, sqlite3.connect(destination_path) as dest_con:
        source_con.backup(dest_con)
    integrity = sqlite_integrity(destination_path)
    if integrity != "ok":
        raise ValueError(f"backup integrity check failed: {integrity}")
    return BackupEvidence(
        source=source_path,
        backup=destination_path,
        sha256=sha256_file(destination_path),
        integrity=integrity,
    )


def restore_sqlite(backup: str | Path, restore_path: str | Path) -> BackupEvidence:
    """Restore into a new path and verify it; never removes an existing file."""
    backup_path = Path(backup)
    restore = Path(restore_path)
    if restore.exists():
        raise FileExistsError(f"restore destination already exists: {restore}")
    restore.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, restore)
    integrity = sqlite_integrity(restore)
    if integrity != "ok":
        raise ValueError(f"restore integrity check failed: {integrity}")
    return BackupEvidence(
        source=backup_path,
        backup=restore,
        sha256=sha256_file(restore),
        integrity=integrity,
    )
