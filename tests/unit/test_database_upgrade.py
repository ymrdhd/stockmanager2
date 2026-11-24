"""Formal binding and real 0001 → 0002 migration safety regressions."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from backend.infrastructure import database
from backend.infrastructure.backup import create_backup
from backend.infrastructure.binding import Binding, publish_binding
from backend.infrastructure.database import (REVISION, SCHEMA_VERSION, DatabaseError, DatabaseSettings, Identity, adopt_database,
    initialize_database, migrate_schema, open_database, upgrade_database)
from test_database import isolated_install


def cfg_for(path, identity, binding):
    return DatabaseSettings(path, identity.database_uuid, identity.generation, path, binding_path=binding)


def test_iss006_fixed_binding_cli_and_service(tmp_path):
    install=isolated_install(tmp_path)
    cmd=[sys.executable,str(install/'start.py')]
    a=tmp_path/'a.sqlite'
    b=tmp_path/'b.sqlite'
    binding=install/'data'/'official.json'
    second_binding=tmp_path/'independent'/'official.json'
    aid=initialize_database(a,role='formal',confirm=True,binding_path=binding)
    bid=initialize_database(b,role='formal',confirm=True,binding_path=second_binding)
    original=binding.read_bytes()
    with pytest.raises(DatabaseError):
        initialize_database(tmp_path/'third.sqlite',role='formal',confirm=True,binding_path=binding)
    assert not (tmp_path/'third.sqlite').exists()
    assert binding.read_bytes()==original
    config=cfg_for(b,bid,binding)
    with pytest.raises(DatabaseError,match='fixed formal binding'),open_database(config):
        pytest.fail('other formal database accepted')
    with pytest.raises(DatabaseError):
        create_backup(config,tmp_path/'wrong-backup')
    with pytest.raises(DatabaseError):
        adopt_database(config,confirm=True)
    for command in ('check-database','server','backup','upgrade-database'):
        args=['--database',str(b),'--uuid',bid.database_uuid,'--generation','1']
        if command in ('backup','upgrade-database'):
            args+=['--destination',str(tmp_path/'wrong-backup')]
        if command=='upgrade-database': args+=['--confirm']
        r=subprocess.run(cmd+[command]+args,capture_output=True,text=True,timeout=10)
        assert r.returncode!=0 and 'formal' in r.stderr, r.stderr
    r=subprocess.run(cmd+['check-database'],capture_output=True,text=True,check=True,timeout=10)
    assert json.loads(r.stdout)['database_uuid']==aid.database_uuid
    sandbox=tmp_path/'sandbox.sqlite'
    initialize_database(sandbox,role='sandbox',confirm=True,binding_path=binding)
    assert binding.read_bytes()==original
    binding.write_text('{broken')
    with pytest.raises(DatabaseError,match='missing or invalid'),open_database(cfg_for(a,aid,binding)):
        pytest.fail('bad binding accepted')
    with pytest.raises(DatabaseError):
        initialize_database(tmp_path/'new.sqlite',role='formal',confirm=True,binding_path=binding)
    assert binding.read_text()=='{broken'


def test_iss007_read_snapshot_across_concurrent_commit(tmp_path):
    p=tmp_path/'db.sqlite'; binding=tmp_path/'official.json'
    identity=initialize_database(p,role='formal',confirm=True,binding_path=binding)
    with open_database(cfg_for(p,identity,binding)) as db:
        with db.transaction() as c:
            c.execute('CREATE TABLE sample(x)')
            c.execute('INSERT INTO sample VALUES(1)')
        with db.read() as reader:
            assert reader.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(0,)
            assert reader.execute('SELECT * FROM sample').fetchall()==[(1,)]
            with db.transaction() as writer:
                writer.execute('INSERT INTO sample VALUES(2)')
                writer.execute('UPDATE ledger_state SET ledger_revision=1')
            assert reader.execute('SELECT * FROM sample').fetchall()==[(1,)]
            assert reader.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(0,)
        with db.read() as reader:
            assert reader.execute('SELECT * FROM sample').fetchall()==[(1,),(2,)]
            assert reader.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(1,)


def legacy(tmp_path):
    p=tmp_path/'old.sqlite'
    identity=replace(Identity.new('formal'),schema_version=1)
    engine=create_engine(URL.create('sqlite',database=str(p)))
    try: migrate_schema(engine,identity,target='0001')
    finally: engine.dispose()
    with closing(sqlite3.connect(p)) as c,c:
        c.execute('CREATE TABLE original(x INTEGER)')
        c.execute('INSERT INTO original VALUES(42)')
    cfg=cfg_for(p,identity,tmp_path/'official.json')
    assert adopt_database(cfg,confirm=True)==identity
    return cfg


def test_upgrade_0001_to_latest_with_backup(tmp_path):
    cfg=legacy(tmp_path)
    with pytest.raises(DatabaseError),open_database(cfg):
        pytest.fail('old schema accepted for service')
    dest=tmp_path/'pre-upgrade'
    upgrade_database(cfg,dest,confirm=True)
    with open_database(cfg) as db,db.read() as c:
        assert db.identity.schema_version==SCHEMA_VERSION
        assert c.execute('SELECT version_num FROM alembic_version').fetchone()==(REVISION,)
        assert c.execute('SELECT * FROM original').fetchall()==[(42,)]
        assert c.execute('SELECT ledger_revision FROM ledger_state').fetchone()==(0,)
        assert c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
    with closing(sqlite3.connect(dest/'database.sqlite')) as c:
        assert c.execute('SELECT schema_version,instance_role FROM system_metadata').fetchone()==(1,'backup')
        assert c.execute('SELECT * FROM original').fetchall()==[(42,)]
    assert json.loads((dest/'manifest.json').read_text())['schema_version']==1
    with pytest.raises(DatabaseError):
        adopt_database(cfg_for(dest/'database.sqlite',replace(Identity.new('formal'),database_uuid=cfg.expected_uuid),tmp_path/'other'/'official.json'),confirm=True)


def test_upgrade_failure_rolls_back_ddl_metadata_and_original(tmp_path,monkeypatch):
    cfg=legacy(tmp_path)
    original_factory=database.create_engine
    def failing_engine(*a,**kw):
        engine=original_factory(*a,**kw)
        @event.listens_for(engine,'after_cursor_execute')
        def fail(conn,cursor,statement,parameters,context,executemany):
            if 'CREATE TABLE trade_lots' in statement:
                raise RuntimeError('injected upgrade failure after actual DDL')
        return engine
    monkeypatch.setattr(database,'create_engine',failing_engine)
    dest=tmp_path/'failed-upgrade-backup'
    with pytest.raises(RuntimeError,match='injected upgrade failure'):
        upgrade_database(cfg,dest,confirm=True)
    assert (dest/'manifest.json').is_file()
    with closing(sqlite3.connect(cfg.path)) as c:
        assert c.execute('SELECT schema_version FROM system_metadata').fetchone()==(1,)
        assert c.execute('SELECT version_num FROM alembic_version').fetchone()==('0001',)
        assert c.execute('SELECT * FROM original').fetchall()==[(42,)]
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}=={'original','system_metadata','alembic_version'}
        assert c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_explicit_cli_upgrade_and_maintenance_lock(tmp_path):
    cfg=legacy(tmp_path)
    install=isolated_install(tmp_path)
    (install/'data').mkdir()
    # Explicit adoption is required in a fresh installation, and must validate old identity.
    cmd=[sys.executable,str(install/'start.py')]
    args=['--database',str(cfg.path),'--uuid',cfg.expected_uuid,'--generation','1']
    check=subprocess.run(cmd+['check-database']+args,capture_output=True,text=True,timeout=10)
    assert check.returncode!=0 and 'binding missing' in check.stderr
    subprocess.run(cmd+['adopt-database']+args+['--confirm'],capture_output=True,text=True,check=True,timeout=10)
    dest=tmp_path/'cli-backup'
    with database.writer_lock(cfg.path):
        rejected=subprocess.run(cmd+['upgrade-database','--confirm','--destination',str(dest)],capture_output=True,text=True,timeout=10)
        assert rejected.returncode!=0 and 'writer lock' in rejected.stderr
        assert not dest.exists()
    subprocess.run(cmd+['upgrade-database','--confirm','--destination',str(dest)],capture_output=True,text=True,check=True,timeout=10)
    result=subprocess.run(cmd+['check-database'],capture_output=True,text=True,check=True,timeout=10)
    assert json.loads(result.stdout)['schema_version']==SCHEMA_VERSION
    assert (dest/'manifest.json').exists()
