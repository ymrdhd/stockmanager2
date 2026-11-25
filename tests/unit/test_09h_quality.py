"""Read-only amount evidence is based on the same authoritative snapshot."""
from backend.application.finance import FinanceService
from backend.application.portfolio import PortfolioService
from test_trading import cfg, ledger, trade
from test_finance import manual, balance, bill
from test_pnl import at, cash, quote


def test_overview_amount_evidence_is_exact_and_keeps_unknowns(ledger):
    svc,a,i=ledger
    finance=FinanceService(svc.db)
    p=PortfolioService(svc.db)
    bank=manual(svc,'09h-bank')
    balance(finance,bank,100000)
    cash(svc,a,'cash-opening','opening',300000,5)
    svc.submit(trade(a,i,'purchase',day=5,quantity=100,price='10',fees_fen=0))
    before=finance.overview(at(6))
    assert before['net_worth_fen'] is None
    by_field={x['field']:x for x in before['amount_evidence']}
    assert by_field['net_worth_fen']['amount_fen'] is None
    assert by_field['net_worth_fen']['quality']=='unavailable'
    assert by_field['securities_cash_fen']['amount_fen']==200000
    assert by_field['securities_cash_fen']['quality']=='valid'
    assert by_field['securities_market_value_fen']['amount_fen'] is None
    sec=next(r for r in before['accounts'] if r['id']==a)
    assert sec['quote_evidence'][0]['quote_id'] is None
    assert sec['cash_baseline']['amount_fen']==300000
    b=next(r for r in before['accounts'] if r['id']==bank)
    assert b['balance_id'] and b['balance_source']=='manual_balance_snapshot'
    quote(p,i,5,10)
    quote(p,i,6,12)
    after=finance.overview(at(6))
    assert after['net_worth_fen']==420000
    for item in after['amount_evidence']:
        assert item['ledger_revision']==after['ledger_revision']
        assert item['quote_revision']==after['quote_revision']
        assert item['as_of']==after['as_of']
        expected=after['finance_month']['bill_cash_flow_fen'] if item['field']=='finance_month.bill_cash_flow_fen' else after[item['field']]
        assert item['amount_fen']==expected
    sec=next(r for r in after['accounts'] if r['id']==a)
    q=sec['quote_evidence'][0]
    assert q['quote_id'] and q['quote_at'] and q['quote_source']
    assert q['market_value_fen']==120000 and q['quality']=='valid'
    assert finance.overview(at(6))==after
