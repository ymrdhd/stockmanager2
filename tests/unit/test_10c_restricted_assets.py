"""10C restricted-asset marker: liquidity math, audit, migration and recovery.

All databases here are temporary synthetic fixtures. No real ledger is touched.
"""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from backend.application.finance import BalanceInput, FinanceService, LiquidityInput
from backend.application.portfolio import PortfolioService
from backend.application.trading.service import AccountInput, Conflict, TradeError, TradeService
from backend.infrastructure.backup import create_backup
from backend.infrastructure.database import (
    SCHEMA_VERSION, DatabaseSettings, Identity, adopt_database, migrate_schema,
    open_database, upgrade_database,
)
from backend.infrastructure.restore import check_backup, restore
from test_database_upgrade import cfg_for
from test_finance import account, balance, manual
from test_pnl import at, cash, quote
from test_trading import cfg, ledger, trade


def _liquidity(f, account_id, status, *, occurred_at, request='liquidity', note='confirmed statement'):
    revision = f.overview(occurred_at)['ledger_revision']
    return f.set_account_liquidity(account_id, LiquidityInput(
        request_id=request, status=status, occurred_at=occurred_at,
        note=note, expected_ledger_revision=revision))


def test_restricted_asset_kept_in_totals_but_excluded_from_liquid(ledger):
    svc, a, i = ledger
    f, p = FinanceService(svc.db), PortfolioService(svc.db, clock=lambda: at(6))
    bank = manual(svc, 'bank')
    frozen = manual(svc, 'frozen', liquidity='restricted')
    balance(f, bank, 1000000, 'bank-balance')
    balance(f, frozen, 300000, 'frozen-balance')
    cash(svc, a, 'opening', 'opening', 3000000, 5)
    svc.submit(trade(a, i, 'buy-once', quantity=1000, price='10', fees_fen=0))
    quote(p, i, 6, 10)
    before = f.overview(at(6))
    # Securities cash and market value are counted once, through equity.
    assert before['securities_cash_fen'] == 2000000
    assert before['securities_market_value_fen'] == 1000000
    assert before['securities_equity_fen'] == 3000000
    assert before['total_assets_fen'] == 4300000
    assert before['net_worth_fen'] == 4300000
    assert before['restricted_assets_fen'] == 300000
    assert before['liquid_assets_fen'] == 4000000
    assert before['liquid_net_worth_fen'] == 4000000
    assert before['liquidity_reasons'] == []
    row = next(x for x in before['accounts'] if x['id'] == frozen)
    assert row['liquidity_status'] == 'restricted' and '受限' in row['liquidity_status_display']
    # Marking the whole securities account restricted changes only liquidity metrics.
    _liquidity(f, a, 'restricted', occurred_at=at(6, 16), request='freeze-securities')
    after = f.overview(at(6, 17))
    assert after['total_assets_fen'] == before['total_assets_fen']
    assert after['net_worth_fen'] == before['net_worth_fen']
    assert after['restricted_assets_fen'] == 3300000
    assert after['liquid_assets_fen'] == 1000000
    assert after['liquid_net_worth_fen'] == 1000000


def test_unknown_classification_is_withheld_and_prompted(ledger):
    svc, a, i = ledger
    f = FinanceService(svc.db)
    cash(svc, a, 'opening', 'opening', 50000, 5)
    unknown = manual(svc, 'uncertain', 'other_asset', liquidity='unknown')
    balance(f, unknown, 10000, 'unknown-balance')
    v = f.overview(at(6))
    assert v['liquid_assets_fen'] is None
    assert v['liquid_net_worth_fen'] is None
    assert v['liquidity_reasons'] == ['liquidity_classification_unknown:' + unknown]
    assert v['total_assets_fen'] == 60000 and v['net_worth_fen'] == 60000
    row = next(x for x in v['accounts'] if x['id'] == unknown)
    assert row['liquidity_status'] == 'unknown' and row['liquidity_status_effective_at']
    assert '待确认' in row['liquidity_status_display']
    # Confirming it is liquid makes the metric computable without changing totals.
    _liquidity(f, unknown, 'liquid', occurred_at=at(6, 16), request='confirm-other')
    after = f.overview(at(6, 17))
    assert after['liquid_assets_fen'] == 60000 and after['liquidity_reasons'] == []
    assert after['total_assets_fen'] == v['total_assets_fen']


