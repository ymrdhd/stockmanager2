"""Verify a backup and switch the formal ledger to it, keeping the old one on failure."""

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.infrastructure.backup import backup_layout, create_backup
from backend.infrastructure.binding import Binding, publish_binding
from backend.infrastructure.database import (
    SCHEMA_VERSION,
    DatabaseError,
    DatabaseSettings,
    Identity,
    absolute_path,
    read_identity,
    validate_binding,
    validate_identity,
    writer_lock,
)
from backend.infrastructure.durability import fsync_file

# Business tables a usable ledger must expose; a missing table proves the copy is not
# this application's database, whatever the manifest claims.
BUSINESS_TABLES = ('accounts', 'instruments', 'trade_executions', 'trade_lots',
                   'cash_movements', 'finance_transactions', 'lot_allocations',
                   'balance_adjustments', 'audit_events', 'opening_positions',
                   'company_events', 'strategy_signals', 'strategy_plans')
REVISION_SOURCES = (('ledger_state', 'ledger_revision'), ('quote_state', 'quote_revision'))
ATOMIC_NOTE = (
    '数据库文件与 official.json 之间存在非原子窗口（先替换数据库，再发布绑定）：两个文件不可能同时原子切换。'
    '发布前持久化维护标记；发布中断时拒绝正式启动，使用保留的安全备份显式离线回滚。'
)
TEMP_SUFFIX = '.restore-tmp'


class RestoreError(DatabaseError):
    """A restore stage refused; the formal ledger and its binding stay untouched."""


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {'name': name, 'ok': bool(ok), 'detail': detail}


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=5,
                           isolation_level=None)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA query_only=ON')
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()
    return row is not None


