"""Finance HTTP adapter; all facts written through the finance application service."""
from datetime import UTC, date, datetime
from typing import Annotated
from fastapi import APIRouter, Depends, Query, Request
from backend.api.trading import invoke, service
from backend.application.finance import (
    FinanceService, AccountUpdate, BalanceInput, BillInput, CategoryInput,
    TransferInput, CashCorrection, VoidInput, month_start, TransferReconciliation, AccountScope, AccountEffective,
    LiquidityInput,
)
from backend.application.portfolio import day_of

router = APIRouter(prefix='/api/v1', tags=['finance'])


def finance(request: Request) -> FinanceService:
    return FinanceService(service(request).db)


Service = Annotated[FinanceService, Depends(finance)]


@router.patch('/accounts/{id}')
def update_account(id: str, data: AccountUpdate, svc: Service):
    return invoke(svc.update_account,id,data)


@router.post('/finance/balances')
def balance(data: BalanceInput, svc: Service):
    return invoke(svc.balance,data)


@router.post('/finance/categories')
def category(data: CategoryInput, svc: Service):
    return invoke(svc.category,data)


@router.post('/finance/bills')
def bill(data: BillInput, svc: Service):
    return invoke(svc.bill,data)


@router.post('/finance/bills/{id}/corrections')
def correct_bill(id: str, data: BillInput, svc: Service):
    return invoke(svc.change_bill,id,data)


@router.post('/finance/bills/{id}/void')
def void_bill(id: str, data: VoidInput, svc: Service):
    return invoke(svc.change_bill,id,data)


@router.post('/finance/transfers')
def transfer(data: TransferInput, svc: Service):
    return invoke(svc.transfer,data)


@router.post('/finance/transfers/{id}/corrections')
def correct_transfer(id: str, data: TransferInput, svc: Service):
    return invoke(svc.change_transfer,id,data)


@router.post('/finance/transfers/{id}/void')
def void_transfer(id: str, data: VoidInput, svc: Service):
    return invoke(svc.change_transfer,id,data)


@router.post('/cash/{id}/corrections')
def correct_cash(id: str, data: CashCorrection, svc: Service):
    return invoke(svc.change_cash,id,data)


@router.post('/cash/{id}/void')
def void_cash(id: str, data: VoidInput, svc: Service):
    return invoke(svc.change_cash,id,data)


@router.post('/company-events/{id}/corrections')
def correct_dividend(id: str, data: CashCorrection, svc: Service):
    return invoke(svc.change_dividend,id,data)


@router.post('/company-events/{id}/void')
def void_dividend(id: str, data: VoidInput, svc: Service):
    return invoke(svc.change_dividend,id,data)


@router.get('/finance/overview')
def overview(svc: Service, as_of: datetime | None=None, quote_revision: int | None=None):
    return invoke(svc.overview,as_of or datetime.now(UTC),quote_revision)


@router.get('/finance/net-worth-trend')
def net_worth_trend(svc: Service, months: int=12, as_of: datetime | None=None):
    return invoke(svc.net_worth_trend,months,as_of or datetime.now(UTC))


@router.get('/finance/month')
def month(svc: Service, period: date | None=None, as_of: datetime | None=None):
    at=as_of or datetime.now(UTC)
    return invoke(svc.month,period or month_start(day_of(at)),at)


@router.get('/finance/year/{year}')
def year(year: int, svc: Service, as_of: datetime | None=None):
    return invoke(svc.year,year,as_of or datetime.now(UTC))


def reader(kind: str):
    def endpoint(svc: Service, limit: Annotated[int, Query(ge=1,le=500)]=100,
                 offset: Annotated[int, Query(ge=0,le=10**9)]=0, account_id: str | None=None,
                 from_at: datetime | None=None, to_at: datetime | None=None,
                 ledger_revision: Annotated[int | None, Query(ge=0)]=None):
        return invoke(svc.records,kind,limit,offset,account_id,from_at,to_at,ledger_revision)
    return endpoint


for name in ('balances','categories','bills','transfers'):
    router.add_api_route('/finance/'+name,reader(name),methods=['GET'],name='finance_list_'+name)

router.add_api_route('/company-events',reader('company-events'),methods=['GET'],name='list_company_events')


@router.post('/accounts/{id}/scope')
def account_scope(id: str, data: AccountScope, svc: Service):
    return invoke(svc.set_account_scope,id,data)


@router.post('/accounts/{id}/liquidity')
def account_liquidity(id: str, data: LiquidityInput, svc: Service):
    return invoke(svc.set_account_liquidity,id,data)


@router.post('/accounts/{id}/effective-date')
def account_effective(id: str, data: AccountEffective, svc: Service):
    return invoke(svc.confirm_account_effective,id,data)


@router.post('/finance/transfers/{id}/reconciliation')
def reconcile_transfer(id: str, data: TransferReconciliation, svc: Service):
    return invoke(svc.reconcile_transfer,id,data)


@router.get('/finance/audit/{kind}/{id}')
def audit_detail(kind: str, id: str, svc: Service, ledger_revision: Annotated[int | None, Query(ge=0)]=None):
    return invoke(svc.record_detail,kind,id,ledger_revision)
