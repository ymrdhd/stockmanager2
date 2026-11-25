"""Offline migration publication. A durable marker fences all formal access until
both publications are verified, or an explicit offline rollback succeeds."""
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing, ExitStack
from pathlib import Path
from uuid import uuid4

from backend.infrastructure.backup import create_backup
from backend.infrastructure.binding import Binding, load_binding, publish_binding
from backend.infrastructure.database import (DatabaseError, DatabaseSettings, _single_file,
    read_identity, validate_binding, validate_identity, writer_lock)
from backend.infrastructure.restore import check_backup


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def marker_path(binding):
    return binding.with_name(binding.name + '.takeover-pending')


def _sync_dir(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_json(path, data):
    tmp = path.with_name('.marker-'+uuid4().hex+'.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
        _sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _quiet(path):
    _single_file(path)
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True,
                                 timeout=.2, isolation_level=None)) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        conn.execute('ROLLBACK')
    if any(Path(str(path)+s).exists() for s in ('-wal', '-shm', '-journal')):
        raise DatabaseError('SQLite sidecars remain; stop every connection')


def _verified(path):
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as conn:
        identity = read_identity(conn)
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise DatabaseError('database integrity or foreign keys failed')
        return identity


def _approved_report(target, report_file):
    from backend.application.migration import MIGRATION_PROTOCOL, open_source, SourceError
    report = json.loads(report_file.read_text(encoding='utf-8'))
    identity = _verified(target)
    if identity.instance_role != 'sandbox':
        raise DatabaseError('only an isolated sandbox may be promoted')
    with closing(sqlite3.connect(f'{target.as_uri()}?mode=ro', uri=True)) as conn:
        run = conn.execute('SELECT id,status,summary,stock_sha256,finance_sha256,stock_path,finance_path FROM migration_runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
        if not run or run[1] != 'completed' or json.loads(run[2]) != report:
            raise DatabaseError('report must exactly match the latest completed stored report')
        if conn.execute("SELECT 1 FROM migration_runs WHERE status!='completed'").fetchone():
            raise DatabaseError('incomplete migration run')
        if (run[3],run[4],run[5],run[6]) != tuple(report['source'].get(k) for k in ('stock_sha256','finance_sha256','stock_path','finance_path')):
            raise DatabaseError('persisted source identity differs from report')
        projection = report.get('formal_projection', {})
        if (projection.get('ledger_revision'), projection.get('quote_revision')) != (
                conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0],
                conn.execute('SELECT quote_revision FROM quote_state').fetchone()[0]):
            raise DatabaseError('migration projection is stale')
    if (report.get('blocks_formal_takeover') is not False or report.get('dry_run') is not False
            or report.get('migration_protocol') != MIGRATION_PROTOCOL or report.get('run_id') != run[0]
            or Path(report.get('target','')).resolve() != target
            or report.get('target_identity') != {'uuid': identity.database_uuid, 'generation': identity.generation,
                                                 'schema_version': identity.schema_version}
            or report.get('migration_tool_errors') or report.get('stock', {}).get('pending_trades_detail')
            or any(d.get('blocks_takeover') is not False for d in report.get('differences', []))
            or projection.get('source_valuation_at') is None
            or projection.get('source_valuation_status') != 'operator_statement_matched'
            or not report.get('reconciliation',{}).get('evidence_sha256')):
        raise DatabaseError('unresolved or unverified migration report')
    for kind in ('stock', 'finance'):
        source = report['source'].get(kind+'_path')
        sha = report['source'].get(kind+'_sha256')
        if source is None and sha is None and kind == 'finance':
            continue
        if not source or not sha:
            raise DatabaseError('source identity missing')
        path = Path(source).resolve()
        try:
            with open_source(path):
                if digest(path) != sha:
                    raise DatabaseError('source changed since migration')
        except SourceError as exc:
            raise DatabaseError(str(exc)) from exc
    return identity


def _token(target, report, settings):
    return hashlib.sha256(json.dumps([digest(target), digest(report), str(settings.path),
        settings.expected_uuid, settings.generation, digest(settings.path)], separators=(',', ':')).encode()).hexdigest()


def _publish_db(source, destination):
    temp = destination.with_name('.takeover-'+uuid4().hex+'.sqlite')
    try:
        shutil.copyfile(source, temp)
        with temp.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(temp, destination)
        _sync_dir(destination.parent)
    finally:
        temp.unlink(missing_ok=True)


