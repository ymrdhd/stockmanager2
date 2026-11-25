"""Synthetic accepted-report contract tests, not evidence that legacy reconciliation passes."""
import json
import sqlite3
from contextlib import closing

import pytest

from backend.application.migration import MIGRATION_PROTOCOL, SELF_SCHEMA
from backend.infrastructure.database import (DatabaseError, DatabaseSettings, initialize_database,
                                              open_database, writer_lock)
from backend.infrastructure.takeover import digest, marker_path, rollback_pending, takeover
from backend.infrastructure.binding import load_binding
from backend.infrastructure import takeover as implementation


@pytest.fixture
def accepted(tmp_path):
    official = tmp_path/'official.sqlite'
    binding = tmp_path/'official.json'
    old = initialize_database(official, role='formal', confirm=True, binding_path=binding)
    target = tmp_path/'sandbox.sqlite'
    new = initialize_database(target, role='sandbox', confirm=True, official_path=official, binding_path=binding)
    source = tmp_path/'source.db'
    with closing(sqlite3.connect(source)) as conn:
        conn.execute('CREATE TABLE synthetic(id INT)'); conn.commit()
    report = {'run_id':'accepted-fixture', 'target':str(target), 'dry_run':False,
              'migration_protocol':MIGRATION_PROTOCOL, 'blocks_formal_takeover':False,
              'target_identity':{'uuid':new.database_uuid,'generation':new.generation,'schema_version':new.schema_version},
              'source':{'stock_path':str(source),'stock_sha256':digest(source)},
              'formal_projection':{'ledger_revision':0,'quote_revision':0,'source_valuation_at':'2026-01-01T00:00:00+00:00','source_valuation_status':'operator_statement_matched'},
              'reconciliation':{'evidence_sha256':'synthetic-contract-only'}, 'differences':[], 'migration_tool_errors':[], 'stock':{'pending_trades_detail':[]}}
    with closing(sqlite3.connect(target)) as conn:
        for ddl in SELF_SCHEMA: conn.execute(ddl)
        conn.execute('INSERT INTO migration_runs(id,source_kind,stock_sha256,stock_path,mode,target_role,status,started_at,summary) VALUES(?,?,?,?,?,?,?,?,?)',
                     ('accepted-fixture','stock',digest(source),str(source),'migrate','sandbox','completed','2026-01-01',json.dumps(report)))
        conn.commit()
    file = tmp_path/'report.json'; file.write_text(json.dumps(report))
    return DatabaseSettings(official,old.database_uuid,1,official,binding_path=binding),target,file,source


def test_success_and_old_generation_refused(accepted):
    settings,target,report,_=accepted
    token=takeover(settings,target,report)['approval']
    result=takeover(settings,target,report,approval=token,isolated=True)
    assert result['ok'] and result['generation']==2
    assert load_binding(settings.binding_path).generation==2
    with pytest.raises(DatabaseError), open_database(settings): pass
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute('SELECT instance_role FROM system_metadata').fetchone()==('sandbox',)


def test_no_confirmation_and_changed_report(accepted):
    settings,target,report,_=accepted
    before=digest(settings.path)
    token=takeover(settings,target,report)['approval']
    with pytest.raises(DatabaseError): takeover(settings,target,report,approval=token)
    report.write_text(report.read_text()+' ')
    with pytest.raises(DatabaseError): takeover(settings,target,report,approval=token,isolated=True)
    assert digest(settings.path)==before


def test_active_writer_refused(accepted):
    settings,target,report,_=accepted
    token=takeover(settings,target,report)['approval']
    with writer_lock(settings.path), pytest.raises(DatabaseError):
        takeover(settings,target,report,approval=token,isolated=True)


def test_source_changed_refused(accepted):
    settings,target,report,source=accepted
    token=takeover(settings,target,report)['approval']
    with closing(sqlite3.connect(source)) as conn:
        conn.execute('INSERT INTO synthetic VALUES(1)'); conn.commit()
    with pytest.raises(DatabaseError, match='source changed'):
        takeover(settings,target,report,approval=token,isolated=True)


@pytest.mark.parametrize('after_binding',[False,True])
def test_interrupted_switch_fenced_then_rollback(accepted,monkeypatch,after_binding):
    settings,target,report,_=accepted
    token=takeover(settings,target,report)['approval']
    publish=implementation._publish_binding
    def interrupt(*args):
        if after_binding: publish(*args)
        raise RuntimeError('synthetic interruption')
    with monkeypatch.context() as patch:
        patch.setattr(implementation,'_publish_binding',interrupt)
        with pytest.raises(RuntimeError): takeover(settings,target,report,approval=token,isolated=True)
    assert marker_path(settings.binding_path).is_file()
    bound=load_binding(settings.binding_path)
    current=DatabaseSettings(settings.path,bound.uuid,bound.generation,settings.path,binding_path=settings.binding_path)
    with pytest.raises(DatabaseError,match='unfinished takeover'), open_database(current): pass
    result=rollback_pending(settings.binding_path,confirm=True,isolated=True)
    assert result['generation']==3
    recovered=DatabaseSettings(settings.path,bound.uuid,3,settings.path,binding_path=settings.binding_path)
    with open_database(recovered) as db, db.read() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='migration_runs'").fetchone()
    assert not marker_path(settings.binding_path).exists()
