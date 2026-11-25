"""Explicit statement reconciliation for an offline migration sandbox.

Never generates source evidence from the target. The operator supplies independent
same-cutoff statement values in integer fen and confirms the source/date mapping.
Only the listed evidence gaps can be resolved by this workflow; discrepancies in
actual facts require a fresh corrected migration, not a report override.
"""
from datetime import datetime
import json
from pathlib import Path

from backend.application.finance import FinanceService, month_start
from backend.application.migration import (MIGRATION_PROTOCOL, Refused, open_source,
    read_target_state, reference_clock, sha256_of, target_database, target_guard, SHANGHAI)

RESOLVABLE = {'formal_valuation_not_reconciled', 'securities_market_value_unverified',
              'securities_account_date_inferred', 'account_effective_date_from_fact','opening_fees_unverified'}
TOTALS = ('securities_cash_fen','securities_market_value_fen','securities_equity_fen',
          'total_assets_fen','total_liabilities_fen','net_worth_fen')


def statement_projection(db, cutoff):
    state = read_target_state(db)
    service = FinanceService(db, reference_clock)
    overview = service.overview(cutoff)
    # Costs use current remaining lots; refuse a cutoff before any effective fact.
    at = overview['as_of']
    accounts = {a['id']: a['name'] for a in state['accounts']}
    positions = {}
    with db.read() as conn:
        for table, column in [('accounts','effective_at'),('trade_executions','executed_at'),
                              ('opening_positions','occurred_at'),('cash_movements','occurred_at'),
                              ('balance_adjustments','occurred_at'),('finance_transactions','occurred_at'),
                              ('finance_transfers','occurred_at')]:
            if conn.execute(f'SELECT 1 FROM {table} WHERE {column}>? LIMIT 1',(at,)).fetchone():
                raise Refused('statement cutoff precedes a recorded fact; research replay not supported')
        counts = {table: conn.execute(f"SELECT count(*) FROM {table} WHERE status='effective'").fetchone()[0]
                  for table in ('trade_executions','finance_transactions','finance_transfers')}
        fees = conn.execute("SELECT COALESCE(sum(fees_fen),0) FROM trade_executions WHERE status='effective'").fetchone()[0]
        mapping = conn.execute('SELECT source_kind,count(*) FROM migration_ledger GROUP BY source_kind').fetchall()
    for lot in state['lots']:
        if lot['status'] != 'open' or not lot['remaining_qty']:
            continue
        key = (accounts[lot['account_id']],state['instruments'][lot['instrument_id']]['code'])
        row = positions.setdefault(key, {'account':key[0],'code':key[1],'quantity':0,'remaining_cost_fen':0,'remaining_fees_fen':0})
        for key1,key2 in [('quantity','remaining_qty'),('remaining_cost_fen','remaining_cost_fen'),('remaining_fees_fen','remaining_fees_fen')]:
            row[key1] += lot[key2]
    periods = sorted({month_start(datetime.fromisoformat(row['occurred_at']).astimezone(SHANGHAI).date()).isoformat() for row in state['bills']})
    months = [{k:v for k,v in service.month(datetime.fromisoformat(period).date(),cutoff).items()
               if k in ('period','income_fen','expense_fen','fees_fen','net_fen')} for period in periods]
    fields = ('name','kind','included','effective_at','balance_fen')
    account_rows = [{**{k:a[k] for k in fields}, **({k:a[k] for k in ('cash_fen','market_value_fen','equity_fen')} if a['kind']=='securities' else {})} for a in overview['accounts']]
    result = {'accounts': sorted(account_rows,key=lambda a:a['name']), 'positions': [positions[k] for k in sorted(positions)],
              'totals':{k:overview[k] for k in TOTALS}, 'counts':counts, 'trade_fees_fen':fees,'months':months}
    if overview['quality'] != 'valid' or any(a['quality']!='valid' or a['effective_date_quality']!='valid' for a in overview['accounts']):
        raise Refused('formal projection contains unknown values')
    return result, overview, dict(mapping)


