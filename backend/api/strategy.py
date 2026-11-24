"""V9/G2 strategy read models and an explicit, idempotent evaluation endpoint.

这里没有任何"下单"能力：``POST /strategy/refresh`` 只会重算并记录信号/计划证据，
重复调用因唯一键而幂等。真实成交始终只能经 ``/api/v1/trades`` 由用户确认。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.application.strategy import SeriesError, StrategyService
from backend.application.strategy.series import BASIS_LABELS, ETF_ADJUSTED, INDEX_BASIS
from backend.domain.strategy.config import INDEX_STRATEGY_VERSION, all_configs
from backend.domain.strategy.symbols import configured_index_ids

router = APIRouter(prefix='/api/v1', tags=['strategy'])


def strategy(request: Request) -> StrategyService:
    service = getattr(request.app.state, 'strategy', None)
    if service is None:
        raise HTTPException(503, 'database unavailable')
    return service


Service = Annotated[StrategyService, Depends(strategy)]


def _unavailable(exc: SeriesError) -> HTTPException:
    """行情缺失是可解释的降级，不是服务器错误；把原因原样交给前端。"""
    return HTTPException(503, {'code': 'strategy_data_unavailable', 'reason': exc.reason,
                               'message': str(exc.detail or exc.reason), 'details': {}})


@router.get('/strategy/config')
def config():
    """14 只指数的规则参数（只读，用于前端展示与核对）。"""
    return {
        'strategy_version': INDEX_STRATEGY_VERSION,
        'display_name': 'V9＋G2',
        'price_bases': BASIS_LABELS,
        'items': [{
            'index_id': cfg.index_id, 'name': cfg.name, 'market': cfg.market,
            'track': cfg.track_label, 'breakout_window': cfg.breakout_window,
            'entry_window': cfg.entry_window,
            'initial_stop_atr': cfg.initial_stop_atr,
            'trail_trigger_atr': cfg.trail_trigger_atr,
            'trail_drawdown_pct': cfg.trail_drawdown_pct,
            'exit_after_bars': cfg.exit_after_bars,
            'e2a_enabled': cfg.e2a_enabled, 'factor_key': cfg.factor_key,
            'etf_code': cfg.etf_code, 'price_unit': cfg.price_unit,
            'counts_against_core_slots': cfg.counts_against_core_slots,
        } for cfg in all_configs()],
        'index_ids': configured_index_ids(),
    }


@router.get('/strategy/regime')
def regime(svc: Service, as_of: str | None = None, allow_network: bool = True):
    snapshot = svc.regime(as_of=svc.current_day(as_of), allow_network=allow_network)
    return {'state': snapshot.state, 'label': snapshot.label, 'er60': snapshot.er60,
            'change_pct': snapshot.change_pct, 'components': snapshot.components,
            'points': snapshot.points, 'as_of': snapshot.as_of,
            'warning': snapshot.warning, 'known': snapshot.known}


@router.get('/strategy/pool/{account_id}')
def pool(account_id: str, svc: Service, as_of: str | None = None,
         allow_network: bool = True):
    return svc.pool(account_id, as_of=as_of, allow_network=allow_network)


@router.get('/strategy/positions/{account_id}')
def positions(account_id: str, svc: Service, as_of: str | None = None,
              allow_network: bool = True):
    return svc.position_rows(account_id, as_of=as_of, allow_network=allow_network)


@router.get('/strategy/signal/{index_id}')
def signal(index_id: str, svc: Service, account_id: str | None = None):
    row = svc.store.latest_signal(index_id, account_id=account_id)
    if row is None:
        raise HTTPException(404, 'no strategy signal recorded')
    return row


@router.post('/strategy/refresh')
def refresh(svc: Service, account_id: str, as_of: str | None = None,
            allow_network: bool = True):
    """重算观察池并记录正式信号证据；不产生任何真实成交。"""
    return svc.pool(account_id, as_of=as_of, allow_network=allow_network)


@router.get('/strategy/series/{index_id}')
def series(index_id: str, svc: Service, as_of: str | None = None,
           basis: str = ETF_ADJUSTED, allow_network: bool = False):
    """显式查看某一价格口径的日线；默认禁止联网，避免读接口触发网络请求。"""
    if basis not in BASIS_LABELS:
        raise HTTPException(422, 'unknown price basis')
    day = svc.current_day(as_of)
    try:
        loaded = (svc.data.index_series(index_id, completed_on=day, allow_network=allow_network)
                  if basis == INDEX_BASIS else
                  svc.data.etf_series(index_id, completed_on=day, basis=basis,
                                      allow_network=allow_network))
    except SeriesError as exc:
        raise _unavailable(exc) from exc
    return {'index_id': loaded.index_id, 'source': loaded.source,
            'price_basis': basis,
            'price_unit': '点' if basis == INDEX_BASIS else '元',
            'source_verified': loaded.source_verified, 'warning': loaded.warning,
            'bars': [{'date': bar.date, 'open': bar.open, 'high': bar.high,
                      'low': bar.low, 'close': bar.close} for bar in loaded.bars[-60:]]}
