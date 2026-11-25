"""Acceptance tests use only pytest temporary databases."""

import json
import os
import socket
import shutil
import time
import urllib.request
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.config import AppConfig
from backend.infrastructure.database import (
    DatabaseError,
    DatabaseSettings,
    Identity,
    REVISION,
    SCHEMA_VERSION,
    initialize_database,
    open_database,
    migrate_schema,
)
from backend.infrastructure.backup import create_backup
from backend.infrastructure.binding import Binding, publish_binding
from backend.main import create_app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def formal(tmp_path: Path) -> tuple[Path, Identity]:
    path = tmp_path / 'official.sqlite'
    return path, initialize_database(path, role='formal', confirm=True, binding_path=path.parent / 'official.json')


def settings(path: Path, identity: Identity) -> DatabaseSettings:
    return DatabaseSettings(path=path, official_path=path, expected_uuid=identity.database_uuid,
                            generation=identity.generation, binding_path=path.parent / 'official.json')


def test_db01_init(formal: tuple[Path, Identity]) -> None:
    path, identity = formal
    with open_database(settings(path, identity)) as db, db.read() as conn:
        assert db.identity == identity
        assert conn.execute('PRAGMA journal_mode').fetchone() == ('wal',)
        assert conn.execute('PRAGMA foreign_keys').fetchone() == (1,)
        assert conn.execute('PRAGMA busy_timeout').fetchone() == (5000,)
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone() == (REVISION,)
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {'system_metadata', 'alembic_version', 'accounts', 'instruments', 'trade_executions', 'trade_lots', 'lot_allocations', 'cash_movements', 'audit_events', 'ledger_state', 'ledger_requests', 'opening_positions', 'company_events', 'quotes', 'quote_state', 'quote_resolutions', 'finance_categories', 'finance_transactions', 'finance_transfers', 'balance_adjustments', 'account_scope_events', 'transfer_reconciliation_gaps', 'strategy_bars', 'strategy_signals', 'strategy_plans', 'strategy_positions', 'strategy_cooldowns', 'strategy_ndx_aux'}


def test_db02_no_overwrite(formal: tuple[Path, Identity], tmp_path: Path) -> None:
    path, _ = formal
    original = path.read_bytes()
    with pytest.raises(DatabaseError):
        initialize_database(path, role='formal', confirm=True, binding_path=path.parent / 'official.json')
    # A fresh installation with no binding must also refuse an existing file.
    with pytest.raises(DatabaseError, match='already exists'):
        initialize_database(path, role='formal', confirm=True, binding_path=tmp_path / 'unbound' / 'official.json')
    assert path.read_bytes() == original
    unconfirmed = tmp_path / 'unconfirmed.sqlite'
    with pytest.raises(DatabaseError):
        initialize_database(unconfirmed, role='formal', confirm=False, binding_path=tmp_path / 'official.json')
    assert not unconfirmed.exists()


def test_i01_missing_refuses_asgi(tmp_path: Path) -> None:
    path = tmp_path / 'missing.sqlite'
    config = DatabaseSettings(path=path, official_path=path, expected_uuid=str(uuid4()), generation=1, binding_path=tmp_path / 'official.json')
    publish_binding(config.binding_path, Binding(path, config.expected_uuid, 1))
    with pytest.raises(DatabaseError, match='existing regular file'), TestClient(create_app(AppConfig(), database=config)):
        pytest.fail('service started without database')
    assert not path.exists()


@pytest.mark.parametrize('field,value', [('database_uuid', str(uuid4())), ('instance_role', 'sandbox'), ('schema_version', 99), ('generation', 2)])
def test_i02_mismatch_refuses_asgi(formal: tuple[Path, Identity], field: str, value: object) -> None:
    path, identity = formal
    with closing(sqlite3.connect(path)) as conn, conn:
        # Fixed parametrized column names, not user input.
        conn.execute(f'UPDATE system_metadata SET {field}=?', (value,))
    with pytest.raises(DatabaseError), TestClient(create_app(database=settings(path, identity))):
        pytest.fail('invalid identity accepted')


def test_i02_revision_mismatch(formal: tuple[Path, Identity]) -> None:
    path, identity = formal
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num='unknown'")
    with pytest.raises(DatabaseError), open_database(settings(path, identity)):
        pytest.fail('unknown migration revision accepted')


WORKER = '''
import sys
from pathlib import Path
from backend.infrastructure.database import DatabaseSettings, open_database
p, official, uuid = sys.argv[1:4]
with open_database(DatabaseSettings(path=Path(p), official_path=Path(official), expected_uuid=uuid, generation=1, binding_path=Path(official).parent / 'official.json')):
    print('LOCKED', flush=True)
    sys.stdin.readline()
'''