def reconcile(target: Path, evidence_path: Path, *, confirm=False):
    if not confirm:
        raise Refused('explicit statement confirmation required')
    target = target_guard(target,'sandbox')
    if not target.is_file():
        raise Refused('existing migration sandbox required')
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    if not isinstance(evidence.get('source_reference'),str) or not evidence['source_reference'].strip():
        raise Refused('independent source statement reference required')
    cutoff = datetime.fromisoformat(evidence['valuation_at'])
    if cutoff.tzinfo is None or cutoff > reference_clock():
        raise Refused('explicit past/current timezone-aware cutoff required')
    with target_database(target) as (db, identity, _):
        with db.read() as conn:
            row = conn.execute('SELECT id,status,summary FROM migration_runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
            if not row or row[1]!='completed' or conn.execute("SELECT 1 FROM migration_runs WHERE status!='completed'").fetchone():
                raise Refused('completed migration required')
        report = json.loads(row[2])
        if report['migration_protocol'] != MIGRATION_PROTOCOL or report['dry_run'] or report['run_id']!=row[0]:
            raise Refused('migration protocol or run mismatch')
        if evidence.get('run_id') != row[0] or evidence.get('target_identity') != report['target_identity'] or report['target_identity'] != {'uuid':identity.database_uuid,'generation':identity.generation,'schema_version':identity.schema_version}:
            raise Refused('statement not bound to this run and target')
        if report['stock']['pending_trades_detail'] or report['migration_tool_errors'] or any(d['blocks_takeover'] and d['kind'] not in RESOLVABLE for d in report['differences']):
            raise Refused('unresolved key differences or pending facts; no override')
        for kind in ('stock','finance'):
            path = report['source'][kind+'_path']; sha = report['source'][kind+'_sha256']
            if evidence.get(kind+'_sha256') != sha:
                raise Refused('statement source hash mismatch')
            if path:
                with open_source(Path(path)):
                    if sha256_of(Path(path)) != sha:
                        raise Refused('source changed')
        actual, overview, mapping = statement_projection(db,cutoff)
        # Canonical JSON distinguishes true from 1 and disallows missing or extra fields.
        canonical = lambda x: json.dumps(x,sort_keys=True,ensure_ascii=False,allow_nan=False)
        if canonical(evidence.get('expected')) != canonical(actual):
            raise Refused('source statement differs from formal projection; no takeover')
        if evidence.get('confirmed_account_mapping_and_dates') is not True:
            raise Refused('explicit independent account and date confirmation required')
        from hashlib import sha256
        resolved = []
        for d in report['differences']:
            if d['blocks_takeover']:
                resolved.append(dict(d))
                d['blocks_takeover'] = False
                d['resolution'] = 'independent statement and explicit account/date confirmation'
        report['reconciliation'] = {'evidence_sha256':sha256(evidence_bytes).hexdigest(), 'source_reference':evidence['source_reference'],
             'expected':evidence['expected'],'resolved_differences':resolved,'mapping_counts':mapping,'confirmed_at':reference_clock().isoformat()}
        report['formal_projection'].update({'valuation_at':overview['as_of'],'source_valuation_at':overview['as_of'],
            'source_valuation_status':'operator_statement_matched','ledger_revision':overview['ledger_revision'],'quote_revision':overview['quote_revision'],
            'overview':{k:overview[k] for k in report['formal_projection']['overview']},'months':actual['months'],
            'effective_trade_count':actual['counts']['trade_executions'],'effective_trade_fees_fen':actual['trade_fees_fen']})
        report['reconciliation']['original_finance_summary'] = dict(report['finance'])
        report['finance'].update({k:overview[k] for k in TOTALS})
        report['finance']['quality'] = overview['quality']
        report['blocks_formal_takeover'] = False
        with db.transaction() as conn:
            if (conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0],conn.execute('SELECT quote_revision FROM quote_state').fetchone()[0]) != (overview['ledger_revision'],overview['quote_revision']):
                raise Refused('target changed during reconciliation')
            if conn.execute('SELECT summary FROM migration_runs WHERE id=?',(row[0],)).fetchone()[0] != row[2]:
                raise Refused('migration report changed')
            conn.execute('UPDATE migration_runs SET summary=? WHERE id=?',(json.dumps(report,ensure_ascii=False,sort_keys=True),row[0]))
        return report
