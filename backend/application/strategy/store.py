"""策略证据持久化：信号、计划、持仓推进状态、冷却、NDX 辅助仓。

原则：
- 同一 ``(index_id, signal_date, input_version, strategy_version)`` 只允许一条信号，
  重复刷新不会产生新的等价正式信号（``INSERT ... ON CONFLICT DO UPDATE`` 只更新状态）；
- 持仓推进状态是 ``PositionState`` 的完整快照（``state_json``），列字段只用于查询；
- 冷却与 NDX 辅助仓按账户 + 指数唯一；
- 这里**不会**写 ``trade_executions`` / ``trade_lots`` / ``cash_movements``。
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Optional

from backend.application.trading.service import dump, one, rows, utc
from backend.infrastructure.database import Database
from backend.domain.strategy.exit import PositionState


def _signal_id(index_id: str, signal_date: str, input_version: str, version: str) -> str:
    return 'sig:' + sha256(f'{index_id}|{signal_date}|{input_version}|{version}'.encode()).hexdigest()[:32]


def _plan_id(index_id: str, signal_date: str, version: str) -> str:
    return 'plan:' + sha256(f'{index_id}|{signal_date}|{version}'.encode()).hexdigest()[:32]


class StrategyStore:
    def __init__(self, db: Database,
                 clock=lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.clock = clock

    # -- 信号 ------------------------------------------------------------
    def save_signal(self, *, index_id: str, signal_date: str, input_version: str,
                    strategy_version: str, config_version: str, status: str,
                    account_id: Optional[str] = None, instrument_code: Optional[str] = None,
                    price_basis: str = 'index_level', price_unit: str = '点',
                    reason_codes: Optional[list[str]] = None, detail: str = '',
                    breakout_window: Optional[int] = None, signal_close: Optional[float] = None,
                    threshold_high: Optional[float] = None, atr14: Optional[float] = None,
                    buy_max: Optional[float] = None,
                    initial_stop_reference: Optional[float] = None,
                    trail_trigger_reference: Optional[float] = None,
                    factor_status: Optional[str] = None, factor_detail: str = '',
                    combo_status: Optional[str] = None, combo_detail: str = '',
                    regime_state: Optional[str] = None, regime_er60: Optional[float] = None,
                    data_source: str = '', is_proxy: bool = False,
                    source_verified: bool = True) -> dict:
        key = _signal_id(index_id, signal_date, input_version, strategy_version)
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                '''INSERT INTO strategy_signals
                   (signal_id,account_id,index_id,instrument_code,strategy_version,config_version,
                    signal_date,input_version,price_basis,price_unit,status,reason_codes,detail,
                    breakout_window,signal_close,threshold_high,atr14,buy_max,
                    initial_stop_reference,trail_trigger_reference,factor_status,factor_detail,
                    combo_status,combo_detail,regime_state,regime_er60,data_source,is_proxy,
                    source_verified,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                    status=excluded.status, reason_codes=excluded.reason_codes,
                    detail=excluded.detail, factor_status=excluded.factor_status,
                    factor_detail=excluded.factor_detail, combo_status=excluded.combo_status,
                    combo_detail=excluded.combo_detail, regime_state=excluded.regime_state,
                    regime_er60=excluded.regime_er60, updated_at=excluded.updated_at''',
                (key, account_id, index_id, instrument_code, strategy_version, config_version,
                 signal_date, input_version, price_basis, price_unit, status,
                 dump(reason_codes or []), detail, breakout_window, signal_close, threshold_high,
                 atr14, buy_max, initial_stop_reference, trail_trigger_reference, factor_status,
                 factor_detail, combo_status, combo_detail, regime_state, regime_er60,
                 data_source, int(is_proxy), int(source_verified), now, now))
            return one(conn, 'SELECT * FROM strategy_signals WHERE signal_id=?', (key,))

    def latest_signal(self, index_id: str, *, account_id: Optional[str] = None) -> Optional[dict]:
        with self.db.read() as conn:
            sql = 'SELECT * FROM strategy_signals WHERE index_id=?'
            args: tuple = (index_id,)
            if account_id is not None:
                sql += ' AND (account_id IS NULL OR account_id=?)'
                args += (account_id,)
            sql += ' ORDER BY signal_date DESC, updated_at DESC LIMIT 1'
            found = rows(conn, sql, args)
            return found[0] if found else None

    def signals_on(self, signal_date: str, *, account_id: Optional[str] = None) -> list[dict]:
        with self.db.read() as conn:
            sql = 'SELECT * FROM strategy_signals WHERE signal_date=?'
            args: tuple = (signal_date,)
            if account_id is not None:
                sql += ' AND (account_id IS NULL OR account_id=?)'
                args += (account_id,)
            return rows(conn, sql + ' ORDER BY index_id', args)

    # -- 计划 ------------------------------------------------------------
    def save_plan(self, *, index_id: str, signal_date: str, strategy_version: str,
                  status: str, signal_id: Optional[str] = None,
                  account_id: Optional[str] = None, signal_close: Optional[float] = None,
                  threshold_high: Optional[float] = None, atr14: Optional[float] = None,
                  buy_max: Optional[float] = None,
                  initial_stop_reference: Optional[float] = None,
                  trail_trigger_reference: Optional[float] = None,
                  detail: str = '') -> dict:
        key = _plan_id(index_id, signal_date, strategy_version)
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                '''INSERT INTO strategy_plans
                   (plan_id,account_id,index_id,strategy_version,signal_id,signal_date,status,
                    signal_close,threshold_high,atr14,buy_max,initial_stop_reference,
                    trail_trigger_reference,detail,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status,
                    detail=excluded.detail, buy_max=excluded.buy_max,
                    atr14=excluded.atr14, updated_at=excluded.updated_at''',
                (key, account_id, index_id, strategy_version, signal_id, signal_date, status,
                 signal_close, threshold_high, atr14, buy_max, initial_stop_reference,
                 trail_trigger_reference, detail, now, now))
            return one(conn, 'SELECT * FROM strategy_plans WHERE plan_id=?', (key,))

    def active_plan(self, index_id: str, *, statuses: tuple[str, ...] = ('SIGNAL', 'AWAITING_CONFIRMATION')) -> Optional[dict]:
        marks = ','.join('?' for _ in statuses)
        with self.db.read() as conn:
            found = rows(conn,
                         f'SELECT * FROM strategy_plans WHERE index_id=? AND status IN ({marks})'
                         ' ORDER BY signal_date DESC LIMIT 1', (index_id, *statuses))
            return found[0] if found else None

    def clear_plan(self, index_id: str, *, reason: str) -> None:
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE strategy_plans SET status='CANCELLED', detail=?, updated_at=?"
                " WHERE index_id=? AND status IN ('SIGNAL','AWAITING_CONFIRMATION')",
                (reason, now, index_id))

    # -- 持仓推进状态 ----------------------------------------------------
    def save_position(self, account_id: str, state: PositionState) -> None:
        payload = state.to_payload()
        now = utc(self.clock())
        with self.db.transaction() as conn:
            existing = rows(conn,
                            'SELECT position_id FROM strategy_positions WHERE account_id=? AND index_id=?'
                            ' AND signal_date=?', (account_id, state.index_id, state.signal_date))
            values = (state.entry_date, state.entry_price, state.signal_atr, state.initial_stop,
                      state.stop_price, state.trail_trigger, state.stage, state.highest_price,
                      state.current_atr, state.holding_days, state.exit_after_bars,
                      state.renewals, state.e2a_extended_days, state.below_ma20_streak,
                      int(state.extended), int(state.add_executed), state.add_filled_date,
                      int(state.exit_filled), state.closed_date, state.last_trade_date,
                      state.last_exit_date, state.last_exit_action, state.last_exit_reason,
                      state.pending_stop, state.pending_stop_effective_date, state.pending_action,
                      state.pending_action_date, state.main_lot_id,
                      int(state.shares) if state.shares else None, int(state.entry_executed),
                      state.execution_source, dump(payload), now)
            if existing:
                conn.execute(
                    '''UPDATE strategy_positions SET entry_date=?,entry_price=?,signal_atr=?,
                       initial_stop=?,stop_price=?,trail_trigger=?,stage=?,highest_price=?,
                       current_atr=?,holding_days=?,exit_after_bars=?,renewals=?,
                       e2a_extended_days=?,below_ma20_streak=?,extended=?,add_executed=?,
                       add_filled_date=?,exit_filled=?,closed_date=?,last_trade_date=?,
                       last_exit_date=?,last_exit_action=?,last_exit_reason=?,pending_stop=?,
                       pending_stop_effective_date=?,pending_action=?,pending_action_date=?,
                       main_lot_id=?,shares=?,entry_executed=?,execution_source=?,state_json=?,
                       updated_at=? WHERE position_id=?''',
                    (*values, existing[0]['position_id']))
                return
            position_id = 'pos:' + sha256(
                f'{account_id}|{state.index_id}|{state.signal_date}'.encode()).hexdigest()[:32]
            conn.execute(
                '''INSERT INTO strategy_positions
                   (position_id,account_id,index_id,strategy_version,signal_date,entry_date,
                    entry_price,signal_atr,initial_stop,stop_price,trail_trigger,stage,
                    highest_price,current_atr,holding_days,exit_after_bars,renewals,
                    e2a_extended_days,below_ma20_streak,extended,add_executed,add_filled_date,
                    exit_filled,closed_date,last_trade_date,last_exit_date,last_exit_action,
                    last_exit_reason,pending_stop,pending_stop_effective_date,pending_action,
                    pending_action_date,main_lot_id,shares,entry_executed,execution_source,
                    state_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (position_id, account_id, state.index_id, state.strategy_version,
                 state.signal_date, *values, now))

    def load_position(self, account_id: str, index_id: str) -> Optional[PositionState]:
        with self.db.read() as conn:
            found = rows(conn,
                         'SELECT state_json FROM strategy_positions WHERE account_id=? AND index_id=?'
                         ' ORDER BY signal_date DESC LIMIT 1', (account_id, index_id))
        if not found:
            return None
        return PositionState.from_payload(json.loads(found[0]['state_json']))

    def open_positions(self, account_id: str) -> list[dict]:
        with self.db.read() as conn:
            return rows(conn, 'SELECT * FROM strategy_positions WHERE account_id=?'
                              ' AND exit_filled=0 AND entry_executed=1 ORDER BY index_id',
                        (account_id,))

    # -- 冷却 ------------------------------------------------------------
    def save_cooldown(self, account_id: str, index_id: str, *, exit_date: str,
                      exit_action: str, exit_reason: str, required_days: int,
                      cooldown_until: Optional[str], regime: str = '', extended: bool = False) -> None:
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                '''INSERT INTO strategy_cooldowns
                   (account_id,index_id,exit_date,exit_action,exit_reason,required_days,
                    cooldown_until,regime,extended,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id,index_id) DO UPDATE SET exit_date=excluded.exit_date,
                    exit_action=excluded.exit_action, exit_reason=excluded.exit_reason,
                    required_days=excluded.required_days, cooldown_until=excluded.cooldown_until,
                    regime=excluded.regime, extended=excluded.extended,
                    updated_at=excluded.updated_at''',
                (account_id, index_id, exit_date, exit_action, exit_reason, int(required_days),
                 cooldown_until, regime, int(extended), now))

    def load_cooldown(self, account_id: str, index_id: str) -> Optional[dict]:
        with self.db.read() as conn:
            found = rows(conn,
                         'SELECT * FROM strategy_cooldowns WHERE account_id=? AND index_id=?',
                         (account_id, index_id))
            return found[0] if found else None

    # -- NDX 辅助仓 ------------------------------------------------------
    def save_ndx_aux(self, account_id: str, *, ratio: float,
                     last_add_date: Optional[str]) -> None:
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                '''INSERT INTO strategy_ndx_aux (account_id,ratio,last_add_date,updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET ratio=excluded.ratio,
                   last_add_date=excluded.last_add_date, updated_at=excluded.updated_at''',
                (account_id, float(ratio), last_add_date, now))

    def load_ndx_aux(self, account_id: str) -> dict:
        with self.db.read() as conn:
            found = rows(conn, 'SELECT * FROM strategy_ndx_aux WHERE account_id=?', (account_id,))
        return found[0] if found else {'ratio': 0.0, 'last_add_date': None}


__all__ = ['StrategyStore']