def worker(path: Path, official: Path, identity: Identity) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, '-c', WORKER, str(path), str(official), identity.database_uuid], cwd=ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize('crash', [False, True])
def test_i04_db03_process_lock(formal: tuple[Path, Identity], tmp_path: Path, crash: bool) -> None:
    path, identity = formal
    alias = tmp_path / 'alias.sqlite'
    alias.symlink_to(path)
    first = worker(path, path, identity)
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == 'LOCKED'
        second = worker(alias, path, identity)
        out, err = second.communicate(timeout=10)
        assert second.returncode != 0 and 'writer lock' in err and 'LOCKED' not in out
        if crash:
            first.kill()
        else:
            assert first.stdin is not None
            first.stdin.write('\n')
            first.stdin.flush()
        first.communicate(timeout=10)
        assert first.returncode != 0 if crash else first.returncode == 0
        with open_database(settings(path, identity)):
            pass
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)


def test_db04_readonly_and_db05_sandbox(formal: tuple[Path, Identity], tmp_path: Path) -> None:
    path, identity = formal
    sandbox = tmp_path / 'sandbox.sqlite'
    sid = initialize_database(sandbox, role='sandbox', confirm=True, official_path=path, binding_path=tmp_path / 'official.json')
    local = DatabaseSettings(path=sandbox, official_path=path, expected_uuid=sid.database_uuid, generation=1, binding_path=path.parent / 'official.json', mode='local')
    with open_database(local) as db, db.read() as conn:
        conn.execute('PRAGMA query_only=OFF')  # even disabling pragma cannot bypass mode=ro
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('CREATE TABLE forbidden(x)')
        with pytest.raises(DatabaseError), db.transaction():
            pytest.fail('read-only transaction granted')
    with open_database(DatabaseSettings(path=sandbox, official_path=path, expected_uuid=sid.database_uuid,
                                        generation=1, binding_path=path.parent / 'official.json', mode='local', writable_sandbox=True)) as db, db.transaction() as conn:
        conn.execute('CREATE TABLE sample(x)')
    for alias in (path, tmp_path / 'link.sqlite', tmp_path / 'hard.sqlite'):
        if alias.name == 'link.sqlite':
            alias.symlink_to(path)
        if alias.name == 'hard.sqlite':
            os.link(path, alias)
        with pytest.raises(DatabaseError), open_database(DatabaseSettings(path=alias, official_path=path,
                expected_uuid=identity.database_uuid, generation=1, binding_path=path.parent / 'official.json', mode='local', writable_sandbox=True)):
            pytest.fail('formal alias accepted as sandbox')
    with pytest.raises(DatabaseError):
        DatabaseSettings(path=Path('relative.sqlite'), official_path=path, expected_uuid=identity.database_uuid, generation=1)


def test_db06_rollback(formal: tuple[Path, Identity]) -> None:
    path, identity = formal
    with open_database(settings(path, identity)) as db:
        with db.transaction() as conn:
            conn.execute('CREATE TABLE sample(x INTEGER UNIQUE)')
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute('INSERT INTO sample VALUES (1)')
            conn.execute('INSERT INTO sample VALUES (1)')
        with db.read() as conn:
            assert conn.execute('SELECT * FROM sample').fetchall() == []


