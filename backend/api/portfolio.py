"""HTTP adapters for valuation and explicit, sourced inputs."""
from datetime import date, datetime
from typing import Annotated
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from backend.api.trading import invoke, service
from backend.application.portfolio import (
    CompanyInput, OpeningCorrectionInput, OpeningInput, PortfolioService, QuoteInput, QuoteResolutionInput,
)

router=APIRouter(prefix='/api/v1',tags=['portfolio'])


def portfolio(request: Request) -> PortfolioService:
    return PortfolioService(service(request).db)


Service=Annotated[PortfolioService,Depends(portfolio)]


class VoidInput(BaseModel):
    request_id: str = Field(min_length=1,max_length=128)
    reason: str = Field(min_length=1,max_length=2000)


@router.post('/opening-positions')
def opening(data: OpeningInput,svc: Service):
    return invoke(svc.opening,data)


@router.get('/openings')
def openings(svc: Service,account_id: str|None=None,
             limit: Annotated[int,Query(ge=1,le=500)]=100,
             offset: Annotated[int,Query(ge=0,le=10**9)]=0):
    result=invoke(svc.openings,account_id,limit,offset)
    result['source']='sqlite_ledger'
    result['quality']='derived_unverified' if result['data_incomplete'] else 'valid'
    result['as_of']=datetime.now().astimezone().isoformat()
    return result


@router.post('/portfolio/openings/{opening_id}/correct')
def correct_opening(opening_id: str,data: OpeningCorrectionInput,svc: Service):
    return invoke(svc.correct_opening,data,opening_id)


@router.post('/portfolio/openings/{opening_id}/void')
def void_opening(opening_id: str,data: VoidInput,svc: Service):
    return invoke(svc.void_opening,opening_id,data.request_id,data.reason)


@router.post('/portfolio/company-events/{event_id}/void')
def void_company_event(event_id: str,data: VoidInput,svc: Service):
    return invoke(svc.void_company_event,event_id,data.request_id,data.reason)


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


@router.get('/portfolio/{account_id}/month')
def monthly_pnl(account_id: str, month: date, svc: Service):
    return invoke(svc.monthly_pnl,account_id,month)
