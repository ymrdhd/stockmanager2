"""Remote-access boundary: authentication, host/origin/CSRF, declared identity and request recovery."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig, AuthConfig
from backend.infrastructure.database import (
    DatabaseError,
    DatabaseSettings,
    initialize_database,
    open_database,
)
from backend.main import create_app
from backend.security.passwords import hash_password

PASSWORD = 'test-only-correct-horse'
HOST = '127.0.0.1:9091'
BASE = 'http://' + HOST


@pytest.fixture
def cfg(tmp_path: Path) -> DatabaseSettings:
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    return DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)


def auth(**overrides: object) -> AuthConfig:
    values: dict[str, object] = {
        'username': 'owner',
        'password_hash': hash_password(PASSWORD, salt=b'7' * 16),
        'allowed_hosts': (HOST, 'localhost:9091'),
        'session_ttl_seconds': 3600,
        'cookie_secure': False,
    }
    values.update(overrides)
    return AuthConfig(**values)  # type: ignore[arg-type]


def app_for(cfg: DatabaseSettings, **overrides: object):
    return create_app(AppConfig(market_auto_refresh=False, auth=auth(**overrides),
                                require_write_identity=True), database=cfg)


def sign_in(client: TestClient) -> dict[str, str]:
    response = client.post('/api/v1/auth/login', json={'username': 'owner', 'password': PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()['csrf']
    return {'x-csrf-token': token}


def identity_headers(cfg: DatabaseSettings, token: dict[str, str]) -> dict[str, str]:
    return {**token, 'x-database-uuid': cfg.expected_uuid,
            'x-database-generation': str(cfg.generation)}


def account_body(request_id: str) -> dict[str, object]:
    return {'request_id': request_id, 'name': 'security account', 'currency': 'CNY',
            'effective_at': '2006-01-01T00:00:00+08:00'}


def test_sec01_unauthenticated_business_data_is_refused(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        assert client.get('/api/v1/health').status_code == 200
        for path in ('/api/v1/system/status', '/api/v1/trades', '/api/v1/finance/balances',
                     '/api/v1/market/status', '/api/v1/requests/anything'):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json()['detail']['code'] == 'unauthenticated'
        write = client.post('/api/v1/accounts', json=account_body('unauth-write'))
        assert write.status_code == 401
    with closing(sqlite3.connect(cfg.path)) as conn, conn:
        assert conn.execute('SELECT count(*) FROM accounts').fetchone() == (0,)


def test_sec02_login_session_and_revocation(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        wrong = client.post('/api/v1/auth/login', json={'username': 'owner', 'password': 'nope'})
        assert wrong.status_code == 401
        assert wrong.json()['detail']['code'] == 'invalid_credentials'
        token = sign_in(client)
        session = client.get('/api/v1/auth/session')
        assert session.status_code == 200 and session.json()['username'] == 'owner'
        assert session.json()['csrf'] == token['x-csrf-token']
        assert client.get('/api/v1/system/status').status_code == 200
        assert client.post('/api/v1/auth/logout', headers=token).status_code == 200
        assert client.get('/api/v1/system/status').status_code == 401


def test_sec03_host_and_origin_are_rejected_before_handlers(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        forged = client.get('/api/v1/health', headers={'host': 'evil.example'})
        assert forged.status_code == 400
        assert forged.json()['detail']['code'] == 'host_not_allowed'
        bad_origin = client.get('/api/v1/health', headers={'origin': 'https://evil.example'})
        assert bad_origin.status_code == 403
        assert bad_origin.json()['detail']['code'] == 'origin_not_allowed'
        good_origin = client.get('/api/v1/health', headers={'origin': BASE})
        assert good_origin.status_code == 200


def test_sec04_proxy_identity_headers_never_authenticate(cfg: DatabaseSettings) -> None:
    spoofed = {'x-forwarded-host': HOST, 'x-forwarded-proto': 'https',
               'x-forwarded-for': '127.0.0.1', 'tailscale-user-login': 'owner',
               'remote-user': 'owner', 'x-authenticated-user': 'owner'}
    with TestClient(app_for(cfg), base_url=BASE) as client:
        assert client.get('/api/v1/health', headers=spoofed).status_code == 200
        assert client.get('/api/v1/system/status', headers=spoofed).status_code == 401
        assert client.post('/api/v1/accounts', json=account_body('proxy-forged'),
                           headers=spoofed).status_code == 401


def test_sec05_csrf_token_is_required_for_writes(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        sign_in(client)
        missing = client.post('/api/v1/accounts', json=account_body('no-csrf'),
                              headers={'x-database-uuid': cfg.expected_uuid,
                                       'x-database-generation': '1'})
        assert missing.status_code == 403
        assert missing.json()['detail']['code'] == 'csrf_failed'
        wrong = client.post('/api/v1/accounts', json=account_body('bad-csrf'),
                            headers={'x-csrf-token': 'forged', 'x-database-uuid': cfg.expected_uuid,
                                     'x-database-generation': '1'})
        assert wrong.status_code == 403 and wrong.json()['detail']['code'] == 'csrf_failed'
    with closing(sqlite3.connect(cfg.path)) as conn, conn:
        assert conn.execute('SELECT count(*) FROM accounts').fetchone() == (0,)


def test_sec06_write_identity_must_match_the_open_database(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        token = sign_in(client)
        absent = client.post('/api/v1/accounts', json=account_body('no-identity'), headers=token)
        assert absent.status_code == 428
        assert absent.json()['detail']['code'] == 'identity_required'
        malformed = client.post('/api/v1/accounts', json=account_body('bad-identity'),
                                headers={**token, 'x-database-uuid': 'not-a-uuid',
                                         'x-database-generation': '1'})
        assert malformed.status_code == 400
        assert malformed.json()['detail']['code'] == 'identity_malformed'
        other = str(uuid4())
        wrong = client.post('/api/v1/accounts', json=account_body('other-database'),
                            headers={**token, 'x-database-uuid': other,
                                     'x-database-generation': '1'})
        assert wrong.status_code == 409
        assert wrong.json()['detail']['code'] == 'identity_mismatch'
        stale = client.post('/api/v1/accounts', json=account_body('stale-generation'),
                            headers={**token, 'x-database-uuid': cfg.expected_uuid,
                                     'x-database-generation': '2'})
        assert stale.status_code == 409 and stale.json()['detail']['code'] == 'identity_mismatch'
    with closing(sqlite3.connect(cfg.path)) as conn, conn:
        assert conn.execute('SELECT count(*) FROM accounts').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM ledger_requests').fetchone() == (0,)
    # A matching declaration is the only path that writes.
    with TestClient(app_for(cfg), base_url=BASE) as client:
        headers = identity_headers(cfg, sign_in(client))
        accepted = client.post('/api/v1/accounts', json=account_body('matching'), headers=headers)
        assert accepted.status_code == 200, accepted.text


def test_sec07_lost_response_recovers_one_trade_and_one_batch(cfg: DatabaseSettings) -> None:
    body: dict[str, object] = {'request_id': 'lost-response-buy', 'side': 'buy', 'quantity': 100,
                               'price': '3.4567', 'fees_fen': 5,
                               'executed_at': '2026-01-05T09:30:00+08:00',
                               'trading_day': '2026-01-05'}
    with TestClient(app_for(cfg), base_url=BASE) as client:
        headers = identity_headers(cfg, sign_in(client))
        account = client.post('/api/v1/accounts', json=account_body('sec-recovery'), headers=headers).json()['id']
        instrument = client.post('/api/v1/instruments', headers=headers, json={
            'request_id': 'sec-instrument', 'code': '510300', 'market': 'SH', 'kind': 'etf'}).json()['id']
        order = {**body, 'account_id': account, 'instrument_id': instrument}
        first = client.post('/api/v1/trades', json=order, headers=headers)
        assert first.status_code == 200, first.text
        # The browser lost this response; the page is reloaded in a new client.
    with TestClient(app_for(cfg), base_url=BASE) as client:
        headers = identity_headers(cfg, sign_in(client))
        state = client.get('/api/v1/requests/lost-response-buy', headers=headers)
        assert state.status_code == 200
        reported = state.json()
        assert reported['committed'] is True
        assert reported['result']['id'] == first.json()['id']
        assert reported['database_uuid'] == cfg.expected_uuid
        assert reported['generation'] == cfg.generation
        # Retrying with the same request ID returns the original result, never a second trade.
        again = client.post('/api/v1/trades', json=order, headers=headers)
        assert again.status_code == 200
        assert again.json() == first.json()
        unknown = client.get('/api/v1/requests/never-submitted', headers=headers)
        assert unknown.json()['committed'] is False and unknown.json()['result'] is None
        cross = client.get('/api/v1/requests/lost-response-buy',
                           headers={**headers, 'x-database-uuid': str(uuid4())})
        assert cross.json()['database_uuid'] == cfg.expected_uuid
    with closing(sqlite3.connect(cfg.path)) as conn, conn:
        assert conn.execute('SELECT count(*) FROM trade_executions').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM trade_lots').fetchone() == (1,)
        assert conn.execute("SELECT count(*), sum(amount_fen) FROM cash_movements WHERE kind='trade'").fetchone() == (1, -34572)
        # Exactly one ledger request for this trade, even though the body was posted twice.
        assert conn.execute("SELECT count(*) FROM ledger_requests WHERE request_id='lost-response-buy'").fetchone() == (1,)


def test_sec08_standalone_local_sandbox_without_formal_binding(tmp_path: Path) -> None:
    sandbox = tmp_path / 'standalone' / 'sandbox.sqlite'
    unbound = tmp_path / 'standalone' / 'official.json'
    unbound.parent.mkdir()
    identity = initialize_database(sandbox, role='sandbox', confirm=True, binding_path=unbound)
    assert identity.instance_role == 'sandbox'
    assert not unbound.exists()
    formal = tmp_path / 'company' / 'official.sqlite'
    local = DatabaseSettings(sandbox, identity.database_uuid, identity.generation,
                             official_path=formal, mode='local', writable_sandbox=True,
                             binding_path=unbound)
    with open_database(local) as db:
        assert db.settings.writable is True
        assert db.identity.instance_role == 'sandbox'
    with TestClient(create_app(AppConfig(market_auto_refresh=False), database=local), base_url=BASE) as client:
        status = client.get('/api/v1/system/status').json()
        assert status['mode'] == 'sandbox' and status['writable'] is True
        created = client.post('/api/v1/accounts', json=account_body('local-only'))
        assert created.status_code == 200, created.text
    with closing(sqlite3.connect(sandbox)) as conn, conn:
        assert conn.execute('SELECT count(*) FROM accounts').fetchone() == (1,)
    assert not formal.exists()


def test_sec09_local_mode_never_opens_the_formal_database(tmp_path: Path) -> None:
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    official = DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)
    with pytest.raises(DatabaseError, match='must use a distinct snapshot or sandbox'):
        DatabaseSettings(path, identity.database_uuid, 1, path, mode='local', writable_sandbox=True,
                         binding_path=binding)
    copy = tmp_path / 'copy.sqlite'
    copy.write_bytes(path.read_bytes())
    # A byte copy keeps the formal UUID, so it may only open as a read-only snapshot:
    # a writable sandbox must carry instance_role='sandbox' and this one never will.
    snapshot = DatabaseSettings(copy, identity.database_uuid, 1, path, mode='local',
                                binding_path=binding)
    copied = DatabaseSettings(copy, identity.database_uuid, 1, path, mode='local',
                              writable_sandbox=True, binding_path=binding)
    with pytest.raises(DatabaseError, match='UUID, role or generation mismatch'):
        with open_database(copied):
            pytest.fail('a formal database copy became a writable sandbox')
    with open_database(snapshot) as db, db.read():
        assert db.settings.writable is False
        assert db.identity.instance_role == 'formal'
    alias = tmp_path / 'alias.sqlite'
    try:
        alias.hardlink_to(path)
    except (OSError, NotImplementedError):
        pytest.skip('hard links unavailable on this file system')
    with pytest.raises(DatabaseError):
        DatabaseSettings(alias, identity.database_uuid, 1, path, mode='local', writable_sandbox=True,
                         binding_path=binding)
    alias.unlink()  # Remove the extra link so the formal file is single-link again.
    with pytest.raises(DatabaseError, match='initialization path conflicts with official database role'):
        initialize_database(path, role='sandbox', confirm=True, official_path=path,
                            binding_path=tmp_path / 'fresh' / 'official.json')
    # The refused call must leave the formal database untouched.
    assert path.is_file()
    with open_database(official) as db, db.read():
        assert db.identity.instance_role == 'formal'


def test_sec10_broken_formal_binding_blocks_writes_and_static_build_is_honest(tmp_path: Path) -> None:
    path = tmp_path / 'official.sqlite'
    binding = tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    binding.write_text('{ not json', encoding='utf-8')
    broken = DatabaseSettings(path, identity.database_uuid, 1, path, binding_path=binding)
    with pytest.raises(DatabaseError, match='formal binding missing or invalid'):
        with TestClient(create_app(AppConfig(market_auto_refresh=False), database=broken), base_url=BASE):
            pytest.fail('service started with a broken formal binding')
    with pytest.raises(DatabaseError, match='formal binding missing or invalid'):
        open_database(broken).__enter__()
    missing_dist = create_app(AppConfig(static_dir=tmp_path / 'absent'))
    with TestClient(missing_dist, base_url=BASE) as client:
        shell = client.get('/')
        assert shell.status_code == 503
        assert shell.json()['detail']['code'] == 'web_build_missing'
        status = client.get('/api/v1/health')
        assert status.status_code == 200
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<!doctype html><title>built shell</title>', encoding='utf-8')
    (dist / 'assets' / 'app.js').write_text('console.log(1)', encoding='utf-8')
    with TestClient(create_app(AppConfig(static_dir=dist)), base_url=BASE) as client:
        page = client.get('/')
        assert page.status_code == 200 and 'built shell' in page.text
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/assets/../index.html').status_code in (200, 404)
        assert client.get('/api/v1/system/status').json()['web']['built'] is True
        assert client.get('/api/v1/system/status').json()['auth_required'] is False


def test_sec11_authenticated_server_reports_identity_for_the_page(cfg: DatabaseSettings) -> None:
    with TestClient(app_for(cfg), base_url=BASE) as client:
        headers = identity_headers(cfg, sign_in(client))
        status = client.get('/api/v1/system/status').json()
        assert status['auth_required'] is True
        assert status['identity']['database_uuid'] == cfg.expected_uuid
        assert status['identity']['instance_role'] == 'formal'
        assert status['mode'] == 'server' and status['writable'] is True
        sandbox = cfg.path.parent / 'session-sandbox.sqlite'
        sandbox_identity = initialize_database(sandbox, role='sandbox', confirm=True,
                                              binding_path=cfg.binding_path)
        local = DatabaseSettings(sandbox, sandbox_identity.database_uuid, 1, cfg.path,
                                 mode='local', writable_sandbox=True, binding_path=cfg.binding_path)
        assert json.loads(json.dumps(local.expected_uuid)) == sandbox_identity.database_uuid
        assert headers['x-database-uuid'] == cfg.expected_uuid
