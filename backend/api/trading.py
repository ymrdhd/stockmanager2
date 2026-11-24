"""HTTP adapter; business rules live exclusively in TradeService."""
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.application.trading.service import AccountInput, CashInput, Conflict, InstrumentInput, Missing, TradeError, TradeInput, TradeService
from backend.infrastructure.database import DatabaseError

router = APIRouter(prefix='/api/v1', tags=['trading'])


def service(request: Request) -> TradeService:
    db = getattr(request.app.state, 'database', None)
    if db is None:
        raise HTTPException(503, 'database unavailable')
    return TradeService(db)


Service = Annotated[TradeService, Depends(service)]


def wire(value: Any) -> Any:
    """Keep exact fixed-point values across the JSON/JavaScript boundary."""
    if isinstance(value, list):
        return [wire(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if item is not None and key in ('price', 'frozen_atr'):
            result[key] = format(Decimal(item) / Decimal(10000), '.4f')
        elif item is not None and key in ('quantity', 'opened_qty', 'remaining_qty'):
            result[key] = str(item)
        elif key == 'requested_allocations' and item is not None:
            result[key] = {lot: str(quantity) for lot, quantity in item.items()}
        elif key.endswith('_fen') and type(item) is int and abs(item) > 2**53 - 1:
            result[key] = str(item)
        else:
            result[key] = wire(item)
    return result


def invoke(operation, *args):
    request_id = next((getattr(arg, 'request_id', None) for arg in args if getattr(arg, 'request_id', None)), None)
    try:
        return wire(operation(*args))
    except Conflict as exc:
        raise HTTPException(409, {'code': 'conflict', 'message': str(exc), 'request_id': request_id, 'details': {}}) from exc
    except Missing as exc:
        raise HTTPException(404, {'code': 'not_found', 'message': str(exc), 'request_id': request_id, 'details': {}}) from exc
    except TradeError as exc:
        raise HTTPException(422, {'code': 'invalid_input', 'message': str(exc), 'request_id': request_id, 'details': {}}) from exc
    except DatabaseError as exc:
        raise HTTPException(403, {'code': 'database_refused', 'message': str(exc), 'request_id': request_id, 'details': {}}) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, {'code': 'constraint_conflict', 'message': 'database constraint rejected operation', 'request_id': request_id, 'details': {}}) from exc


@router.post('/accounts')
def account(data: AccountInput, svc: Service):
    return invoke(svc.create_account, data)


@router.post('/instruments')
def instrument(data: InstrumentInput, svc: Service):
    return invoke(svc.create_instrument, data)


@router.post('/trades')
def trade(data: TradeInput, svc: Service):
    return invoke(svc.submit, data)


@router.get('/trades/{id}')
def get_trade(id: str, svc: Service):
    return invoke(svc.get, id)


@router.post('/trades/{id}/corrections')
def correction(id: str, data: TradeInput, svc: Service):
    return invoke(svc.correct, id, data)


class ConfirmInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)


@router.post('/trades/{id}/confirm')
def confirm(id: str, data: ConfirmInput, svc: Service):
    return invoke(svc.confirm_pending, id, data.request_id)


@router.post('/cash')
def cash(data: CashInput, svc: Service):
    return invoke(svc.cash, data)


def reader(name: str):
    def endpoint(svc: Service, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                 offset: Annotated[int, Query(ge=0, le=10**9)] = 0,
                 ledger_revision: Annotated[int | None, Query(ge=0)] = None):
        result = invoke(svc.list_records, name, limit, offset, ledger_revision)
        result['source'] = 'sqlite_ledger'
        result['quality'] = 'derived_unverified' if result['data_incomplete'] else 'valid'
        result['as_of'] = datetime.now(UTC).isoformat()
        return result
    return endpoint


for name in ('accounts', 'instruments', 'trades', 'pending', 'lots', 'allocations', 'cash'):
    router.add_api_route('/' + name, reader(name), methods=['GET'], name='list_' + name)


class VoidInput(ConfirmInput):
    reason: str = Field(min_length=1,max_length=2000)


@router.post('/trades/{id}/void')
def void(id: str,data: VoidInput,svc: Service):
    return invoke(svc.void,id,data.request_id,data.reason)