def test_liabilities_keep_current_liability_basis_and_reject_the_marker(ledger):
    svc, a, i = ledger
    f = FinanceService(svc.db)
    cash(svc, a, 'opening', 'opening', 50000, 5)
    bank = manual(svc, 'bank')
    credit = manual(svc, 'credit', 'credit_card')
    long_term = manual(svc, 'long', 'other_liability')
    balance(f, bank, 1000000, 'bank')
    balance(f, credit, 200000, 'credit')
    balance(f, long_term, 100000, 'long')
    v = f.overview(at(6))
    assert v['total_liabilities_fen'] == 300000
    assert v['liquid_assets_fen'] == 1050000
    assert v['liquid_net_worth_fen'] == 750000
    assert v['net_worth_fen'] == 750000
    assert v['liquidity_reasons'] == []
    for row in v['accounts']:
        if row['asset_class'] == 'liability':
            assert '负债' in row['liquidity_status_display']
    with pytest.raises(TradeError):
        _liquidity(f, credit, 'restricted', occurred_at=at(6, 16), request='bad-liability')
    with pytest.raises(ValueError):
        AccountInput(request_id='bad-liability-create', name='credit', kind='credit_card',
                     effective_at=at(5, 7), liquidity_status='liquid')


def test_liquidity_change_is_audited_append_only_and_history_uses_evidence(ledger):
    svc, a, i = ledger
    f = FinanceService(svc.db)
    cash(svc, a, 'opening', 'opening', 50000, 5)
    bank = manual(svc, 'bank', liquidity='liquid')
    balance(f, bank, 100000, 'bank')
    assert f.overview(at(6))['liquid_assets_fen'] == 150000
    stale = f.overview(at(6))['ledger_revision']
    change = LiquidityInput(request_id='freeze', status='restricted', occurred_at=at(7, 8),
                            note='bank statement shows court freeze', expected_ledger_revision=stale)
    result = f.set_account_liquidity(bank, change)
    assert result['previous_status'] == 'liquid' and result['liquidity_status'] == 'restricted'
    assert f.set_account_liquidity(bank, change) == result  # idempotent retry
    with pytest.raises(Conflict):  # stale ledger revision
        f.set_account_liquidity(bank, LiquidityInput(
            request_id='stale', status='liquid', occurred_at=at(7, 9), note='retry',
            expected_ledger_revision=stale))
    current = f.overview(at(8))['ledger_revision']
    with pytest.raises(Conflict):  # append-only evidence, no backdated overwrite
        f.set_account_liquidity(bank, LiquidityInput(
            request_id='backdate', status='liquid', occurred_at=at(6, 9), note='backdate',
            expected_ledger_revision=current))
    # History before the change keeps the recorded classification; after it, the
    # restricted amount is excluded from liquid assets but stays in total assets.
    before = f.overview(at(6, 23))
    after = f.overview(at(7, 9))
    assert before['liquid_assets_fen'] == 150000 and before['restricted_assets_fen'] == 0
    assert after['liquid_assets_fen'] == 50000 and after['restricted_assets_fen'] == 100000
    assert after['total_assets_fen'] == before['total_assets_fen'] == 150000
    with svc.db.read() as conn:
        events = conn.execute(
            "SELECT details FROM audit_events WHERE entity='account' AND entity_id=? "
            "AND event='liquidity_changed'", (bank,)).fetchall()
        assert len(events) == 1
        history = conn.execute(
            'SELECT previous_status,liquidity_status,kind FROM account_liquidity_events '
            'WHERE account_id=? ORDER BY rowid', (bank,)).fetchall()
        assert history == [('unknown', 'liquid', 'initial'), ('liquid', 'restricted', 'changed')]


