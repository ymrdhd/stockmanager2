"""Maintenance inventory and authenticated snapshot creation; restore stays offline CLI."""

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.infrastructure.backup import list_backups
from backend.infrastructure.database import Database

router = APIRouter(prefix='/api/v1', tags=['maintenance'])
BACKUP_DIRECTORY = 'backups'


def database(request: Request) -> Database:
    db = getattr(request.app.state, 'database', None)
    if db is None:
        raise HTTPException(503, 'database unavailable')
    return db


Database_ = Annotated[Database, Depends(database)]


def backup_directory(db: Database) -> Path:
    """Backups stay beside the formal ledger; callers never choose the filesystem path."""
    return db.settings.path.parent / BACKUP_DIRECTORY


@router.get('/maintenance/backups')
def backups(db: Database_):
    """Read-only inventory; `verified` compares each snapshot with its manifest hash."""
    return list_backups(backup_directory(db))


@router.get('/maintenance/migration')
def migration_report(db: Database_):
    """Read the latest recorded import report from the validated database only."""
    with db.read() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_runs'").fetchone():
            return {'status': 'not_run', 'note': '当前数据库没有已记录的迁移演练报告'}
        row = conn.execute('SELECT summary FROM migration_runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
        if row is None:
            return {'status': 'not_run', 'note': '当前数据库没有已记录的迁移演练报告'}
        return {'status': 'recorded', 'report': json.loads(row[0]),
                'note': '迁移时的对账证据，不代表当前估值或正式接管批准'}


@router.post('/maintenance/backups')
def manual_backup(db: Database_, request: Request):
    """Server-chosen destination, one snapshot per minute under a shared OS lock."""
    from datetime import UTC, datetime
    from backend.infrastructure.backup import create_backup
    from backend.infrastructure.restore import check_backup
    from backend.infrastructure.database import DatabaseError, require_declared_identity, writer_lock, declared_identity
    import sqlite3
    if getattr(request.state,'session',None) is None:
        raise HTTPException(401, 'authenticated backup session required')
    if declared_identity.get() is None:
        raise HTTPException(428, 'database identity required')
    if not db.settings.writable or db.settings.mode != 'server':
        raise HTTPException(403, 'manual formal backup requires writable server mode')
    try:
        with db.read():
            require_declared_identity(db.identity)
        root = backup_directory(db)
        if root.is_symlink():
            raise DatabaseError('backup root must not be a symlink')
        root.mkdir(parents=True, exist_ok=True)
        with writer_lock(root/'manual-backup'):
            stamp = datetime.now(UTC).strftime('%Y%m%dT%H%MZ')
            target = root / f'manual-{db.identity.database_uuid}-g{db.identity.generation}-{stamp}'
            if not target.exists():
                create_backup(db.settings,target)
            result=check_backup(db.settings,target)
            if not result['ok']:
                raise DatabaseError('manual backup verification failed')
            return {'ok':True,'name':target.name,'backup_at':result['manifest']['backup_at'],'verified':True}
    except (DatabaseError, OSError, sqlite3.Error) as exc:
        raise HTTPException(409, 'backup refused or failed; check server maintenance state') from exc
