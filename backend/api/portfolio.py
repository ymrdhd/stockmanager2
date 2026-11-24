"""HTTP adapters for valuation and explicit, sourced inputs."""
from datetime import date, datetime
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from backend.api.trading import invoke, service
from backend.application.portfolio import CompanyInput, OpeningInput, PortfolioService, QuoteInput, QuoteResolutionInput

router=APIRouter(prefix='/api/v1',tags=['portfolio'])


def portfolio(request: Request) -> PortfolioService:
    return PortfolioService(service(request).db)


Service=Annotated[PortfolioService,Depends(portfolio)]


@router.post('/opening-positions')
def opening(data: OpeningInput,svc: Service):
    return invoke(svc.opening,data)


@router.post('/company-events')
def company(data: CompanyInput,svc: Service):
    return invoke(svc.company,data)


@router.post('/quotes')
def quote(data: QuoteInput,svc: Service):
    return invoke(svc.quote,data)


@router.post('/quotes/resolutions')
def resolve_close(data: QuoteResolutionInput,svc: Service):
    return invoke(svc.resolve_close,data)


@router.get('/quotes/{instrument_id}')
def quotes(instrument_id: str,as_of: datetime,svc: Service,quote_revision: int|None=None,
           available_at: datetime|None=None):
    return invoke(svc.quotes,instrument_id,as_of,quote_revision,available_at)


@router.get('/portfolio/{account_id}')
def valuation(account_id: str,as_of: datetime,svc: Service,previous_trading_day: date|None=None,quote_revision: int|None=None):
    return invoke(svc.valuation,account_id,as_of,previous_trading_day,quote_revision)