def _revision(conn: sqlite3.Connection, table: str, column: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    try:
        row = conn.execute(f'SELECT {column} FROM {table}').fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row is not None and type(row[0]) is int else None


def _identity_or_none(conn: sqlite3.Connection) -> Identity | None:
    try:
        return read_identity(conn)
    except (DatabaseError, sqlite3.Error):
        return None


def check_backup(settings: DatabaseSettings, backup_path: Path) -> dict[str, Any]:
    """Read-only verification; every check is reported so an operator can see why."""
    checks: list[dict[str, Any]] = []
    counts: dict[str, int | None] = {table: None for table in BUSINESS_TABLES}
    result: dict[str, Any] = {'ok': False, 'checks': checks, 'counts': counts, 'reason': None,
                              'backup_path': str(backup_path), 'manifest': None}
    layout = backup_layout(Path(backup_path))
    if layout is None:
        checks.append(_check('layout', False, 'not a backup directory or database.sqlite file'))
        result['reason'] = 'backup path is not a published backup'
        return result
    snapshot, manifest_path = layout
    if not snapshot.is_file():
        checks.append(_check('snapshot', False, f'missing {snapshot.name}'))
        result['reason'] = 'backup snapshot missing'
        return result
    if not manifest_path.is_file():
        # A snapshot without a manifest cannot be verified, however healthy the file looks.
        checks.append(_check('manifest', False, 'missing_manifest'))
        result['reason'] = 'missing_manifest'
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError('manifest must be a JSON object')
    except (OSError, ValueError) as exc:
        checks.append(_check('manifest', False, f'unreadable: {exc}'))
        result['reason'] = 'manifest unreadable'
        return result
    result['manifest'] = manifest
    expected = manifest.get('sha256')
    try:
        actual = _digest(snapshot)
    except OSError as exc:
        checks.append(_check('sha256', False, f'snapshot unreadable: {exc}'))
        result['reason'] = 'backup snapshot unreadable'
        return result
    size = snapshot.stat().st_size
    checks.append(_check('sha256', actual == expected,
                         'matches manifest' if actual == expected
                         else f'file {actual} != manifest {expected}'))
    checks.append(_check('file_size', manifest.get('file_size') == size,
                         f'{size} bytes' if manifest.get('file_size') == size
                         else f"file {size} != manifest {manifest.get('file_size')}"))
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly(snapshot)
        identity = _identity_or_none(conn)
        checks.append(_check('identity', identity is not None,
                             'readable' if identity is not None else 'system_metadata unreadable'))
        if identity is None:
            result['reason'] = 'backup identity unreadable'
            return result
        same = (manifest.get('database_uuid') == identity.database_uuid
                and manifest.get('generation') == identity.generation
                and manifest.get('schema_version') == identity.schema_version)
        checks.append(_check('manifest_identity', same,
                             'manifest matches copy' if same else
                             f"manifest {manifest.get('database_uuid')}"
                             f"/{manifest.get('generation')}/{manifest.get('schema_version')}"
                             f' != copy {identity.database_uuid}'
                             f'/{identity.generation}/{identity.schema_version}'))
        checks.append(_check('bound_uuid', identity.database_uuid == settings.expected_uuid, 'matches expected database UUID'))
        checks.append(_check('instance_role', identity.instance_role == 'backup',
                             f'instance_role={identity.instance_role}'))
        # A backup newer than this build can never be restored by downgrading the schema.
        compatible = identity.schema_version <= SCHEMA_VERSION
        checks.append(_check('schema_version', compatible,
                             f'backup {identity.schema_version} vs target {SCHEMA_VERSION}'))
        if not compatible:
            result['reason'] = 'backup schema is newer than this build'
            return result
        integrity = conn.execute('PRAGMA integrity_check').fetchall()
        checks.append(_check('integrity_check', integrity == [('ok',)], str(integrity[:3])))
        foreign_keys = conn.execute('PRAGMA foreign_key_check').fetchall()
        checks.append(_check('foreign_key_check', foreign_keys == [], str(foreign_keys[:3])))
        for table in BUSINESS_TABLES:
            # Fixed table names from BUSINESS_TABLES; a missing table stays None and blocks restore.
            try:
                counts[table] = conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        unreadable = [table for table, value in counts.items() if value is None]
        checks.append(_check('business_data', not unreadable,
                             'counts recorded' if not unreadable else f'unreadable: {unreadable}'))
        for table, column in REVISION_SOURCES:
            live = _revision(conn, table, column)
            if identity.schema_version == SCHEMA_VERSION and (live is None or manifest.get(column) is None):
                checks.append(_check(f'manifest_{column}', False, 'revision missing'))
                continue
            if manifest.get(column) is None:
                continue
            checks.append(_check(f'manifest_{column}', live == manifest[column],
                                 f'{live}' if live == manifest[column]
                                 else f"file {live} != manifest {manifest[column]}"))
    except sqlite3.Error as exc:
        checks.append(_check('open', False, f'cannot read backup: {exc}'))
        result['reason'] = 'backup unreadable'
        return result
    finally:
        if conn is not None:
            conn.close()
    failed = [item['name'] for item in checks if not item['ok']]
    result['ok'] = not failed
    result['reason'] = None if not failed else 'failed checks: ' + ', '.join(failed)
    return result


def _lockable(file: Path) -> bool:
    """True when this process can take an exclusive OS lock on an existing file."""
    try:
        fd = os.open(file, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return False
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
        elif os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
        return True
    finally:
        os.close(fd)


def live_writer(official: Path) -> str | None:
    """Name the evidence that a writer is still running, or None when a switch is safe.

    Three independent proofs are tried: the application writer lock, a WAL sidecar that
    cannot be locked exclusively, and a non-blocking immediate transaction on the ledger.
    Deleting any of those sidecars to "free" the file is exactly what must not happen.
    """
    if not official.is_file():
        return None
    lock_path = official.with_name(official.name + '.writer.lock')
    if lock_path.exists() and not _lockable(lock_path):
        return 'writer lock is held by another writer'
    wal = Path(str(official) + '-wal')
    if wal.exists() and not _lockable(wal):
        return 'WAL sidecar is in use by another connection'
    try:
        with writer_lock(official):
            probing = sqlite3.connect(f'{official.as_uri()}?mode=rw', uri=True,
                                      timeout=0.2, isolation_level=None)
            try:
                probing.execute('PRAGMA foreign_keys=ON')
                probing.execute('BEGIN IMMEDIATE')
                probing.execute('ROLLBACK')
            finally:
                probing.close()
    except DatabaseError as exc:
        return str(exc)
    except sqlite3.Error as exc:
        return f'formal database is still being written: {exc}'
    return None


def _current_identity(official: Path) -> Identity:
    with closing(_open_readonly(official)) as conn:
        identity = _identity_or_none(conn)
    if identity is None:
        raise RestoreError('current formal database identity is unreadable')
    return identity


def _swap(staged: Path, official: Path) -> None:
    """The single publication point for the database file.

    Kept as its own function so a test can inject a failure at exactly this step and
    prove that the original database and binding survive it.
    """
    os.replace(staged, official)


def _stamp() -> str:
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')


@contextmanager
def _staged_copy(source: Path, official: Path) -> Iterator[Path]:
    """Copy the snapshot to a sibling `*.restore-tmp` file before any publication."""
    staged = official.with_name(official.name + TEMP_SUFFIX)
    # A leftover from an interrupted run is never a ledger; it is replaced, not trusted.
    staged.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, staged)
        yield staged
    finally:
        staged.unlink(missing_ok=True)


def _promote(path: Path, *, uuid: str, generation: int) -> Identity:
    """Promote the staged copy to the formal writer identity in one transaction."""
    conn = sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, timeout=5,
                           isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute("UPDATE system_metadata SET instance_role='formal', generation=?",
                         (generation,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        identity = read_identity(conn)
        if (identity.instance_role != 'formal' or identity.generation != generation
                or identity.database_uuid != uuid):
            raise RestoreError('staged backup identity update did not persist')
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RestoreError('staged backup failed integrity check')
        if conn.execute('PRAGMA foreign_key_check').fetchall() != []:
            raise RestoreError('staged backup failed foreign key check')
        return identity
    finally:
        conn.close()


def restore(settings: DatabaseSettings, backup_path: Path, *, confirm: bool,
            destination_report: Path | None = None) -> dict[str, Any]:
    """Replace the formal ledger with a verified backup; refuse rather than half-switch."""
    # Reuse the takeover maintenance fence and its explicit offline rollback.
    from backend.infrastructure.takeover import marker_path, _write_json, _sync_dir, _quiet
    official = settings.official_path
    marker = marker_path(settings.binding_path)
    report: dict[str, Any] = {
        'ok': False, 'backup_path': str(absolute_path(Path(backup_path))),
        'target_path': str(official), 'previous_generation': None, 'new_generation': None,
        'pre_restore_backup': None, 'checks': [], 'atomic_note': ATOMIC_NOTE, 'reason': None,
    }

    if destination_report is not None:
        report_target = absolute_path(Path(destination_report))
        if report_target.exists() or report_target.suffix.lower() != '.json' or report_target == settings.binding_path:
            raise RestoreError('restore report must be a new JSON file, separate from database and binding')
    def write_report() -> dict[str, Any]:
        if destination_report is not None:
            target = absolute_path(Path(destination_report))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x', encoding='utf-8') as output:
                output.write(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        return report

    def refuse(reason: str) -> None:
        report['ok'] = False
        report['reason'] = reason
        write_report()
        raise RestoreError(reason)

    if not confirm:
        refuse('restore requires explicit confirmation (--confirm)')
    if settings.mode != 'server':
        refuse('restore is only allowed for the formal server ledger')
    verdict = check_backup(settings, Path(backup_path))
    report['checks'] = verdict['checks']
    if not verdict['ok']:
        refuse(f'backup verification failed: {verdict["reason"]}')
    layout = backup_layout(Path(backup_path))
    if layout is None:  # cannot happen after a passing check; keeps the type honest
        refuse('backup path is not a published backup')
    snapshot = layout[0]
    manifest = verdict['manifest'] or {}
    recorded = (manifest.get('database_uuid'), manifest.get('generation'),
                manifest.get('schema_version'))
    if recorded != (recorded[0], recorded[1], SCHEMA_VERSION):
        # A copy of another schema version would be refused by open_database after the
        # switch, so the restore stops here instead of leaving an unstartable ledger.
        refuse(f'backup schema {recorded[2]} != this build schema {SCHEMA_VERSION}: '
               'upgrade a copy of the backup with upgrade-database first, then restore that copy')
    # Hold the installation and the canonical writer locks until publication AND verification.
    # A probe that releases its lock before the swap has a TOCTOU race with server startup.
    with ExitStack() as guards:
        try:
            guards.enter_context(writer_lock(settings.binding_path))
            guards.enter_context(writer_lock(official))
        except DatabaseError as exc:
            refuse(f'a formal writer is still active ({exc}); stop the writing service first')
        try:
            validate_binding(settings)
            with closing(_open_readonly(official)) as conn:
                current = validate_identity(conn, settings)
            if (recorded[0] != current.database_uuid or
                    manifest.get('source_instance_role') != 'formal'):
                refuse('backup does not belong to the bound formal ledger')
            # Cooperating writers are excluded by the OS lock. Refuse any remaining
            # independent SQLite connection/transaction; do not move or delete sidecars.
            with closing(sqlite3.connect(f'{official.as_uri()}?mode=rw', uri=True,
                                         timeout=0.2, isolation_level=None)) as probe:
                probe.execute('BEGIN IMMEDIATE')
                probe.execute('ROLLBACK')
            if any(Path(str(official) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                refuse('formal database sidecars remain; close every connection before restore')
        except RestoreError:
            raise
        except (DatabaseError, OSError, sqlite3.Error) as exc:
            refuse(f'formal database is still being written or unavailable: {exc}')
        report['previous_generation'] = current.generation
        new_generation = max(current.generation, int(recorded[1])) + 1
        destination = official.parent / 'backups' / ('pre-restore-' + _stamp())
        try:
            report['pre_restore_backup'] = str(create_backup(settings, destination))
        except (DatabaseError, OSError, sqlite3.Error) as exc:
            refuse(f'pre-restore backup of the current ledger failed: {exc}')
        if not check_backup(settings, destination)['ok']:
            refuse('pre-restore backup verification failed')
        # A read-only backup of a WAL database may leave empty WAL/SHM sidecars.
        # Let SQLite close them under the existing locks; never unlink sidecars.
        try:
            _quiet(official)
        except (DatabaseError, OSError, sqlite3.Error) as exc:
            refuse(f'formal database not quiescent after backup: {exc}')
        uuid = current.database_uuid
        # Retain an exact rollback copy, not a backup-role file, across both publications.
        # Cross-file replacement is not atomic: a power loss must fail identity checks.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=official.parent) as rollback_dir:
            original = Path(rollback_dir) / 'original.sqlite'
            original_binding = Path(rollback_dir) / 'official.json'
            shutil.copyfile(official, original)
            shutil.copyfile(settings.binding_path, original_binding)
            temporary = settings.binding_path.with_name(f'.binding-restore-{os.getpid()}.json')
            switched = binding_switched = False
            try:
                with _staged_copy(snapshot, official) as staged:
                    if _digest(staged) != manifest.get('sha256'):
                        raise RestoreError('backup changed after verification')
                    identity = _promote(staged, uuid=uuid, generation=new_generation)
                    report['new_generation'] = identity.generation
                    # Prepare binding before publishing the database.
                    publish_binding(temporary, Binding(official, uuid, new_generation))
                    fsync_file(staged)
                    _write_json(marker, {'path': str(official), 'uuid': uuid,
                        'old_generation': current.generation, 'new_generation': new_generation,
                        'backup': str(destination), 'staged': str(staged), 'operation': 'restore'})
                    _swap(staged, official)
                    switched = True
                    _sync_dir(official.parent)
                os.replace(temporary, settings.binding_path)
                binding_switched = True
                _sync_dir(settings.binding_path.parent)
                restored = DatabaseSettings(path=official, official_path=official, expected_uuid=uuid,
                                           generation=new_generation, binding_path=settings.binding_path)
                from backend.infrastructure.binding import load_binding
                if load_binding(settings.binding_path) != Binding(official, uuid, new_generation):
                    raise RestoreError('published binding mismatch')
                with closing(_open_readonly(official)) as conn:
                    validate_identity(conn, restored)
                    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or conn.execute('PRAGMA foreign_key_check').fetchall():
                        raise RestoreError('published database verification failed')
                marker.unlink()
                _sync_dir(marker.parent)
            except Exception as exc:
                if switched or binding_switched or (marker.exists() and _digest(official) != _digest(original)):
                    refuse(f'restore publication interrupted: {exc}; use takeover-rollback with the retained pre-restore backup {destination}')
                # Before publication it is safe to clear the fence only if both files
                # are still the original bytes. Otherwise leave explicit recovery.
                if marker.exists():
                    if _digest(official) != _digest(original) or _digest(settings.binding_path) != _digest(original_binding):
                        refuse('restore state uncertain; explicit offline rollback required')
                    marker.unlink()
                    _sync_dir(marker.parent)
                refuse('restore aborted before switching the formal ledger: ' + str(exc))
            finally:
                temporary.unlink(missing_ok=True)
    report['ok'] = True
    report['new_generation'] = new_generation
    return write_report()
