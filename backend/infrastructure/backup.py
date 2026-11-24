"""Consistent online SQLite snapshots, never overwriting an existing backup."""

from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from backend.infrastructure.database import (
    DatabaseError, DatabaseSettings, _connect, absolute_path, read_identity, validate_identity, validate_binding,
)


def create_backup(settings: DatabaseSettings, destination: Path, *, allow_previous: bool = False) -> Path:
    """Publish a new directory containing database.sqlite and manifest.json."""
    validate_binding(settings)
    target = absolute_path(destination)
    if target.exists() or target == settings.path or target == settings.official_path:
        raise DatabaseError('backup destination must be a new independent directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.backup-', dir=target.parent))
    try:
        snapshot = staging / 'database.sqlite'
        with closing(_connect(settings.path, readonly=True)) as source:
            validate_identity(source, settings, allow_previous=allow_previous)
            with closing(sqlite3.connect(snapshot)) as copy:
                source.backup(copy)
                # Make backup self-contained; no WAL/SHM required to read it.
                copy.execute('PRAGMA journal_mode=DELETE')
                copy.execute('PRAGMA foreign_keys=ON')
                source_identity = validate_identity(copy, settings, allow_previous=allow_previous)
                # Preserve UUID/generation/schema and facts, but a backup must never
                # start as a formal writer or a writable sandbox merely by changing paths.
                copy.execute("UPDATE system_metadata SET instance_role='backup'")
                copy.commit()
                identity = read_identity(copy, allow_previous=allow_previous)
                if (copy.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                        or copy.execute('PRAGMA foreign_key_check').fetchall() != []):
                    raise DatabaseError('backup integrity or foreign key check failed')
        with snapshot.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        manifest = {**asdict(identity), 'backup_at': datetime.now(UTC).isoformat(),
                    'source_instance_role': source_identity.instance_role,
                    'sha256': digest, 'integrity_check': 'ok', 'foreign_key_check': 'ok'}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        # A valid backup directory is nonempty: rename cannot replace it.
        if target.exists():
            raise DatabaseError('backup destination already exists')
        staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
