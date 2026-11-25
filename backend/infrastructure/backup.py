"""Consistent online SQLite snapshots, never overwriting an existing backup."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from backend.infrastructure.database import (
    DatabaseError,
    DatabaseSettings,
    _connect,
    absolute_path,
    read_identity,
    validate_binding,
    validate_identity,
)

SNAPSHOT_NAME = 'database.sqlite'
MANIFEST_NAME = 'manifest.json'
# Recognised revision tables are read from the copy, never from the live writer.
REVISION_SOURCES = (('ledger_state', 'ledger_revision'), ('quote_state', 'quote_revision'))
SENSITIVE_NOTICE = (
    '本备份包含个人账户、成交、批次、资金与账单等财务数据，仅限本人在做隔离存放与恢复演练时使用；'
    '备份目录只含数据库快照与 manifest，不含登录密码、会话令牌或 CSRF 凭据，请与凭据文件分开保管。'
)


def _revision(conn: sqlite3.Connection, table: str, column: str) -> int | None:
    """Read one revision counter from the snapshot; identifiers are fixed constants."""
    try:
        row = conn.execute(f'SELECT {column} FROM {table}').fetchone()
    except sqlite3.Error:
        return None
    if row is None or type(row[0]) is not int:
        return None
    return row[0]


def create_backup(settings: DatabaseSettings, destination: Path, *, allow_previous: bool = False) -> Path:
    """Publish a new directory containing database.sqlite and manifest.json."""
    validate_binding(settings)
    target = absolute_path(destination)
    if target.exists() or target == settings.path or target == settings.official_path:
        raise DatabaseError('backup destination must be a new independent directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.backup-', dir=target.parent))
    try:
        snapshot = staging / SNAPSHOT_NAME
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
                revisions = {column: _revision(copy, table, column)
                             for table, column in REVISION_SOURCES}
        missing = [column for column, value in revisions.items() if value is None]
        with snapshot.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        manifest = {**asdict(identity), 'backup_at': datetime.now(UTC).isoformat(),
                    'source_instance_role': source_identity.instance_role,
                    # Recorded so a restore report can name the ledger the snapshot came from.
                    'source_path_basename': settings.path.name,
                    'file_size': snapshot.stat().st_size,
                    'sha256': digest, 'integrity_check': 'ok', 'foreign_key_check': 'ok',
                    'ledger_revision': revisions['ledger_revision'],
                    'quote_revision': revisions['quote_revision'],
                    'missing': missing,
                    'contains_personal_financial_data': True,
                    'sensitive_notice': SENSITIVE_NOTICE}
        # Default ensure_ascii keeps the manifest readable on any console/locale while the
        # JSON semantics still carry the Chinese notice.
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        # Credentials and sessions live in data/ next to the ledger; a backup must never
        # widen into a directory copy. Only these two files may exist before publication.
        if sorted(path.name for path in staging.iterdir()) != sorted([MANIFEST_NAME, SNAPSHOT_NAME]):
            raise DatabaseError('backup staging must contain exactly the snapshot and manifest')
        # A valid backup directory is nonempty: rename cannot replace it.
        if target.exists():
            raise DatabaseError('backup destination already exists')
        staging.rename(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def backup_layout(backup: Path) -> tuple[Path, Path] | None:
    """Accept either the backup directory or its database.sqlite file."""
    if backup.is_dir() and not backup.is_symlink():
        return backup / SNAPSHOT_NAME, backup / MANIFEST_NAME
    if backup.is_file() and backup.name == SNAPSHOT_NAME:
        return backup, backup.with_name(MANIFEST_NAME)
    return None


def list_backups(directory: Path) -> dict:
    """Read-only inventory of published backups; unknown content is reported, not trusted."""
    path = absolute_path(directory)
    if not path.is_dir():
        return {'items': [], 'directory': str(path),
                'note': '备份目录尚不存在；正式服务创建首个备份后才会出现。'}
    items = []
    problems: list[str] = []
    for entry in sorted(path.iterdir(), key=lambda item: item.name):
        layout = backup_layout(entry)
        if layout is None:
            continue
        snapshot, manifest_path = layout
        manifest: dict = {}
        note = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if not isinstance(manifest, dict):
                raise ValueError('manifest must be a JSON object')
        except (OSError, ValueError) as exc:
            manifest, note = {}, f'manifest unreadable: {exc}'
        verified = False
        if not note and snapshot.is_file():
            try:
                with snapshot.open('rb') as stream:
                    verified = hashlib.file_digest(stream, 'sha256').hexdigest() == manifest.get('sha256')
            except OSError as exc:
                note = f'snapshot unreadable: {exc}'
        if note is not None:
            problems.append(f'{entry.name}: {note}')
        items.append({
            'name': entry.name,
            'path_basename': entry.name,
            'backup_at': manifest.get('backup_at'),
            'generation': manifest.get('generation'),
            'schema_version': manifest.get('schema_version'),
            'ledger_revision': manifest.get('ledger_revision'),
            'quote_revision': manifest.get('quote_revision'),
            'sha256': manifest.get('sha256'),
            'verified': verified,
        })
    if not items:
        note = '该目录下没有已发布的备份（缺少 manifest.json 的条目已跳过）。'
    elif problems:
        note = '；'.join(problems)
    else:
        note = None
    return {'items': items, 'directory': str(path), 'note': note}


def daily_backup(settings: DatabaseSettings, directory: Path, *, now=None) -> dict:
    """One verified automatic snapshot per UTC day; retain 7 daily + 4 weekly.

    Manual, restore and takeover backups, other identities and corrupt snapshots
    are never removed by this policy. A failed new backup never triggers pruning.
    """
    from datetime import date, timedelta
    from backend.infrastructure.database import writer_lock
    from backend.infrastructure.restore import check_backup
    import re
    if settings.mode != 'server':
        raise DatabaseError('automatic backup requires bound formal ledger')
    day = (now or datetime.now(UTC)).astimezone(UTC).date()
    root = absolute_path(directory)
    root.mkdir(parents=True, exist_ok=True)
    prefix = f'auto-{settings.expected_uuid}-g{settings.generation}-'
    target = root / (prefix+day.isoformat())
    with writer_lock(settings.binding_path), writer_lock(root/'auto-retention'):
        validate_binding(settings)
        if not target.exists():
            create_backup(settings,target)
        if not check_backup(settings,target)['ok']:
            raise DatabaseError('daily snapshot invalid; retention refused')
        verified = []
        for path in root.iterdir():
            if not path.name.startswith(prefix) or path.is_symlink() or not path.is_dir():
                continue
            suffix = path.name[len(prefix):]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',suffix):
                continue
            try:
                when = date.fromisoformat(suffix)
            except ValueError:
                continue
            if when > day or {p.name for p in path.iterdir()} != {SNAPSHOT_NAME,MANIFEST_NAME}:
                continue
            if any(p.is_symlink() for p in path.iterdir()) or not check_backup(settings,path)['ok']:
                continue
            verified.append((when,path))
        keep = {p for d,p in verified if d >= day-timedelta(days=6)}
        weeks = set()
        for d,p in sorted(verified,reverse=True):
            week = d-timedelta(days=d.weekday())
            if p not in keep and week not in weeks and len(weeks)<4:
                keep.add(p); weeks.add(week)
        removed = []
        for _,p in verified:
            if p not in keep:
                shutil.rmtree(p)
                removed.append(p.name)
        return {'ok':True,'snapshot':str(target),'removed':removed,'kept':len(keep)}
