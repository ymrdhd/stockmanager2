"""策略证据持久化：信号、计划、持仓推进状态、冷却、NDX 辅助仓。

原则（ISS-033 之后）：

- **市场信号可共享，执行计划必须归属**。``strategy_signals`` 允许 ``account_id`` 为空
  （表示无账户归属的市场级证据），但 ``strategy_plans`` 是执行计划，必须有明确账户，
  否则账户 A 的计划会让账户 B 误判为 PLAN_PENDING。
- 幂等键里带上账户维度：``_signal_id`` / ``_plan_id`` 的摘要输入包含 ``account_id``，
  同一账户、同一等价信号重复刷新仍是同一条记录（不产生第二份证据），
  不同账户各自独立。
- 持仓推进状态是 ``PositionState`` 的完整快照（``state_json``），列字段只用于查询；
  它**只是可重建的衍生证据**，真实持仓始终以 ``trade_executions`` / ``trade_lots`` /
  ``lot_allocations`` 为准。
- 这里**不会**写 ``trade_executions`` / ``trade_lots`` / ``cash_movements``。
"""
from __future__ import annotations

from datetime import UTC, datetime
from contextlib import nullcontext
from hashlib import sha256
import json
from typing import Optional

from backend.application.trading.service import dump, one, rows, utc
from backend.infrastructure.database import Database
from backend.domain.strategy.exit import PositionState

#: 无账户归属的市场级证据在幂等摘要里的占位符（不是账户 ID，不入外键）。
MARKET_SCOPE = '-'


def _scope(account_id: Optional[str]) -> str:
    return account_id or MARKET_SCOPE


def _signal_id(account_id: Optional[str], index_id: str, signal_date: str,
               input_version: str, version: str) -> str:
    raw = f'{_scope(account_id)}|{index_id}|{signal_date}|{input_version}|{version}'
    return 'sig:' + sha256(raw.encode()).hexdigest()[:32]


def _plan_id(account_id: Optional[str], index_id: str, signal_date: str, version: str) -> str:
    raw = f'{_scope(account_id)}|{index_id}|{signal_date}|{version}'
    return 'plan:' + sha256(raw.encode()).hexdigest()[:32]