def test_liquidity_status_survives_database_reopen(cfg):
    with open_database(cfg) as db:
        svc, f = TradeService(db), FinanceService(db)
        account_id = svc.create_account(AccountInput(
            request_id='bank', name='bank', kind='bank',
            effective_at='2025-01-01T00:00:00+08:00',
            liquidity_status='restricted'))['id']
        f.balance(BalanceInput(request_id='balance', account_id=account_id, after_fen=100000,
                               occurred_at='2025-01-02T10:00:00+08:00', note='statement',
                               expected_balance_id=None))
    with open_database(cfg) as db:
        f = FinanceService(db)
        v = f.overview(at(6))
        assert v['restricted_assets_fen'] == 100000
        assert v['liquid_assets_fen'] == 0 and v['liquid_net_worth_fen'] == 0
        assert v['total_assets_fen'] == 100000 and v['net_worth_fen'] == 100000
        row = account(v, account_id)
        assert row['liquidity_status'] == 'restricted'


def test_0011_upgrade_never_assumes_existing_accounts_are_unrestricted(tmp_path):
    path = tmp_path / 'old.sqlite'
    identity = replace(Identity.new('formal'), schema_version=11)
    engine = create_engine(URL.create('sqlite', database=str(path)))
    try:
        migrate_schema(engine, identity, target='0011')
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO accounts (id,name,kind,currency,balance_mode,status,"
                     "asset_class,included,effective_at) VALUES "
                     "('bank','bank','bank','CNY','manual_snapshot','active','asset',1,"
                     "'2025-01-01T00:00:00Z')")
        conn.execute("INSERT INTO balance_adjustments VALUES ('b','bank',NULL,100000,"
                     "'2025-01-01T00:00:00Z','opening',NULL,NULL,'effective')")
    settings = cfg_for(path, identity, tmp_path / 'official.json')
    adopt_database(settings, confirm=True)
    destination = tmp_path / 'pre-0012'
    upgrade_database(settings, destination, confirm=True)
    with open_database(settings) as db, db.read() as conn:
        assert db.identity.schema_version == SCHEMA_VERSION
        assert conn.execute('SELECT liquidity_status FROM accounts').fetchall() == [('unknown',)]
        assert conn.execute('SELECT count(*) FROM account_liquidity_events').fetchone() == (0,)
        assert conn.execute('SELECT after_fen FROM balance_adjustments').fetchall() == [(100000,)]
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    with open_database(settings) as db:
        v = FinanceService(db).overview(at(6))
        assert v['total_assets_fen'] == 100000 and v['net_worth_fen'] == 100000
        assert v['liquid_assets_fen'] is None and v['liquid_net_worth_fen'] is None
        assert v['liquidity_reasons'] == ['liquidity_classification_unknown:bank']
    with closing(sqlite3.connect(destination / 'database.sqlite')) as conn:
        assert conn.execute('SELECT schema_version FROM system_metadata').fetchone() == (11,)
        assert conn.execute('SELECT count(*) FROM accounts').fetchone() == (1,)


def test_backup_and_restore_preserve_liquidity_classification(cfg, tmp_path):
    with open_database(cfg) as db:
        svc, f = TradeService(db), FinanceService(db)
        account_id = svc.create_account(AccountInput(
            request_id='bank', name='bank', kind='bank',
            effective_at='2025-01-01T00:00:00+08:00',
            liquidity_status='restricted'))['id']
        f.balance(BalanceInput(request_id='balance', account_id=account_id, after_fen=100000,
                               occurred_at='2025-01-02T10:00:00+08:00', note='statement',
                               expected_balance_id=None))
        created = create_backup(cfg, tmp_path / 'backup')
    assert check_backup(cfg, created)['ok']
    report = restore(cfg, created, confirm=True)
    assert report['ok']
    restored = DatabaseSettings(path=cfg.path, official_path=cfg.path,
                                expected_uuid=cfg.expected_uuid,
                                generation=report['new_generation'],
                                binding_path=cfg.binding_path)
    with open_database(restored) as db:
        v = FinanceService(db).overview(at(6))
        assert v['restricted_assets_fen'] == 100000
        assert v['liquid_assets_fen'] == 0
        assert v['total_assets_fen'] == 100000 and v['net_worth_fen'] == 100000
