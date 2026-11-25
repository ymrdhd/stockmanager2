"""Deterministic supported legacy schemas; independent statement constants."""
from contextlib import closing
from datetime import datetime
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from test_migration import builder, query
from backend.application.migration import run_migration, target_database, write_report, Refused
from backend.application.migration_reconcile import reconcile, statement_projection
from backend.application.portfolio import PortfolioService, QuoteInput
from backend.config import AppConfig
from backend.infrastructure.database import DatabaseSettings, initialize_database, DatabaseError, open_database
from backend.infrastructure.takeover import takeover
from backend.main import create_app

CUTOFF = datetime.fromisoformat('2026-02-11T16:00:00+08:00')


@pytest.mark.parametrize('mixed,interrupt', [(False,False),(True,False),(True,True)])
def test_securities_statement_to_running_formal(builder, tmp_path, mixed, interrupt, monkeypatch):
    stock = builder.build_stock(tmp_path/'stock.db')
    # Cash 50,000 - 3,900 - 5 = 46,095; 1000 shares at independent close 4.20.
    with closing(sqlite3.connect(stock)) as conn:
        for table in ('kv_store', 'daily_snapshot', 'capital_history', 'daily_stock_snapshot'):
            conn.execute('DELETE FROM '+table)
        conn.execute('INSERT INTO kv_store VALUES(?,?)', ('trade_history', json.dumps([builder.TRADE_HISTORY[0]])))
        conn.execute('INSERT INTO daily_snapshot VALUES(?,?,?,?,?,?,?)', ('2025-11-28',0,50000,0,0,0,0))
        conn.commit()
    finance = None
    if mixed:
        finance = builder.build_finance(tmp_path/'finance.db')
        with closing(sqlite3.connect(finance)) as conn:
            conn.execute('DELETE FROM finance_transaction WHERE id NOT IN (1,6)')
            conn.execute('DELETE FROM finance_account WHERE id NOT IN (3,4)')
            conn.execute('DELETE FROM finance_balance_sheet_snapshot')
            conn.execute("UPDATE finance_account SET updated_at='2026-02-11T00:00:00'")
            conn.commit()
    target = tmp_path/'sandbox.sqlite'
    report = run_migration(stock, finance, target, confirm=True)
    assert report['blocks_formal_takeover']
    assert report['stock']['migrated_trades'] == 1
    # Fixture close is a separate synthetic market statement, not derived from target.
    with target_database(target) as (db, _, _):
        with db.read() as conn:
            instrument = conn.execute("SELECT id FROM instruments WHERE code='510300'").fetchone()[0]
        PortfolioService(db, clock=lambda:CUTOFF).quote(QuoteInput(request_id='fixture-close', instrument_id=instrument,
            price='4.20', source='synthetic independent fixture', observed_at=CUTOFF,
            trading_day='2026-02-11', kind='close', quality='valid'))
    accounts = [{'name':'证券账户','kind':'securities','included':True,'effective_at':'2025-11-27T16:00:00.000000+00:00',
                 'balance_fen':5029500,'cash_fen':4609500,'market_value_fen':420000,'equity_fen':5029500}]
    if mixed:
        accounts += [{'name':name,'kind':kind,'included':True,'effective_at':'2025-11-20T01:00:00.000000+00:00','balance_fen':bal}
                     for name,kind,bal in [('招行储蓄卡','bank',1200000),('支付宝','payment',350000)]]
    totals = {'securities_cash_fen':4609500,'securities_market_value_fen':420000,'securities_equity_fen':5029500,
              'total_assets_fen':5029500+(1550000 if mixed else 0),'total_liabilities_fen':0,'net_worth_fen':5029500+(1550000 if mixed else 0)}
    expected = {'accounts':sorted(accounts,key=lambda a:a['name']), 'positions':[{'account':'证券账户','code':'510300','quantity':1000,'remaining_cost_fen':390000,'remaining_fees_fen':500}],
                'totals':totals,'counts':{'trade_executions':1,'finance_transactions':int(mixed),'finance_transfers':int(mixed)},
                'trade_fees_fen':500,'months':[{'period':'2025-11-23','income_fen':0,'expense_fen':8800,'fees_fen':0,'net_fen':-8800}] if mixed else []}
    evidence = {'run_id':report['run_id'],'target_identity':report['target_identity'],'stock_sha256':report['source']['stock_sha256'],
                'finance_sha256':report['source']['finance_sha256'],'source_reference':'independent synthetic stock and bank statements',
                'valuation_at':CUTOFF.isoformat(),'confirmed_account_mapping_and_dates':True,'expected':expected}
    file=tmp_path/'evidence.json'
    expected['positions'][0]['quantity']=999
    file.write_text(json.dumps(evidence), encoding='utf8')
    with pytest.raises(Refused): reconcile(target,file,confirm=True)
    expected['positions'][0]['quantity']=1000
    file.write_text(json.dumps(evidence), encoding='utf8')
    accepted=reconcile(target,file,confirm=True)
    report_path=write_report(accepted,tmp_path/'accepted.json')
    official=tmp_path/'official.sqlite'; binding=tmp_path/'official.json'
    old=initialize_database(official,role='formal',confirm=True,binding_path=binding)
    settings=DatabaseSettings(official,old.database_uuid,old.generation,official,binding_path=binding)
    token=takeover(settings,target,report_path)['approval']
    if interrupt:
        from backend.infrastructure import takeover as implementation
        from backend.infrastructure.binding import load_binding
        from backend.infrastructure.takeover import rollback_pending,marker_path
        def fail(*args): raise RuntimeError('synthetic interrupted binding publication')
        with monkeypatch.context() as patch:
            patch.setattr(implementation,'_publish_binding',fail)
            with pytest.raises(RuntimeError): takeover(settings,target,report_path,approval=token,isolated=True)
        assert marker_path(binding).is_file()
        # The real application startup must refuse the ambiguous pair.
        with pytest.raises(DatabaseError), open_database(settings): pass
        try:
            with TestClient(create_app(AppConfig(market_auto_refresh=False),database=settings)) as blocked:
                assert blocked.get('/api/v1/finance/overview').status_code in (403,503)
        except DatabaseError:
            pass
        recovered=rollback_pending(binding,confirm=True,isolated=True)
        bound=load_binding(binding)
        recovered_settings=DatabaseSettings(official,bound.uuid,recovered['generation'],official,binding_path=binding)
        with TestClient(create_app(AppConfig(market_auto_refresh=False),database=recovered_settings)) as client:
            assert client.get('/api/v1/finance/overview').status_code==200
            with client.app.state.database.read() as conn:
                assert conn.execute('SELECT count(*) FROM trade_executions').fetchone()[0]==0
        assert not marker_path(binding).exists()
        return
    result=takeover(settings,target,report_path,approval=token,isolated=True)
    assert result['ok'] and result['generation']==old.generation+1
    with pytest.raises(DatabaseError), open_database(settings): pass
    current=DatabaseSettings(official,old.database_uuid,result['generation'],official,binding_path=binding)
    with TestClient(create_app(AppConfig(market_auto_refresh=False),database=current)) as client:
        actual,_,_=statement_projection(client.app.state.database,CUTOFF)
        assert actual==expected
        response=client.get('/api/v1/finance/overview',params={'as_of':CUTOFF.isoformat()})
        assert response.status_code==200
        assert {k:response.json()[k] for k in totals}==totals
    assert query(target,'SELECT instance_role FROM system_metadata')==[('sandbox',)]