class CapacityRefused(RuntimeError):
    """A plan could not acquire capacity in the serialized ledger transaction."""


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
                    source_verified: bool = True, connection=None) -> dict:
        key = _signal_id(account_id, index_id, signal_date, input_version, strategy_version)
        now = utc(self.clock())
        with (self.db.transaction() if connection is None else nullcontext(connection)) as conn:
            conn.execute(
                '''INSERT INTO strategy_signals
                   (signal_id,account_id,index_id,instrument_code,strategy_version,config_version,
                    signal_date,input_version,price_basis,price_unit,status,reason_codes,detail,
                    breakout_window,signal_close,threshold_high,atr14,buy_max,
                    initial_stop_reference,trail_trigger_reference,factor_status,factor_detail,
                    combo_status,combo_detail,regime_state,regime_er60,data_source,is_proxy,
                    source_verified,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(signal_id) DO NOTHING''',
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
                sql += ' AND account_id=?'
                args += (account_id,)
            else:
                sql += ' AND account_id IS NULL'
            sql += ' ORDER BY signal_date DESC, updated_at DESC LIMIT 1'
            found = rows(conn, sql, args)
            return found[0] if found else None

    def signals_on(self, signal_date: str, *, account_id: Optional[str] = None) -> list[dict]:
        with self.db.read() as conn:
            sql = 'SELECT * FROM strategy_signals WHERE signal_date=?'
            args: tuple = (signal_date,)
            if account_id is not None:
                sql += ' AND account_id=?'
                args += (account_id,)
            else:
                sql += ' AND account_id IS NULL'
            return rows(conn, sql + ' ORDER BY index_id', args)

    def signal_by_id(self, signal_id: str) -> Optional[dict]:
        with self.db.read() as conn:
            found = rows(conn, 'SELECT * FROM strategy_signals WHERE signal_id=?', (signal_id,))
            return found[0] if found else None

    # -- 计划 ------------------------------------------------------------
    def save_plan(self, *, account_id: str, index_id: str, signal_date: str,
                  strategy_version: str, status: str, signal_id: Optional[str] = None,
                  signal_close: Optional[float] = None, threshold_high: Optional[float] = None,
                  atr14: Optional[float] = None, buy_max: Optional[float] = None,
                  initial_stop_reference: Optional[float] = None,
                  trail_trigger_reference: Optional[float] = None,
                  detail: str = '', capacity_check=None, connection=None) -> dict:
        if not account_id:
            raise ValueError('执行计划必须具有明确账户归属')
        key = _plan_id(account_id, index_id, signal_date, strategy_version)
        now = utc(self.clock())
        with (self.db.transaction() if connection is None else nullcontext(connection)) as conn:
            if capacity_check is not None:
                capacity_check(conn)
            conn.execute(
                '''INSERT INTO strategy_plans
                   (plan_id,account_id,index_id,strategy_version,signal_id,signal_date,status,
                    signal_close,threshold_high,atr14,buy_max,initial_stop_reference,
                    trail_trigger_reference,detail,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status,
                    signal_id=COALESCE(excluded.signal_id,strategy_plans.signal_id),
                    detail=excluded.detail, buy_max=excluded.buy_max,
                    atr14=excluded.atr14, signal_close=excluded.signal_close,
                    threshold_high=excluded.threshold_high,
                    initial_stop_reference=excluded.initial_stop_reference,
                    trail_trigger_reference=excluded.trail_trigger_reference,
                    updated_at=excluded.updated_at''',
                (key, account_id, index_id, strategy_version, signal_id, signal_date, status,
                 signal_close, threshold_high, atr14, buy_max, initial_stop_reference,
                 trail_trigger_reference, detail, now, now))
            return one(conn, 'SELECT * FROM strategy_plans WHERE plan_id=?', (key,))

    def active_plan(self, account_id: str, index_id: str, *,
                    statuses: tuple[str, ...] = ('SIGNAL', 'AWAITING_CONFIRMATION')) -> Optional[dict]:
        """只查**指定账户**的活跃计划；账户之间绝不互相影响。"""
        marks = ','.join('?' for _ in statuses)
        with self.db.read() as conn:
            found = rows(conn,
                         f'SELECT * FROM strategy_plans WHERE account_id=? AND index_id=?'
                         f' AND status IN ({marks}) ORDER BY signal_date DESC LIMIT 1',
                         (account_id, index_id, *statuses))
            return found[0] if found else None

    def clear_plan(self, account_id: str, index_id: str, *, reason: str) -> None:
        """只取消**指定账户**的计划。"""
        now = utc(self.clock())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE strategy_plans SET status='CANCELLED', detail=?, updated_at=?"
                " WHERE account_id=? AND index_id=? AND status IN ('SIGNAL','AWAITING_CONFIRMATION')",
                (reason, now, account_id, index_id))

    def plans_for(self, account_id: str) -> list[dict]:
        with self.db.read() as conn:
            return rows(conn, 'SELECT * FROM strategy_plans WHERE account_id=?'
                              ' AND status IN (\'SIGNAL\',\'AWAITING_CONFIRMATION\')'
                              ' ORDER BY signal_date DESC, index_id', (account_id,))

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
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (position_id, account_id, state.index_id, state.strategy_version,
                 state.signal_date, *values[:-1], now, now))

    def load_position(self, account_id: str, index_id: str) -> Optional[PositionState]:
        with self.db.read() as conn:
            found = rows(conn,
                         'SELECT state_json FROM strategy_positions WHERE account_id=? AND index_id=?'
                         ' ORDER BY signal_date DESC LIMIT 1', (account_id, index_id))
        if not found:
            return None
        return PositionState.from_payload(json.loads(found[0]['state_json']))

    def load_position_on(self, account_id: str, index_id: str,
                         signal_date: str) -> Optional[PositionState]:
        """按 (账户, 指数, 信号日) 精确取某一份证据，供历史查询/重建使用。"""
        with self.db.read() as conn:
            found = rows(conn,
                         'SELECT state_json FROM strategy_positions WHERE account_id=? AND index_id=?'
                         ' AND signal_date=?', (account_id, index_id, signal_date))
        if not found:
            return None
        return PositionState.from_payload(json.loads(found[0]['state_json']))

    def open_positions(self, account_id: str) -> list[dict]:
        with self.db.read() as conn:
            return rows(conn, 'SELECT * FROM strategy_positions WHERE account_id=?'
                              ' AND exit_filled=0 AND entry_executed=1 ORDER BY index_id',
                        (account_id,))

    def positions_for(self, account_id: str, *, index_id: Optional[str] = None) -> list[dict]:
        """该账户的全部策略证据行（含已平仓），供真实持仓对账与历史查询。"""
        sql = 'SELECT * FROM strategy_positions WHERE account_id=?'
        args: tuple = (account_id,)
        if index_id:
            sql += ' AND index_id=?'
            args += (index_id,)
        with self.db.read() as conn:
            return rows(conn, sql + ' ORDER BY signal_date DESC', args)

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


__all__ = ['MARKET_SCOPE', 'StrategyStore']