def test_db07_migration_rollback(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL

    path = tmp_path / 'migration.sqlite'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE original(x)')
        conn.execute('INSERT INTO original VALUES (42)')
    engine = create_engine(URL.create('sqlite', database=str(path)))

    @event.listens_for(engine, 'after_cursor_execute')
    def fail_after_ddl(conn, cursor, statement, parameters, context, executemany):
        if 'CREATE TABLE system_metadata' in statement:
            raise RuntimeError('injected migration failure after DDL')

    try:
        with pytest.raises(RuntimeError, match='injected migration failure'):
            migrate_schema(engine, Identity.new('formal'))
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute('SELECT * FROM original').fetchall() == [(42,)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('original',)]


def test_i06_online_backup(formal: tuple[Path, Identity], tmp_path: Path) -> None:
    import hashlib
    path, identity = formal
    cfg = settings(path, identity)
    target = tmp_path / 'backup'
    with open_database(cfg) as db:
        with db.transaction() as conn:
            conn.execute('CREATE TABLE sample(x)')
            conn.execute('INSERT INTO sample VALUES (42)')
        with db.transaction() as conn:
            conn.execute('INSERT INTO sample VALUES (99)')  # uncommitted must not enter backup
            create_backup(cfg, target)
        with pytest.raises(DatabaseError):
            create_backup(cfg, target)
        with pytest.raises(DatabaseError):
            create_backup(cfg, path)
    manifest = json.loads((target / 'manifest.json').read_text())
    snapshot = target / 'database.sqlite'
    assert manifest['sha256'] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert manifest['database_uuid'] == identity.database_uuid and manifest['generation'] == 1
    assert manifest['schema_version'] == SCHEMA_VERSION and manifest['backup_at']
    assert manifest['instance_role'] == 'backup' and manifest['source_instance_role'] == 'formal'
    with pytest.raises(DatabaseError), open_database(DatabaseSettings(path=snapshot, official_path=snapshot, expected_uuid=identity.database_uuid, generation=1, binding_path=path.parent / 'official.json')):
        pytest.fail('backup accepted as formal writer')
    local = DatabaseSettings(path=snapshot, official_path=path, expected_uuid=identity.database_uuid,
                             generation=1, binding_path=path.parent / 'official.json', mode='local')
    with open_database(local) as db, db.read() as conn:
        assert db.identity.instance_role == 'backup'
        assert conn.execute('SELECT * FROM sample').fetchall() == [(42,)]
    with closing(sqlite3.connect(snapshot)) as conn, conn:
        assert conn.execute('SELECT * FROM sample').fetchall() == [(42,)]
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def isolated_install(root: Path) -> Path:
    install = root / 'installation'
    install.mkdir()
    for directory in ('backend', 'migrations'):
        shutil.copytree(ROOT / directory, install / directory, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'start.py', install / 'start.py')
    return install


def test_db08_cli_cwd_and_client(tmp_path: Path) -> None:
    path = tmp_path / 'explicit.sqlite'
    install = isolated_install(tmp_path)
    cmd = [sys.executable, str(install / 'start.py')]
    result = subprocess.run(cmd + ['init-database', '--database', str(path), '--confirm'], cwd=tmp_path, capture_output=True, text=True, check=True)
    identity = json.loads(result.stdout)
    other = tmp_path / 'other'
    other.mkdir()
    # Inspect the default without ever opening or creating that real path.
    probe = [sys.executable, '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
             'from backend.infrastructure.database import DEFAULT_DATABASE; print(DEFAULT_DATABASE)']
    for cwd in (tmp_path, other):
        assert subprocess.check_output(probe, cwd=cwd, text=True).strip() == str(ROOT / 'data' / 'stock_manager.sqlite')
    args = ['check-database', '--database', str(path), '--uuid', identity['database_uuid'], '--generation', '1']
    subprocess.run(cmd + args, cwd=other, capture_output=True, check=True)
    assert list(other.iterdir()) == []
    result = subprocess.run(cmd + ['client'], cwd=other, capture_output=True)
    assert result.returncode != 0 and list(other.iterdir()) == []


def test_i06_failed_backup_preserves_existing(formal: tuple[Path, Identity], tmp_path: Path) -> None:
    path, identity = formal
    cfg = settings(path, identity)
    good = create_backup(cfg, tmp_path / 'good')
    before = {file.name: file.read_bytes() for file in good.iterdir()}
    # Simulate malformed input in an isolated test DB, never in a real database.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE child(pid INTEGER REFERENCES parent(id))')
        conn.execute('INSERT INTO child VALUES (42)')
    failed = tmp_path / 'failed'
    with pytest.raises(DatabaseError, match='foreign key'):
        create_backup(cfg, failed)
    assert not failed.exists()
    assert not list(tmp_path.glob('.backup-*'))
    with pytest.raises(DatabaseError):
        create_backup(cfg, good)
    assert {file.name: file.read_bytes() for file in good.iterdir()} == before


def auth_file(install: Path, *, host: str) -> Path:
    """A real credential file is mandatory now; tests derive it, never a default."""
    from backend.security.passwords import hash_password
    file = install / 'data' / 'auth.json'
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({
        'username': 'owner',
        'password_hash': hash_password('test-only-password', salt=b'0' * 16),
        'allowed_hosts': [host], 'allowed_origins': [],
        'session_ttl_seconds': 3600, 'cookie_secure': False,
    }), encoding='utf-8')
    return file


def test_server_real_http_and_second_instance(formal: tuple[Path, Identity]) -> None:
    path, identity = formal
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    install = isolated_install(path.parent)
    (install / 'data').mkdir()
    shutil.copy2(path.parent / 'official.json', install / 'data' / 'official.json')
    auth_file(install, host=f'127.0.0.1:{port}')
    cmd = [sys.executable, str(install / 'start.py'), 'server', '--database', str(path),
           '--uuid', identity.database_uuid, '--generation', '1', '--port', str(port),
           '--allow-missing-web-build']
    first = subprocess.Popen(cmd, cwd=path.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if first.poll() is not None:
                pytest.fail(f'server exited early: {first.communicate()}')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/health', timeout=0.5) as response:
                    assert response.status == 200
                    assert json.load(response) == {'status': 'ok', 'version': '2.0.0-dev'}
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail('server did not start')
        # Business data stays closed to a client that never authenticated.
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/system/status', timeout=1)
        assert refused.value.code == 401
        second = subprocess.run(cmd, cwd=path.parent, capture_output=True, text=True, timeout=10)
        assert second.returncode != 0 and 'writer lock unavailable' in second.stderr
    finally:
        first.terminate()
        try:
            first.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate(timeout=10)
    with open_database(settings(path, identity)):
        pass