def _publish_binding(path, binding):
    temp = path.with_name('.takeover-'+uuid4().hex+'.json')
    try:
        publish_binding(temp, binding)
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def takeover(settings: DatabaseSettings, target: Path, report: Path, *, approval=None,
             isolated=False):
    """Without approval return the exact review token; with approval switch offline.
    `isolated` is operator attestation, not proof of remote machine shutdown."""
    target, report = target.resolve(), report.resolve()
    if settings.mode != 'server' or target == settings.path or target.samefile(settings.path):
        raise DatabaseError('distinct formal and sandbox files required')
    if approval is not None and not isolated:
        raise DatabaseError('explicit writer isolation confirmation required')
    marker = marker_path(settings.binding_path)
    with ExitStack() as stack:
        for path in (settings.binding_path, settings.path, target):
            stack.enter_context(writer_lock(path))
        validate_binding(settings)
        _quiet(settings.path)
        _quiet(target)
        with closing(sqlite3.connect(f'{settings.path.as_uri()}?mode=ro', uri=True)) as conn:
            validate_identity(conn, settings)
        identity = _approved_report(target, report)
        token = _token(target, report, settings)
        if approval is None:
            return {'approval': token, 'target_uuid': identity.database_uuid, 'formal_uuid': settings.expected_uuid,
                    'formal_generation': settings.generation, 'target_sha256': digest(target), 'report_sha256': digest(report)}
        if approval != token:
            raise DatabaseError('approval does not match current files and binding')
        recovery = settings.path.parent / 'backups' / ('pre-takeover-'+uuid4().hex)
        create_backup(settings, recovery)
        if not check_backup(settings, recovery)['ok']:
            raise DatabaseError('pre-takeover backup verification failed')
        # A private staging directory survives process death. The marker is removed only
        # after successful verification; a crash even after binding publication is fenced.
        staged = recovery.parent / (recovery.name+'-staged.sqlite')
        shutil.copyfile(target, staged)
        if _token(target, report, settings) != token or digest(staged) != digest(target):
            raise DatabaseError('files changed during preparation')
        generation = max(settings.generation, identity.generation)+1
        with closing(sqlite3.connect(staged)) as conn:
            conn.execute("UPDATE system_metadata SET database_uuid=?,generation=?,instance_role='formal'",
                         (settings.expected_uuid, generation))
            conn.commit()
        _verified(staged)
        _approved_report(target, report)
        _write_json(marker, {'path': str(settings.path), 'uuid': settings.expected_uuid,
            'old_generation': settings.generation, 'new_generation': generation,
            'backup': str(recovery), 'approval': token, 'staged': str(staged)})
        _publish_db(staged, settings.path)
        _publish_binding(settings.binding_path, Binding(settings.path, settings.expected_uuid, generation))
        promoted = _verified(settings.path)
        if (promoted.database_uuid, promoted.generation, promoted.instance_role) != (settings.expected_uuid,generation,'formal') or load_binding(settings.binding_path) != Binding(settings.path, settings.expected_uuid, generation):
            raise DatabaseError('publication verification failed; offline rollback required')
        marker.unlink()
        _sync_dir(marker.parent)
        staged.unlink(missing_ok=True)
        return {'ok': True, 'generation': generation, 'pre_takeover_backup': str(recovery), 'approval': token}


def rollback_pending(binding_path: Path, *, confirm=False, isolated=False):
    if not confirm or not isolated:
        raise DatabaseError('rollback requires confirmation and writer isolation')
    marker = marker_path(binding_path)
    with writer_lock(binding_path):
        record = json.loads(marker.read_text(encoding='utf-8'))
        official = Path(record['path'])
        with writer_lock(official):
            _quiet(official)
            bound = load_binding(binding_path)
            if bound.path != official or bound.uuid != record['uuid'] or not record['old_generation'] <= bound.generation <= record['new_generation']+1:
                raise DatabaseError('binding changed; manual offline recovery required')
            settings = DatabaseSettings(official, record['uuid'], record['old_generation'], official, binding_path=binding_path)
            backup = Path(record['backup'])
            if not check_backup(settings, backup)['ok']:
                raise DatabaseError('rollback backup invalid')
            staged = official.parent / ('.rollback-'+uuid4().hex+'.sqlite')
            shutil.copyfile(backup/'database.sqlite', staged)
            generation = record['new_generation']+1
            try:
                with closing(sqlite3.connect(staged)) as conn:
                    conn.execute("UPDATE system_metadata SET instance_role='formal',generation=?", (generation,))
                    conn.commit()
                _verified(staged)
                _publish_db(staged, official)
                _publish_binding(binding_path, Binding(official, record['uuid'], generation))
                if _verified(official).generation != generation or load_binding(binding_path).generation != generation:
                    raise DatabaseError('rollback verification failed')
                marker.unlink()
                _sync_dir(marker.parent)
                Path(record['staged']).unlink(missing_ok=True)
            finally:
                staged.unlink(missing_ok=True)
            return {'ok': True, 'generation': generation, 'restored_backup': str(backup)}
