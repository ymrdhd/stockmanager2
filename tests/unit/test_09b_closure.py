"""Focused 09B checks with synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC, timedelta
from threading import Barrier
import json
import sqlite3
from contextlib import closing

import pytest

from test_trading import cfg, ledger, trade
from test_strategy_service import service, fund, FakeData, rising_bars
from test_migration import builder, sources, query
from backend.application.strategy.store import CapacityRefused
from backend.domain.strategy.config import INDEX_STRATEGY_VERSION, get_config
from backend.domain.strategy.symbols import configured_index_ids
from backend.application.migration import run_migration, write_report, Refused
from backend.application.migration_reconcile import reconcile
from backend.infrastructure.database import initialize_database, DatabaseSettings
from backend.infrastructure.takeover import takeover
from backend.infrastructure.backup import daily_backup, create_backup


def test_transaction_capacity_race(ledger):
    svc, account, _ = ledger
    fund(svc,account)
    strategy = service(svc.db)
    ids = [i for i in configured_index_ids() if get_config(i).counts_against_core_slots]
    # Seven claims already occupy slots. Competing distinct eighth/ninth claims
    # reach the transaction concurrently; only one may commit.
    for idx in ids[:7]:
        strategy.store.save_plan(account_id=account,index_id=idx,signal_date='2026-11-24',strategy_version=INDEX_STRATEGY_VERSION,status='SIGNAL')
    barrier=Barrier(2)
    def attempt(idx):
        barrier.wait(timeout=5)
        try:
            strategy.store.save_plan(account_id=account,index_id=idx,signal_date='2026-11-24',strategy_version=INDEX_STRATEGY_VERSION,status='SIGNAL',capacity_check=lambda conn: strategy._check_plan_capacity(conn,account,idx,'2026-11-24','synthetic-'+idx))
            return 'saved'
        except CapacityRefused:
            return 'blocked'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt,ids[7:9]))==['blocked','saved']
    assert len(strategy.store.plans_for(account))==8


def test_real_trade_concurrent_request_id(ledger):
    svc,account,instrument=ledger
    order=trade(account,instrument,'concurrent-real-order')
    barrier=Barrier(2)
    def submit(_):
        barrier.wait(timeout=5)
        return svc.submit(order)
    with ThreadPoolExecutor(2) as pool:
        a,b=pool.map(submit,range(2))
    assert a==b
    with svc.db.read() as conn:
        for table in ('trade_executions','trade_lots','cash_movements'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==1


def test_ndx_cash_buffer_reference(ledger):
    svc,account,_=ledger
    fund(svc,account,amount_fen=20_000_000)
    strategy=service(svc.db)
    series=FakeData(rising_bars(220,breakout=160)).etf_series('NDX',completed_on='2026-11-24')
    plan=strategy._ndx_advice(account,'2026-11-24',get_config('NDX'),series,[])
    assert plan['status']=='CASH_BLOCKED' and plan['tradable'] is False
    assert plan['current_ratio']==0


def test_daily_retention_keeps_manual(cfg,tmp_path):
    root=tmp_path/'backups'; manual=create_backup(cfg,root/'manual')
    begin=datetime(2026,1,1,tzinfo=UTC)
    for i in range(12):
        result=daily_backup(cfg,root,now=begin+timedelta(days=i*7))
    assert result['kept']<=5 and manual.exists()
    again=daily_backup(cfg,root,now=begin+timedelta(days=77))
    assert again['snapshot']==result['snapshot'] and not again['removed']


def test_stock_only_sample(sources,tmp_path):
    stock,_=sources
    report=run_migration(stock,None,tmp_path/'stock-sandbox.sqlite',confirm=True)
    assert report['blocks_formal_takeover'] and report['stock']['migrated_trades']==4
    assert {p['code']:p['quantity'] for p in report['stock']['positions']}['510300']==1100
    assert report['stock']['known_securities_cash_fen']==6654500


def test_finance_only_sample_independent_statement_and_takeover(builder,tmp_path):
    stock=builder.build_stock(tmp_path/'empty-stock.db')
    with closing(sqlite3.connect(stock)) as conn:
        for name in ('kv_store','daily_snapshot','capital_history','daily_stock_snapshot'):
            conn.execute('DELETE FROM '+name)
        conn.commit()
    finance=builder.build_finance(tmp_path/'finance.db')
    with closing(sqlite3.connect(finance)) as conn:
        conn.execute('DELETE FROM finance_transaction WHERE id NOT IN (1,6)')
        conn.execute('DELETE FROM finance_account WHERE id NOT IN (3,4)')
        conn.execute('DELETE FROM finance_balance_sheet_snapshot')
        conn.execute("UPDATE finance_account SET updated_at='2026-02-11T00:00:00'")
        conn.commit()
    target=tmp_path/'sandbox.sqlite'
    report=run_migration(stock,finance,target,confirm=True)
    assert report['blocks_formal_takeover']
    assert query(target,'SELECT count(*) FROM finance_transfers')==[(1,)]
    assert query(target,'SELECT count(*) FROM balance_adjustments')==[(2,)]
    # Independently computed from fixture constants, never read from target values.
    evidence={'run_id':report['run_id'],'target_identity':report['target_identity'],
              'stock_sha256':report['source']['stock_sha256'],'finance_sha256':report['source']['finance_sha256'],
              'source_reference':'synthetic independent bank statement', 'valuation_at':'2026-02-11T12:00:00+08:00',
              'confirmed_account_mapping_and_dates':True,
              'expected':{'accounts':sorted([{'name':name,'kind':kind,'included':True,'effective_at':'2025-11-20T01:00:00.000000+00:00','balance_fen':bal} for name,kind,bal in [('招行储蓄卡','bank',1200000),('支付宝','payment',350000)]],key=lambda a:a['name']),
                          'positions':[],'totals':{'securities_cash_fen':0,'securities_market_value_fen':0,'securities_equity_fen':0,'total_assets_fen':1550000,'total_liabilities_fen':0,'net_worth_fen':1550000},
                          'counts':{'trade_executions':0,'finance_transactions':1,'finance_transfers':1},'trade_fees_fen':0,
                          'months':[{'period':'2025-11-23','income_fen':0,'expense_fen':8800,'fees_fen':0,'net_fen':-8800}]}}
    file=tmp_path/'evidence.json'; file.write_text(json.dumps(evidence),encoding='utf8')
    with pytest.raises(Refused): reconcile(target,file)
    evidence['expected']['totals']['net_worth_fen']+=1
    file.write_text(json.dumps(evidence),encoding='utf8')
    with pytest.raises(Refused): reconcile(target,file,confirm=True)
    evidence['expected']['totals']['net_worth_fen']-=1
    file.write_text(json.dumps(evidence),encoding='utf8')
    accepted=reconcile(target,file,confirm=True)
    assert accepted['blocks_formal_takeover'] is False
    report_file=write_report(accepted,tmp_path/'accepted.json')
    official=tmp_path/'official.sqlite'; binding=tmp_path/'official.json'
    old=initialize_database(official,role='formal',confirm=True,binding_path=binding)
    settings=DatabaseSettings(official,old.database_uuid,old.generation,official,binding_path=binding)
    token=takeover(settings,target,report_file)['approval']
    assert takeover(settings,target,report_file,approval=token,isolated=True)['ok']
    assert query(official,'SELECT count(*) FROM finance_transfers')==[(1,)]
