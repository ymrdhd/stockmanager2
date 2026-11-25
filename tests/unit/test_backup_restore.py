"""Backup completeness and restore rehearsal; exclusively temporary databases."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from backend.application.finance import BillInput, CategoryInput, FinanceService
from backend.application.trading.service import (
    AccountInput,
    CashInput,
    InstrumentInput,
    TradeInput,
    TradeService,
)
from backend.infrastructure import restore as restore_module
from backend.infrastructure.backup import MANIFEST_NAME, SNAPSHOT_NAME, create_backup
from backend.infrastructure.binding import load_binding
from backend.infrastructure.database import (
    DatabaseError,
    DatabaseSettings,
    Identity,
    initialize_database,
    open_database,
    validate_identity,
)
from backend.infrastructure.restore import RestoreError, check_backup, restore

BUSINESS_TABLES = ('accounts', 'instruments', 'trade_executions', 'trade_lots',
                   'cash_movements', 'finance_transactions', 'lot_allocations',
                   'balance_adjustments', 'audit_events', 'opening_positions',
                   'company_events', 'strategy_signals', 'strategy_plans')


@pytest.fixture
def formal(tmp_path: Path) -> DatabaseSettings:
    path, binding = tmp_path / 'official.sqlite', tmp_path / 'official.json'
    identity = initialize_database(path, role='formal', confirm=True, binding_path=binding)
    return DatabaseSettings(path=path, expected_uuid=identity.database_uuid,
                            generation=identity.generation, official_path=path,
                            binding_path=binding)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def counts(settings: DatabaseSettings) -> dict[str, int]:
    with open_database(settings) as db, db.read() as conn:
        return {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in BUSINESS_TABLES}


def seed(settings: DatabaseSettings) -> dict[str, str]:
    """Write one account, one instrument, one execution, one lot and one bill."""
    with open_database(settings) as db:
        trades, finance = TradeService(db), FinanceService(db)
        account = trades.create_account(AccountInput(
            request_id='account-securities', name='securities',
            effective_at='2000-01-01T00:00:00+08:00'))['id']
        bank = trades.create_account(AccountInput(
            request_id='account-bank', name='bank', kind='bank',
            effective_at='2000-01-01T00:00:00+08:00'))['id']
        instrument = trades.create_instrument(InstrumentInput(
            request_id='instrument-510300', code='510300', market='SH', kind='etf'))['id']
        trades.cash(CashInput(request_id='cash-opening', account_id=account, kind='opening',
                              amount_fen=500000, note='verified opening',
                              occurred_at='2026-01-05T01:00:00+08:00'))
        trades.submit(TradeInput(request_id='trade-buy', account_id=account,
                                 instrument_id=instrument, side='buy', quantity=100,
                                 price='3.4567', fees_fen=5,
                                 executed_at='2026-01-05T09:30:00+08:00',
                                 trading_day='2026-01-05'))
        category = finance.category(CategoryInput(request_id='category-books', name='books',
                                                  kind='expense'))['id']
        finance.bill(BillInput(request_id='bill-books', account_id=bank, kind='expense',
                               category_id=category, amount_fen=12345, note='receipt',
                               occurred_at='2026-01-06T12:00:00+08:00'))
        return {'account': account, 'bank': bank, 'instrument': instrument,
                'category': category}


def add_later_facts(settings: DatabaseSettings, ids: dict[str, str]) -> None:
    """Facts written after the backup must disappear when it is restored."""
    with open_database(settings) as db:
        trades, finance = TradeService(db), FinanceService(db)
        trades.submit(TradeInput(request_id='trade-later', account_id=ids['account'],
                                 instrument_id=ids['instrument'], side='buy', quantity=50,
                                 price='4.5000', fees_fen=5,
                                 executed_at='2026-01-07T09:30:00+08:00',
                                 trading_day='2026-01-07'))
        trades.cash(CashInput(request_id='cash-later', account_id=ids['account'],
                              kind='deposit', amount_fen=250000, note='later deposit',
                              occurred_at='2026-01-07T10:00:00+08:00'))
        finance.bill(BillInput(request_id='bill-later', account_id=ids['bank'],
                               kind='expense', category_id=ids['category'],
                               amount_fen=999, note='later bill',
                               occurred_at='2026-01-07T12:00:00+08:00'))


def test_backup_manifest_is_complete_and_verifiable(formal: DatabaseSettings,
                                                    tmp_path: Path) -> None:
    ids = seed(formal)
    assert ids['account']
    destination = tmp_path / 'backup-1'
    created = create_backup(formal, destination)
    manifest = json.loads((created / MANIFEST_NAME).read_text(encoding='utf-8'))
    for field in ('database_uuid', 'generation', 'schema_version', 'backup_at', 'file_size',
                  'sha256', 'ledger_revision', 'quote_revision', 'source_path_basename',
                  'integrity_check', 'foreign_key_check', 'missing'):
        assert field in manifest, field
    assert manifest['instance_role'] == 'backup'
    assert manifest['source_instance_role'] == 'formal'
    assert manifest['source_path_basename'] == formal.path.name
    assert manifest['file_size'] == (created / SNAPSHOT_NAME).stat().st_size
    assert manifest['sha256'] == digest(created / SNAPSHOT_NAME)
    assert manifest['integrity_check'] == manifest['foreign_key_check'] == 'ok'
    with open_database(formal) as db, db.read() as conn:
        revisions = (conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0],
                     conn.execute('SELECT quote_revision FROM quote_state').fetchone()[0])
    assert (manifest['ledger_revision'], manifest['quote_revision']) == revisions
    assert manifest['ledger_revision'] > 0 and manifest['quote_revision'] == 0
    assert manifest['missing'] == []
    assert manifest['contains_personal_financial_data'] is True
    assert '个人' in manifest['sensitive_notice']
    # A backup directory holds exactly the snapshot and its manifest: no credential file.
    assert sorted(item.name for item in created.iterdir()) == sorted([MANIFEST_NAME, SNAPSHOT_NAME])
    assert 'password' not in json.dumps(manifest) and 'csrf' not in json.dumps(manifest)
    verdict = check_backup(formal, created)
    assert verdict['ok'], verdict
    # Counts must match the ledger the snapshot was taken from, table by table.
    assert verdict['counts'] == counts(formal)
    assert all(item['ok'] for item in verdict['checks']), verdict['checks']


def test_restore_returns_every_table_to_the_backup_point(formal: DatabaseSettings,
                                                          tmp_path: Path) -> None:
    ids = seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    before = counts(formal)
    add_later_facts(formal, ids)
    after = counts(formal)
    assert after['trade_executions'] == before['trade_executions'] + 1
    # A trade posts its own cash movement, so the deposit plus its fee both add rows.
    assert after['cash_movements'] > before['cash_movements']
    assert after['finance_transactions'] == before['finance_transactions'] + 1
    report = restore(formal, created, confirm=True)
    assert report['ok'], report
    assert report['previous_generation'] == formal.generation
    binding = load_binding(formal.binding_path)
    restored = DatabaseSettings(path=formal.path, official_path=formal.path,
                                expected_uuid=binding.uuid, generation=binding.generation,
                                binding_path=formal.binding_path)
    assert counts(restored) == before
    with open_database(restored) as db, db.read() as conn:
        assert conn.execute('SELECT instance_role FROM system_metadata').fetchone() == ('formal',)
        # The later facts are gone, and so is the request record that produced them.
        assert conn.execute("SELECT count(*) FROM trade_executions WHERE request_id='trade-later'").fetchone() == (0,)


def test_corrupt_or_unverifiable_backup_is_refused(formal: DatabaseSettings,
                                                    tmp_path: Path) -> None:
    seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    broken = tmp_path / 'broken'
    shutil.copytree(created, broken)
    with (broken / SNAPSHOT_NAME).open('r+b') as stream:
        stream.truncate(256)
    verdict = check_backup(formal, broken)
    assert not verdict['ok'] and verdict['reason']
    assert any(item['name'] == 'sha256' and not item['ok'] for item in verdict['checks'])
    with pytest.raises(RestoreError):
        restore(formal, broken, confirm=True)

    edited = tmp_path / 'edited'
    shutil.copytree(created, edited)
    manifest = json.loads((edited / MANIFEST_NAME).read_text(encoding='utf-8'))
    manifest['sha256'] = '0' * 64
    (edited / MANIFEST_NAME).write_text(json.dumps(manifest), encoding='utf-8')
    assert not check_backup(formal, edited)['ok']
    with pytest.raises(RestoreError):
        restore(formal, edited, confirm=True)

    missing = tmp_path / 'missing-manifest'
    shutil.copytree(created, missing)
    (missing / MANIFEST_NAME).unlink()
    verdict = check_backup(formal, missing)
    assert not verdict['ok'] and verdict['reason'] == 'missing_manifest'
    with pytest.raises(RestoreError):
        restore(formal, missing, confirm=True)


def test_live_writer_blocks_restore_and_changes_nothing(formal: DatabaseSettings,
                                                        tmp_path: Path) -> None:
    seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    mark = digest(formal.path)
    binding_text = formal.binding_path.read_text(encoding='utf-8')
    with open_database(formal) as db:
        assert db.identity.database_uuid == formal.expected_uuid
        with pytest.raises(RestoreError, match='writer is still active'):
            restore(formal, created, confirm=True)
    assert digest(formal.path) == mark
    assert formal.binding_path.read_text(encoding='utf-8') == binding_text
    assert not (formal.path.parent / 'backups').exists()
    # The lock sidecar is still there: clearing the blocker never means deleting it.
    assert formal.path.with_name(formal.path.name + '.writer.lock').exists()


def test_failed_switch_keeps_database_and_binding(formal: DatabaseSettings,
                                                  tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    ids = seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    add_later_facts(formal, ids)
    after = counts(formal)
    mark, binding_text = digest(formal.path), formal.binding_path.read_text(encoding='utf-8')

    def broken_swap(staged: Path, official: Path) -> None:
        raise OSError('injected publication failure')

    monkeypatch.setattr(restore_module, '_swap', broken_swap)
    with pytest.raises(RestoreError, match='aborted before switching'):
        restore(formal, created, confirm=True)
    assert digest(formal.path) == mark
    assert formal.binding_path.read_text(encoding='utf-8') == binding_text
    # The pre-restore backup was still taken, and the ledger is still usable as before.
    assert list((formal.path.parent / 'backups').iterdir())
    assert counts(formal) == after
    with open_database(formal) as db, db.read() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)


def test_restore_publishes_new_generation_and_old_identity_is_rejected(
        formal: DatabaseSettings, tmp_path: Path) -> None:
    seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    report = restore(formal, created, confirm=True)
    assert report['ok'], report
    assert report['new_generation'] == formal.generation + 1
    assert report['pre_restore_backup'] and Path(report['pre_restore_backup']).is_dir()
    assert '非原子窗口' in report['atomic_note']
    binding = load_binding(formal.binding_path)
    assert binding.generation == report['new_generation']
    assert binding.path == formal.path
    old = DatabaseSettings(path=formal.path, official_path=formal.path,
                           expected_uuid=formal.expected_uuid, generation=formal.generation,
                           binding_path=formal.binding_path)
    with closing(sqlite3.connect(f'{formal.path.as_uri()}?mode=ro', uri=True)) as conn:
        with pytest.raises(DatabaseError, match='mismatch'):
            validate_identity(conn, old)
    with pytest.raises(DatabaseError):
        with open_database(old):
            pass


def test_backup_file_is_never_accepted_as_the_formal_ledger(formal: DatabaseSettings,
                                                            tmp_path: Path) -> None:
    seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    snapshot = created / SNAPSHOT_NAME
    manifest = json.loads((created / MANIFEST_NAME).read_text(encoding='utf-8'))
    as_formal = DatabaseSettings(path=snapshot, expected_uuid=manifest['database_uuid'],
                                generation=manifest['generation'], official_path=snapshot,
                                binding_path=tmp_path / 'unused.json')
    with closing(sqlite3.connect(f'{snapshot.as_uri()}?mode=ro', uri=True)) as conn:
        identity = Identity(*conn.execute(
            'SELECT database_uuid,instance_role,generation,schema_version,created_at '
            'FROM system_metadata').fetchone())
        assert identity.instance_role == 'backup'
        with pytest.raises(DatabaseError, match='mismatch'):
            validate_identity(conn, as_formal)
    with pytest.raises(DatabaseError):
        with open_database(as_formal):
            pass


def test_open_read_write_connection_also_blocks_restore(formal: DatabaseSettings,
                                                        tmp_path: Path) -> None:
    """A live rw connection proves occupancy through the ledger itself, no lock file needed."""
    seed(formal)
    created = create_backup(formal, tmp_path / 'backup-1')
    mark = digest(formal.path)
    outsider = sqlite3.connect(f'{formal.path.as_uri()}?mode=rw', uri=True,
                              timeout=1, isolation_level=None)
    try:
        outsider.execute('BEGIN IMMEDIATE')
        assert (formal.path.parent / 'backups').exists() is False
        with pytest.raises(RestoreError, match='still being written|still active|unavailable'):
            restore(formal, created, confirm=True)
        outsider.execute('ROLLBACK')
    finally:
        outsider.close()
    assert digest(formal.path) == mark
    # With the connection gone the same restore is allowed, so the refusal was the writer.
    assert restore(formal, created, confirm=True)['ok'] is True


def test_failed_backup_never_overwrites_a_valid_one(formal: DatabaseSettings,
                                                    tmp_path: Path) -> None:
    seed(formal)
    destination = tmp_path / 'backup-1'
    created = create_backup(formal, destination)
    snapshot, manifest = created / SNAPSHOT_NAME, created / MANIFEST_NAME
    before = (digest(snapshot), manifest.read_bytes())
    with pytest.raises(DatabaseError, match='new independent directory'):
        create_backup(formal, destination)
    assert (digest(snapshot), manifest.read_bytes()) == before
    # An interrupted staging directory is removed and never published as a backup.
    with pytest.raises(DatabaseError):
        create_backup(formal, formal.path)
    assert not [item for item in formal.path.parent.iterdir() if item.name.startswith('.backup-')]


def test_restore_holds_writer_lock_through_publication(formal,tmp_path,monkeypatch):
    seed(formal)
    backup=create_backup(formal,tmp_path/'locked-backup')
    original=restore_module._swap
    def checked(staged,official):
        with pytest.raises(DatabaseError):
            with open_database(formal):
                pytest.fail('competing writer opened during restore')
        return original(staged,official)
    monkeypatch.setattr(restore_module,'_swap',checked)
    assert restore(formal,backup,confirm=True)['ok']


def test_restore_refuses_report_overwriting_database(formal,tmp_path):
    seed(formal)
    backup=create_backup(formal,tmp_path/'report-backup')
    before=digest(formal.path)
    with pytest.raises(RestoreError):
        restore(formal,backup,confirm=False,destination_report=formal.path)
    assert digest(formal.path)==before


def test_restore_valuation_at_same_cutoff(formal, tmp_path):
    from datetime import datetime
    from backend.application.finance import BalanceInput
    from backend.application.portfolio import PortfolioService, QuoteInput
    ids = seed(formal)
    cutoff = datetime.fromisoformat('2026-01-07T16:00:00+08:00')
    with open_database(formal) as db:
        FinanceService(db).balance(BalanceInput(request_id='confirmed-balance',account_id=ids['bank'],after_fen=800000,
            expected_balance_id=None,occurred_at='2026-01-06T12:00:00+08:00',note='statement fixture'))
        PortfolioService(db).quote(QuoteInput(request_id='confirmed-quote',instrument_id=ids['instrument'],price='4.0000',source='fixture',
            observed_at='2026-01-07T15:00:00+08:00',trading_day='2026-01-07',kind='close',quality='valid'))
        before = FinanceService(db).overview(cutoff)
        assert before['net_worth_fen']==1305428
    backup = create_backup(formal,tmp_path/'valuation-backup')
    add_later_facts(formal,ids)
    restore(formal,backup,confirm=True)
    binding = load_binding(formal.binding_path)
    restored = DatabaseSettings(path=formal.path,official_path=formal.path,expected_uuid=binding.uuid,
                                generation=binding.generation,binding_path=formal.binding_path)
    with open_database(restored) as db:
        after = FinanceService(db).overview(cutoff)
        for key in ('securities_cash_fen','securities_market_value_fen','securities_equity_fen','net_worth_fen','ledger_revision','quote_revision','finance_month'):
            assert before[key]==after[key]
