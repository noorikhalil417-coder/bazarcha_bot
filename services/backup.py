"""SQLite online backup and validated restore."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from database import SCHEMA_VERSION, Database


def create_backup(database: Database, directory: Path) -> Path:
    if directory.is_symlink():
        raise ValueError("پوشهٔ پشتیبان نباید پیوند نمادین باشد.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / f"bazaarche-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.sqlite3"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    completed = False
    try:
        with database.exclusive():
            database.checkpoint()
            with database.connect() as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
        target.chmod(0o600)
        completed = True
    finally:
        if not completed:
            target.unlink(missing_ok=True)
    return target


def validate_backup(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("فایل پشتیبان معتبر نیست.")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if result != "ok" or not 1 <= version <= SCHEMA_VERSION or not {"users", "ads", "categories"}.issubset(tables):
            raise ValueError("سلامت یا نسخهٔ پشتیبان معتبر نیست.")


def restore_backup(database: Database, source: Path, backup_directory: Path, *, confirmed: bool = False) -> Path:
    if not confirmed:
        raise ValueError("بازیابی دیتابیس نیازمند تأیید صریح مدیر است.")
    validate_backup(source)
    if source.resolve() == database.path.resolve():
        raise ValueError("فایل پشتیبان و پایگاه‌داده نباید یکی باشند.")
    with database.exclusive():
        pre_restore_backup = create_backup(database, backup_directory)
        database.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = database.path.with_name(f".{database.path.name}.{uuid.uuid4().hex}.restore")
        uri = f"file:{quote(str(source.resolve()))}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as backup, closing(sqlite3.connect(temporary_path)) as target:
                backup.backup(target)
            temporary_path.chmod(0o600)
            validate_backup(temporary_path)
            with database.connect() as current:
                current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.replace(temporary_path, database.path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database.path}{suffix}")
                if sidecar.exists() or sidecar.is_symlink():
                    sidecar.unlink()
            database.initialize()
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return pre_restore_backup


def prune_backups(directory: Path, retention_days: int) -> int:
    if retention_days < 1:
        raise ValueError("مدت نگهداری پشتیبان باید دست‌کم یک روز باشد.")
    if directory.is_symlink():
        raise ValueError("پوشهٔ پشتیبان نباید پیوند نمادین باشد.")
    if not directory.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
    pattern = re.compile(r"^bazaarche-\d{8}T\d{12}Z\.sqlite3$")
    removed = 0
    for candidate in directory.iterdir():
        if candidate.is_symlink() or not candidate.is_file() or not pattern.fullmatch(candidate.name):
            continue
        if candidate.stat().st_mtime < cutoff:
            candidate.unlink()
            removed += 1
    return removed