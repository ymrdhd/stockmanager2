"""V9/G2 strategy read models and an explicit, idempotent evaluation endpoint.

读写分离（ISS-034）：

- 所有 **GET** 都是只读快照：只计算与展示，不写正式信号、不改计划、不推进持仓状态、
  不启停冷却。读接口即使拿不到行情也只返回降级原因，绝不因为一次浏览就产生正式证据。
- **POST /strategy/refresh** 是唯一的正式写入入口，需要显式账户与自己的请求语义；
  它仍然只记录信号/计划证据，不产生任何真实成交。
- **POST /strategy/sync/{account_id}** 从真实批次重建缺失的策略证据（ISS-032），
  这是可核验的衍生重建，不是新的事实来源。

真实成交始终只能经 ``/api/v1/trades`` 由用户确认。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.application.strategy import SeriesError, StrategyService
from backend.application.strategy.service import StrategyCalendarUnavailable
from backend.application.strategy.series import BASIS_LABELS, ETF_UNADJUSTED, INDEX_BASIS
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
        'v9_price_basis': ETF_UNADJUSTED,
        'g2_price_basis': INDEX_BASIS,
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
            'price_basis': ETF_UNADJUSTED if cfg.uses_etf_underlying else INDEX_BASIS,
            'counts_against_core_slots': cfg.counts_against_core_slots,
        } for cfg in all_configs()],
        'index_ids': configured_index_ids(),
    }


@router.get('/strategy/regime')
def regime(svc: Service, as_of: str | None = None, allow_network: bool = True):
    try:
        day = svc.current_day(as_of)
    except StrategyCalendarUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    snapshot = svc.regime(as_of=day, allow_network=False)
    return {'state': snapshot.state, 'label': snapshot.label, 'er60': snapshot.er60,
            'change_pct': snapshot.change_pct, 'components': snapshot.components,
            'points': snapshot.points, 'as_of': snapshot.as_of,
            'warning': snapshot.warning, 'known': snapshot.known,
            'price_basis': INDEX_BASIS, 'price_unit': '点'}


@router.get('/strategy/pool/{account_id}')
def pool(account_id: str, svc: Service, as_of: str | None = None,
         allow_network: bool = True):
    """只读观察池：只计算与展示，不写正式信号。"""
    return svc.pool(account_id, as_of=as_of, allow_network=False, persist=False)


@router.get('/strategy/positions/{account_id}')
def positions(account_id: str, svc: Service, as_of: str | None = None,
              allow_network: bool = True):
    """只读持仓决策：真实批次驱动，模型退出只作建议。"""
    return svc.position_rows(account_id, as_of=as_of, allow_network=False,
                             persist=False)


@router.get('/strategy/signal/{index_id}')
def signal(index_id: str, svc: Service, account_id: str | None = None,
           signal_date: str | None = None):
    if signal_date:
        found = svc.store.signals_on(signal_date, account_id=account_id)
        for row in found:
            if row['index_id'] == index_id:
                return row
        raise HTTPException(404, 'no strategy signal recorded for that date')
    row = svc.store.latest_signal(index_id, account_id=account_id)
    if row is None:
        raise HTTPException(404, 'no strategy signal recorded')
    return row


@router.get('/strategy/signals/{account_id}')
def signals(account_id: str, svc: Service, signal_date: str | None = None):
    """该账户已记录的信号快照（只读）；不触发任何重算。"""
    if signal_date:
        return {'items': svc.store.signals_on(signal_date, account_id=account_id)}
    with svc.db.read() as conn:
        from backend.application.trading.service import rows
        return {'items': rows(conn,'SELECT * FROM strategy_signals WHERE account_id=? ORDER BY signal_date DESC,created_at DESC LIMIT 500',(account_id,))}


@router.get('/strategy/positions/{account_id}/history')
def history(account_id: str, svc: Service, index_id: str | None = None):
    """历史策略证据（只读）：绝不使用当前持仓冒充历史。"""
    return {'account_id': account_id, 'items': svc.store.positions_for(account_id,
                                                                      index_id=index_id)}


@router.post('/strategy/refresh')
def refresh(svc: Service, account_id: str, as_of: str | None = None,
            allow_network: bool = True):
    """重算观察池并记录正式信号证据；不产生任何真实成交。

    重复调用是幂等的：同一账户、同一等价输入只会留下一条信号与一条计划。
    """
    return svc.pool(account_id, as_of=as_of, allow_network=allow_network, persist=True)


@router.post('/strategy/positions/{account_id}/refresh')
def refresh_positions(account_id: str, svc: Service, as_of: str | None = None,
                      allow_network: bool = True):
    """重算持仓决策并记录观测衍生状态；不产生任何真实成交。"""
    return svc.position_rows(account_id, as_of=as_of, allow_network=allow_network,
                             persist=True)


@router.post('/strategy/sync/{account_id}')
def sync(account_id: str, svc: Service, as_of: str | None = None):
    """从真实批次重建缺失的策略证据；证据不全的进入待人工核对，绝不编造。"""
    try:
        return svc.sync_from_custody(account_id, as_of=as_of)
    except StrategyCalendarUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/strategy/series/{index_id}')
def series(index_id: str, svc: Service, as_of: str | None = None,
           basis: str = ETF_UNADJUSTED, allow_network: bool = False):
    """显式查看某一价格口径的日线；默认未复权，默认禁止联网。"""
    if basis not in BASIS_LABELS:
        raise HTTPException(422, 'unknown price basis')
    try:
        day = svc.current_day(as_of)
    except StrategyCalendarUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        loaded = (svc.data.index_series(index_id, completed_on=day, allow_network=False)
                  if basis == INDEX_BASIS else
                  svc.data.etf_series(index_id, completed_on=day, basis=basis,
                                      allow_network=False))
    except SeriesError as exc:
        raise _unavailable(exc) from exc
    return {'index_id': loaded.index_id, 'source': loaded.source,
            'price_basis': basis,
            'price_unit': '点' if basis == INDEX_BASIS else '元',
            'source_verified': loaded.source_verified, 'warning': loaded.warning,
            'bars': [{'date': bar.date, 'open': bar.open, 'high': bar.high,
                      'low': bar.low, 'close': bar.close} for bar in loaded.bars[-60:]]}
