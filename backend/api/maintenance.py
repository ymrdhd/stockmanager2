"""Read-only maintenance inventory; backup creation and restore use the CLI."""

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
