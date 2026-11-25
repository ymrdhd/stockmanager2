"""Market status and explicit refresh; reads never trigger network writes."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.trading import invoke
from backend.application.market import MarketService

router = APIRouter(prefix='/api/v1',tags=['market'])


def market(request: Request) -> MarketService:
    service = getattr(request.app.state,'market',None)
    if service is None:
        raise HTTPException(503,'database unavailable')
    return service


Service = Annotated[MarketService,Depends(market)]


@router.get('/market/status')
def status(svc: Service,instrument_id: str|None=None):
    """Expose quote provenance per instrument plus the configured provider.

    Route path is unchanged: the desktop client already polls this URL. The
    per-item evidence fields are projected explicitly so the response contract
    never depends on how many columns a quote row happens to carry.
    """
    result = invoke(svc.status,instrument_id)
    items = []
    for item in result.get('items',[]):
        items.append({'instrument_id':item.get('instrument_id'),'code':item.get('code'),
            'market':item.get('market'),'quote':item.get('quote'),'quality':item.get('quality'),
            'source':item.get('source'),'price_basis':item.get('price_basis'),
            'kind':item.get('kind'),'observed_at':item.get('observed_at'),
            'acquired_at':item.get('acquired_at')})
    return {**result,'items':items,'provider':result.get('provider'),
            'provider_available':result.get('provider_available')}


@router.post('/market/refresh')
def refresh(svc: Service,instrument_id: str|None=None):
    return invoke(svc.refresh,instrument_id)


@router.get('/market/prices/{instrument_id}')
def price(instrument_id: str,svc: Service,as_of: datetime|None=None):
    return invoke(svc.portfolio.current_price,instrument_id,as_of or svc.clock())


@router.get('/strategy/evidence/{account_id}')
def strategy_evidence(account_id: str,svc: Service,as_of: datetime|None=None):
    return invoke(svc.portfolio.strategy_evidence,account_id,as_of or svc.clock())
