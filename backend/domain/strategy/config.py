"""指数独立交易策略的冻结配置（v5 起 14 只）。

参数来源：用户 2026-10-30 提供的 10 指数研究候选配置表（文字 + 截图）。
该表是本次唯一有效的策略参数来源，禁止回退到 MTQ / TQ / 旧买点分级参数。

重要口径说明（不得在界面上写成收益承诺）：
- 这些是**历史研究候选配置**，不是未来收益保证，也不是经过实盘验证的最优参数。
- ATR 统一为 14 个交易日的真实波幅均值（与 ``subroutine.indicators.compute_atr14``
  同一口径，即 14 个 TR 的简单均值，不是 Wilder 平滑），避免同一项目里出现两套 ATR。
- 额外入场因子的精确计算定义（年化方式、波动率单位、分母范围、缺失处理、趋势效率公式）
  不在本仓库内，见 ``FACTOR_SPECS`` 的 ``verified`` 标记；未核实因子不得触发正式买入提醒。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

#: 策略版本号。通知去重、持仓状态都以它为准；调整任何参数必须同时升版本。
#:
#: v9.2（G2）：2026-11-13 在 V9 之上接入 G2 两条市场状态规则，**不改动 V9 任何
#: 买卖/止损参数**：
#: 1) 14 指数等权基准 ER60：<0.25 → RANGE 震荡市；≥0.25 且过去 60 日上涨 → UP，
#:    下跌 → DOWN（只用已完成行情，见 ``regime.py``）。
#: 2) G2-1 震荡市止损后延长冷却：RANGE 且上次因普通止损/跳空止损退出 → 至少 10 个
#:    有效交易日才允许重新买入；非 RANGE 仍为原 3 个完整交易日；正常到期退出不延长。
#: 3) G2-2 震荡市禁止盈利加仓：RANGE 下即使满足 S1b+K6 也不允许加仓，且**不算**
#:    已成功加仓（``add_executed`` 不变）；UP/DOWN 仍按原 K6 判定。
#: 4) 首次突破买入不受 G2 限制（RANGE 也允许正常首次突破买入与卖出）。
#:
#: v9.1：2026-11-13 双轨化——**NDX 与 A 股 ETF 分离**。
#: 1) NDX 标为 ``TRACK_US``：首次突破用自身 N=18（不再乘 0.75，A 股轨才是 T3），
#:    满 20 个持仓交易日收盘无条件清仓（不做原始N续期、不套 E2a）。
#: 2) NDX 仓位走 T5 辅助仓（首次 15% → 目标 21.5% → 上限 25%），
#:    不占 A 股 8 个核心槽位，也不参与 62,500 元的 S1b+K6 盈利加仓。
#: 3) 策略池新增「轨道信号」列：只有「已持仓且当日未触发退出」显示绿色，其余红色。
#:    价格坐标仍是 159941 的场内成交价（可成交口径），独立的是参数、到期与仓位规则。
#:
#: v9：2026-11-13 把原 v7「到期日有条件延长」整体替换为 **V9**：
#: ``动态1/8核心仓的K6规则 + 最多8个核心仓位``。相对 v7 的实质改动：
#: 1) T3 首次突破窗口 = ``max(1, floor(0.75 × 原始N))``（软件参数表里的 N 视为
#:    原始 N，已是寻优后的静态值）；**续期仍用原始 N**，不得用 0.75N。
#: 2) E2a 到期管理：基础第 21 个持仓交易日先按原始 N 判断续期；不续期但
#:    「盈利 + 强趋势（收盘>SMA60、SMA20>SMA60、SMA60 上行）」时最多延持
#:    10 个该指数交易日；延持期连续两次收盘低于 SMA20 → 下一合法时点退出。
#: 3) S1b 盈利加仓 + K6 门禁：固定 62,500 元、每个主仓最多成功一次，
#:    要求 ``收盘>SMA20 且 SMA20/SMA60 >= 1.02``。
#: 4) 基础新开仓目标金额 = 该时点策略总权益 ÷ 8（不是固定 12.5 万、也不 ÷10）；
#:    始终最多 8 个不同核心指数，第 9 个一律拒绝（子仓不占槽位）。
#: 5) T5 NDX 独立辅助仓：15% → 21.5%、上限 25%、超限减到 24.5%、
#:    最短 10 个美股交易日补仓、25 万现金缓冲；只做信号/计划展示。
#: **未回测选定的假突破过滤、暂停加仓、延长冷却等研究项一律不纳入 V9。**
#:
#: v7：2026-11-04 到期日有条件延长（收盘>MA20 且距峰值回撤<5% 可延到 40 根）。
#: v9 起该机制被 E2a 取代，``extend_*`` 字段与状态机整体删除，不做兼容保留——
#: 留着会让人以为 V9 还有一层"回撤<5% 自动延长"的规则。
#:
#: v6：2026-11-02 标的由「指数点位」整体切成「该指数对应的 ETF 成交价（元）」
#: （用户决定：「把策略池里的指数，改成指数对应内置的 ETF，其他参数不变」）。
#:
#: 为什么切：指数不能下单，真正成交的是 ETF；此前表格同时给点位与括号里的 ETF 价，
#: 两套坐标系并列容易看错（详见 ``docs/INDEX_STRATEGY.md`` §18）。切换后
#: N / C / A / B / 时间退出 / 仓位规则 / 因子组合**全部不动**，只是把计算价格
#: 从指数点位换成同一只 ETF 的前复权日线，单位由「点」改成「元」、小数位 2→3。
#:
#: 影响面（必须知道）：
#: 1) 四个海外/跨市场标的（纳指 159941、恒生科技 513180、创新药 159992 等）本来就是
#:    QDII/LOF，成交在场内 A 股，故 ``clock_market`` 一律改为 ``MARKET_CN``——
#:    否则会用美股交易日去对 A 股 K 线。
#: 2) ETF 有溢价/折价、申赎、分红与跟踪误差，突破点在 ETF 上会比指数上略早或略晚；
#:    这是从"研究口径"走向"可成交口径"的必然代价，不是参数退化。
#: 3) 存量按点位记录的买入计划已作废（点位与元不能混算），切版时逐条清空。
#: 4) ``ETF_INDEX_CODE_MAP`` 的 7 只是原有可核实代码；另外 7 只（沪深300/中证500/
#:    中证1000/深证成指/创业板指/科创50/中证2000）由用户 2026-11-02 直接指定，
#:    并已用东财行情接口核对名称与代码一一对应（见各配置行注释）。
#:
#: v5：2026-11-02 标的池由 10 只扩到 14 只（新增创新药 / 红利低波 / 芯片 / 白酒），
#: 组合仓位规则由「20% × 最多 5 仓」改为「12.5% × 最多 8 仓」。新增 4 只的参数来自
#: 同日做的单标的网格寻优（N/C/A/B 各 1200 组，2015-01~2026-10，目标 CAGR）：
#:
#:   931152.CSI 创新药   N=8  C=2.00 A=3.0 B=3%   单标的 CAGR 9.00%  MDD -26.3%
#:   H30269.CSI 红利低波 N=5  C=2.00 A=0.5 B=5%   单标的 CAGR 7.23%  MDD -19.4%
#:   H30007.CSI 芯片产业 N=5  C=0.75 A=5.0 B=8%   单标的 CAGR 24.66% MDD -23.0%
#:   399997.SZ  中证白酒 N=10 C=1.00 A=5.0 B=3%   单标的 CAGR 17.91% MDD -17.9%
#:
#: **这 4 组是样本内最优，不是走向前验证过的参数。** 尤其芯片 24.66% 主要来自
#: 2019-2021 与 2024-2025 两段单边行情；且 159992/563020/159599 三只 ETF 分别成立于
#: 2020-03 / 2023-12 / 2024-04，其指数在成立前的部分是中证按现行成份**回溯编制**的，
#: 存在结构性前视。详见 ``docs/INDEX_STRATEGY.md`` §17。
#:
#: 仓位规则改动依据（14 只、2015-2026 组合回测，指数层面）：
#: 5 仓×20% CAGR 11.28% / MDD -10.08% / Calmar 1.119；
#: 8 仓×12.5% CAGR 11.15% / MDD -8.19% / Calmar 1.362。
#: 少赚 0.13pp 换来回撤少 1.9pp，且 5 仓在 14 只下会挡掉 1.86 倍于在册交易质量的信号。
#: 注意该回测引擎未复现原研究的绝对收益水平（8.86% vs 16.60%），只用于相对比较。
#:
#: v4：2026-11-01 把交易模型升级到冻结版 v1.0，与 ``breakout_atr_optimization``
#: 的回测语义对齐。四处改动（缺一不可，否则与回测不等价）：
#: 1) 跟踪止盈由「峰值 − k×ATR」改为「峰值 × (1 − B%)」——回测用的是比例回撤，
#:    本仓库 v3 用的 ATR 距离是另一套规则，必须换成 ``trail_drawdown_pct``；
#: 2) 逐指数写入 A（跟踪启动倍数）与 B（峰值回撤 %），来自 ``best_params_v2.json``
#:    的**原始寻优参数**（用户 2026-11-01 明确要求不采用二次寻优结果）；
#: 3) 时间退出由「第5日减半 + 第8日有条件清空、次日开盘执行」改为
#:    「建仓后满 20 个交易日、当日收盘无条件清仓」；
#: 4) 取消保本档位（2026-11-01 已把 ``breakeven_trigger_atr`` 字段与状态机整体删除）——
#:    回测模型里没有这一层，保留会让实盘止损高于回测，系统性改变退出分布。
#: 5) 取消附加因子过滤：模型 v1.0 是纯 N 日突破，回测没有宽度/量能/均线等任何附加条件
#:    （这些过滤器在研究里全部被证伪）。原先中证500/中证1000/深证成指/中证2000/恒生科技
#:    挂着「未核实」因子，在 ``STRICT_FACTOR_VERIFICATION=True`` 下会让这 5 只**永远不出
#:    正式买入提醒**，等于用未验证条件覆盖已验证规则；故 2026-11-01 按用户决定清空
#:    十指数的 ``factor_key``。``FACTOR_SPECS`` 与因子计算代码保留，需要时可再挂回去。
#: 6) A 股大盘腿由上证综指 000001.SH 换成上证50 000016.SH（模型 v1.0 的标的就是 510050）：
#:    N=40 / A=0.5 / B=8% / C=0.5，与 000001.SH 的原参数完全不同，不是同一只换名字。
#:    000001.SH 已从名单移除，``symbols._REJECTED`` 会给出显式提示；
#:    项目 ``subroutine.config`` 的主裁决指数表**未改**（改它会把市场广度从 8 只变 9 只），
#:    上证50 的行情定义只在 ``symbols._EXTRA_CN_DEFINITIONS`` 里补了一条。
#: 仓位规则见 ``PORTFOLIO_POSITION_CAP`` / ``PORTFOLIO_MAX_POSITIONS``（仅展示口径）。
#:
#: v3：2026-11-01 只把突破窗口 N 与初始止损 C 逐指数写入，A/B 与时间退出仍是旧口径，
#: 因此 v3 与回测不等价，不得把寻优 CAGR 当作该版本的预期收益。（已被 v4 取代。）
#:
#: v2：2026-10-31 按用户新参数表重设十指数默认参数（风险预算 / 突破 N / 止损 k /
#: 保本 BE / 跟踪启动与距离），其中中证500、中证1000 的跟踪止损改为「关闭」。
#: 注意：``STATE_KEY_PREFIX`` 仍保留 ``v1``，避免已有持仓 / 冷却 / 买入计划状态失联
#: （``PositionState.from_payload`` 不做版本校验，前缀一变就等于清空持仓跟踪）。
#: 版本号只影响事件去重键与展示，不迁移 kv 前缀。
#: v7：2026-11-04 加入「到期日有条件延长」——满 20 个交易日后，若
#: ``收盘 > MA20`` 且 ``距峰值收盘回撤 < 5%``，最多延长到 40 个交易日；
#: 原移动止损继续有效、只升不降；条件失效当日收盘清仓。
INDEX_STRATEGY_VERSION = "index-strategy-atr-v9.2"

#: 用户可见的策略名。界面、通知标题一律用它，不要写 K6 / V8。
STRATEGY_DISPLAY_NAME = "V9＋G2"

#: 双轨（2026-11-13）：NDX 不再套 A 股的 N/A/B/C 变换，单独一条轨。
#:
#: ``TRACK_CN``  A 股 ETF 轨：T3 首次窗口 0.75N、到期原始N续期 / E2a 延持、
#:              S1b+K6 盈利加仓 62,500、占 8 个核心槽位之一。
#: ``TRACK_US``  美股独立轨（仅 NDX）：直接用自身 N（不乘 0.75）、
#:              满 20 个持仓交易日收盘无条件清仓（不续期、不延持）、
#:              仓位走 T5 辅助仓 15%→21.5%→25%、不占 A 股 8 个槽位、
#:              不参与 62,500 加仓。
#: 两条轨**共用**同一套 ATR14 / 初始止损 C / 跟踪 A×ATR 与 B% 峰回撤口径；
#: 差别只在「首次窗口 / 到期管理 / 仓位与容量」这三块。
TRACK_CN = "CN"
TRACK_US = "US"
TRACK_LABELS = {TRACK_CN: "A股ETF轨", TRACK_US: "美股独立轨"}

#: 标的口径开关：True = 用「该指数对应的 ETF」自身的成交价（元）跑策略；
#: False = 仍用指数点位（点）。默认 True（v6 起）。
#:
#: 关闭时行为与 v5 完全一致（含「次日限价上限」列后面的括号 ETF 价），
#: 供对账/研究时临时切回；环境变量 ``INDEX_STRATEGY_UNDERLYING_ETF=0`` 亦可。
ETF_UNDERLYING = os.environ.get("INDEX_STRATEGY_UNDERLYING_ETF", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
    "n",
    "f",
}

#: 市场标识。
MARKET_CN = "CN"
MARKET_HK = "HK"
MARKET_US = "US"

MARKET_LABELS = {
    MARKET_CN: "A股",
    MARKET_HK: "港股",
    MARKET_US: "美股",
}

#: ATR 窗口（交易日）。
ATR_WINDOW = 14

#: 次日最高允许买入价的上浮倍数：BuyMax = SignalClose + 0.5 x SignalATR14。
BUY_MAX_ATR_MULTIPLIER = 0.5

#: 初始止损触发后必须冷却的完整交易日数。
COOLDOWN_TRADING_DAYS = 3

#: 组合层仓位规则（V9）：基础仓 = 当前策略总权益 ÷ 8，最多同时持有 8 个不同核心指数，
#: 建仓后不因权益变化自动重平衡；已有指数上的盈利加仓属于子仓，不占新槽位。
#:
#: 2026-11-13（V9）明确：不是固定 125,000 元，也不是 ÷10。
#: 旧口径 12.5% × 8 仓与「权益÷8」在数值上相等，但语义不同——前者是固定百分比，
#: 后者是**下单决策时点的权益快照**；本仓库不发单，故只作为展示与拒绝原因的统一口径。
BASE_POSITION_DIVISOR = 8
PORTFOLIO_POSITION_CAP = 1.0 / BASE_POSITION_DIVISOR
PORTFOLIO_MAX_POSITIONS = 8

#: T3：首次突破窗口系数。``Nentry = max(1, floor(0.75 × N_original))``。
T3_WINDOW_FACTOR = 0.75

#: S1b 盈利加仓：目标金额固定 62,500 元、每个主仓最多成功一次。
PYRAMID_TARGET_AMOUNT = 62500.0
#: 加仓前至少持有 4 个持仓交易日（含建仓日）= 3 个完整指数交易日。
PYRAMID_MIN_HOLDING_DAYS = 4
#: 浮盈门槛：至少 1×入场时冻结的 ATR14。
PYRAMID_ATR_MULTIPLE = 1.0
#: ``Close_t > max(Close_{t-10..t-1})`` 的窗口。
PYRAMID_LOOKBACK_DAYS = 10

#: K6 门禁：``Close>SMA20`` 且 ``SMA20/SMA60 >= 1.02``。
K6_GATE_MIN_MA_RATIO = 1.02

#: 强趋势判定：``Close>SMA60、SMA20>SMA60、SMA60(t)>SMA60(t-10)``。
STRONG_TREND_MA_WINDOW = 20
STRONG_TREND_SLOW_MA_WINDOW = 60
STRONG_TREND_SLOPE_LAG = 10

#: 时间退出（V9）：建仓后满 20 个交易日（含建仓日计为第 21 个交易日）为**基础到期日**。
#: 注意回测口径 ``u - t >= 20`` 里建仓日不计入，因此含建仓日应为第 21 个交易日；
#: 本仓库 ``holding_days`` 把建仓日算作第 1 日，故比较阈值用
#: ``TIME_EXIT_BARS_AFTER_ENTRY + 1``。
TIME_EXIT_BARS_AFTER_ENTRY = 20

#: E2a 到期延持：基础到期日不满足续期、但盈利且强趋势时，最多再持有 10 个该指数交易日。
E2A_ENABLED = os.environ.get(
    "INDEX_E2A_ENABLED", os.environ.get("INDEX_EXTEND_ENABLED", "1")
).strip().lower() not in {"0", "false", "no", "off", "n", "f"}
E2A_MAX_EXTEND_DAYS = 10
#: 延持期退出：连续两次**已完成收盘**低于 SMA20 → 下一合法交易时点退出。
E2A_BELOW_MA20_EXIT_STREAK = 2

#: T5 NDX 独立辅助仓（只在现有指数策略提醒与主界面范围内展示计划，不重建总账）。
NDX_AUX_INITIAL_RATIO = 0.15
NDX_AUX_TARGET_RATIO = 0.215
NDX_AUX_MAX_RATIO = 0.25
NDX_AUX_TRIM_RATIO = 0.245
NDX_AUX_ADD_INTERVAL_DAYS = 10
NDX_AUX_CASH_BUFFER = 250000.0
NDX_AUX_MIN_ADD_AMOUNT = 5000.0

#: 通知去重的键前缀（使用 DBManager 的 kv_store 命名空间，SQLite / PostgreSQL 一致）。
STATE_KEY_PREFIX = "index_strategy:v1:"

#: PushPlus 通道参数。Token 只从环境变量读取，绝不硬编码。
PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
PUSHPLUS_TIMEOUT_SECONDS = 8
PUSHPLUS_MAX_ATTEMPTS = 3

#: 收盘扫描回看的交易日数（用于 App 重启 / 断网后补查已收盘事件）。
ALERT_BACKFILL_TRADING_DAYS = 5

#: 收盘「临近突破·关注」提醒的触发距离：现价距突破位不足这么多倍 ATR 就提醒。
#:
#: 口径：``gap_atr = (突破位 - 现价) / ATR14``，只提醒 ``0 < gap_atr <= 该值`` 的标的——
#: 已经突破的（gap<=0）由买入提醒负责，离得远的没必要天天提醒。
#: 未持仓才是"关注"的对象，已持仓（策略持仓或本地台账）不提醒。
#: 设 0 或负数即关闭该提醒。环境变量 ``INDEX_NEAR_BREAKOUT_ATR``。
def _near_breakout_atr() -> float:
    try:
        return float(str(os.environ.get("INDEX_NEAR_BREAKOUT_ATR", "0.5")).strip())
    except Exception:
        return 0.5


NEAR_BREAKOUT_ATR = _near_breakout_atr()

#: 盘中「达到突破价·立即买入」提醒的轮询间隔（秒）。GUI 定时器按它节流；
#: 外部定时任务建议同样按这个频率跑 ``--trigger``。
#: 环境变量 ``INDEX_INTRADAY_TRIGGER_SECONDS``。
def _intraday_trigger_seconds() -> int:
    try:
        return max(10, int(str(os.environ.get("INDEX_INTRADAY_TRIGGER_SECONDS", "60")).strip()))
    except Exception:
        return 60


INTRADAY_TRIGGER_SECONDS = _intraday_trigger_seconds()

#: 盘中即时提醒的时段起点（市场本地时间）。收盘时间取各市场 ``MarketClock.close_time``。
INTRADAY_TRIGGER_START_HHMM = os.environ.get("INDEX_INTRADAY_TRIGGER_START", "09:30").strip() or "09:30"

#: 盘中突破预警：A 股收盘前的预检时刻（市场本地时区 HH:MM，默认 14:55）。
#: 窗口为「该时刻 ~ 当日收盘」，只做收盘前提醒，不替代收盘确认；
#: 同一指数同一交易日只推一次（事件键含日期），可用 ``INDEX_INTRADAY_ALERT_HHMM`` 覆盖。
INTRADAY_ALERT_HHMM = os.environ.get("INDEX_INTRADAY_ALERT_HHMM", "14:55").strip() or "14:55"


def intraday_alert_time():
    """解析 ``INTRADAY_ALERT_HHMM``；非法值回退 14:55（不抛异常，避免打断定时循环）。"""
    from datetime import time as _dt_time

    try:
        hour, minute = str(INTRADAY_ALERT_HHMM).split(":")
        return _dt_time(int(hour), int(minute))
    except Exception:
        return _dt_time(14, 55)

#: 研究执行器的费率口径（来自 eight_index_ab/code/execution_models.py::FeeSchedule 默认值）。
FEE_BUY_RATE = 0.0005
FEE_SELL_RATE = 0.0005
FEE_TRANSFER_RATE = 0.0
FEE_STAMP_TAX_RATE = 0.0

#: 报价最小单位：ETF / 个股 0.001 元；指数点位显示保留 2 位。
TICK_ETF = 0.001
INDEX_PRICE_DIGITS = 2

#: 时间退出口径说明（V9）。
#:
#: 满 20 个交易日（含建仓日记为第 21 个交易日）先按**原始 N** 判断续期：
#: 收盘仍站在原始 N 日最高价之上 → 按原规则续期（再用一个 20 日周期）；
#: 不续期但持仓盈利且强趋势 → E2a 最多延持 10 个该指数交易日，
#: 延持期连续两次收盘低于 SMA20 即下一合法时点退出；
#: 其余情况当日收盘清仓。硬止损/移动止损始终优先于续持。
TIME_EXIT_SEMANTICS_NOTE = (
    "时间退出（V9）：基础第21个持仓交易日先按原始N判断续期；"
    "不续期但盈利且强趋势（收盘>SMA60、SMA20>SMA60、SMA60上行）时最多延持10个交易日；"
    "延持期连续两次收盘<MA20 则下一交易日开盘退出；否则收盘清仓。"
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except Exception:
        return default


# --------------------------------------------------------------------------------------
# G2：市场状态（14 指数等权基准 ER60）
# --------------------------------------------------------------------------------------
#: 市场状态取值。RANGE=震荡市；UP/DOWN=有方向；UNKNOWN=数据不足（**不套 G2 限制**）。
REGIME_RANGE = "RANGE"
REGIME_UP = "UP"
REGIME_DOWN = "DOWN"
REGIME_UNKNOWN = "UNKNOWN"

#: ER60 窗口（交易日）与阈值：``ER60 < 0.25`` 记 RANGE。
G2_ER60_WINDOW = 60
G2_ER60_THRESHOLD = 0.25

#: 等权基准至少要有这么多只成分（共 14 只）参与才判定状态；不足一律 UNKNOWN。
#: 用 10 只（≈70%）而不是 14 只：个别 QDII 停牌/缺一天不该让整张表的 G2 失效。
G2_REGIME_MIN_COMPONENTS = max(3, min(14, _env_int("INDEX_G2_MIN_COMPONENTS", 10)))

#: G2-1：震荡市 + 止损退出后的冷却要求（有效交易日）。
G2_RANGE_COOLDOWN_TRADING_DAYS = 10

#: G2 总开关（关闭后完全退回 V9 原行为，用于对账/回滚）。
G2_ENABLED = _env_bool("INDEX_G2_ENABLED", True)

#: G2-2 开关：震荡市禁止盈利加仓。
G2_RANGE_PYRAMID_BLOCKED = _env_bool("INDEX_G2_RANGE_PYRAMID_BLOCKED", True)

#: 触发「延长冷却」的退出动作：普通止损 + 跳空止损。时间退出（正常到期）不延长。
G2_EXTENDED_COOLDOWN_ACTIONS = ("STOP", "GAP_STOP")

#: 退出动作的中文短标签（冷却原因展示用）。
G2_EXIT_ACTION_LABELS = {
    "STOP": "普通止损",
    "GAP_STOP": "跳空止损",
    "EXIT": "时间退出（正常到期）",
    "CLOSED": "已清仓",
}

G2_NOTE = (
    "G2：14 指数等权基准 ER60 <0.25 记 RANGE 震荡市（≥0.25 且过去60日上涨记 UP、下跌记 DOWN）。"
    "RANGE 且上次为普通/跳空止损退出 → 冷却延长到 10 个有效交易日；"
    "RANGE 下禁止 S1b+K6 盈利加仓（不算成功加仓）；首次突破买入与卖出不受 G2 限制。"
)


#: 严格因子校验：无法核实的因子一律判为"因子未验证"，不产生正式买入提醒。
STRICT_FACTOR_VERIFICATION = _env_bool("INDEX_STRATEGY_STRICT_FACTORS", True)

#: 允许按"标准公式"评估未核实因子（仅在用户明确接受口径风险时打开）。
ALLOW_UNVERIFIED_FACTORS = _env_bool("INDEX_STRATEGY_ALLOW_UNVERIFIED_FACTORS", False)

#: 代理行情开关：ETF 代理只作为参考行展示，默认不用于生成指数信号。
ALLOW_ETF_PROXY_SIGNAL = _env_bool("INDEX_STRATEGY_ALLOW_ETF_PROXY_SIGNAL", False)

#: 因子组合闸门：off=不计算 / warn=只提示（默认） / block=不通过则不出正式买入提醒。
#: 组合来自 ``breakout_atr_optimization`` 的同样本研究，未做走向前验证，故默认只提示。
FACTOR_COMBO_GATE = str(os.environ.get("INDEX_STRATEGY_FACTOR_COMBO_GATE", "warn")).strip().lower()
if FACTOR_COMBO_GATE not in {"off", "warn", "block"}:
    FACTOR_COMBO_GATE = "warn"

#: 每只指数落地的最优因子组合（实现见 ``factor_combo.COMBO_SPECS``）。
FACTOR_COMBO_BY_INDEX: Dict[str, str] = {
    "000001.SH": "mom20+volume",
    "000300.SH": "mom20+mom60",
    "000905.SH": "volume+breakout",
    "000852.SH": "mom20+volume",
    "399001.SZ": "mom20+volume",
    "399006.SZ": "mom20+volume",
    "000688.SH": "mom60+volume",
    "932000.CSI": "mom20+volume",
    "NDX": "mom20+rev5",
    # 恒生科技无正期望组合（研究中不含资金因子的最优配对为负），故意不配置 → 闸门显示「关闭」。
}


@dataclass(frozen=True)
class FactorSpec:
    """额外入场因子的登记项。"""

    key: str
    label: str
    verified: bool
    definition: str
    source: str


FACTOR_DVOL_RANGE_20 = "DVOL_RANGE_20"
FACTOR_MA_BREADTH_60 = "MA_BREADTH_60"
FACTOR_TREND_EFF_20 = "TREND_EFF_20"
FACTOR_BREAKOUT_BREADTH_20 = "BREAKOUT_BREADTH_20"
FACTOR_DVOL_MAX_20 = "DVOL_MAX_20"

FACTOR_SPECS: Dict[str, FactorSpec] = {
    FACTOR_DVOL_RANGE_20: FactorSpec(
        key=FACTOR_DVOL_RANGE_20,
        label="20日下行波动率在 0.090218 ~ 0.111459 之间",
        verified=False,
        definition="20 个交易日日收益的下行标准差（只取负收益样本），未年化。",
        source=(
            "仓库内无该阈值的研究代码；needs_research: 年化方式、波动率单位、样本门槛、缺失处理均未核实。"
        ),
    ),
    FACTOR_MA_BREADTH_60: FactorSpec(
        key=FACTOR_MA_BREADTH_60,
        label="全市场其他指数 60 日均线广度 >= 0.857143",
        verified=False,
        definition="同市场其他指数中，收盘价站上自身 60 日均线的比例。",
        source="仓库内无该阈值的研究代码；广度分母范围（是否含自身/含黄金等）未核实。",
    ),
    FACTOR_TREND_EFF_20: FactorSpec(
        key=FACTOR_TREND_EFF_20,
        label="20日趋势效率 >= 0.287831",
        verified=False,
        definition="20 日净变动绝对值 / 20 日逐日变动绝对值之和（Kaufman 效率比形式）。",
        source="仓库内无该阈值的研究代码；趋势效率公式与窗口端点未核实。",
    ),
    FACTOR_BREAKOUT_BREADTH_20: FactorSpec(
        key=FACTOR_BREAKOUT_BREADTH_20,
        label="同市场其他指数 20日突破广度 >= 0.228571",
        verified=False,
        definition="同市场其他指数中，当日收盘创 20 日新高（收盘 > 前 20 日最高收盘）的比例。",
        source="仓库内无该阈值的研究代码；突破用收盘价还是最高价、分母范围未核实。",
    ),
    FACTOR_DVOL_MAX_20: FactorSpec(
        key=FACTOR_DVOL_MAX_20,
        label="20日下行波动率 <= 0.198872",
        verified=False,
        definition="20 个交易日日收益的下行标准差（只取负收益样本），未年化。",
        source="仓库内无该阈值的研究代码；needs_research: 年化方式与缺失处理未核实。",
    ),
}


@dataclass(frozen=True)
class IndexStrategyConfig:
    """单只指数的冻结策略参数。"""

    index_id: str
    name: str
    market: str
    breakout_window: int
    initial_stop_atr: float
    trail_trigger_atr: Optional[float]
    exit_after_bars: int
    #: 跟踪止盈的「峰值回撤比例」（百分比数值，8.0 表示峰值回撤 8% 即清仓）。
    #: 这是模型 v1.0 唯一的跟踪口径；v3 的「峰值 − k×ATR」距离档已于 2026-11-01 删除。
    trail_drawdown_pct: Optional[float] = None
    #: V9 E2a 到期延持：开启后基础到期日不必然清仓，盈利+强趋势可延持
    #: ``e2a_max_extend_days`` 个该指数交易日。生产 14 只在下方统一置为 True。
    e2a_enabled: bool = False
    e2a_max_extend_days: int = E2A_MAX_EXTEND_DAYS
    factor_key: Optional[str] = None
    #: 该指数对应的可交易 ETF（LOF）代码——v6 起它就是策略的**实际标的**，
    #: 不再只是「代理」。代码只写已核实的，宁可留空也不猜。
    etf_code: Optional[str] = None
    etf_name: Optional[str] = None
    #: 所属轨道：``TRACK_CN``（默认，A 股 ETF）或 ``TRACK_US``（NDX 独立轨）。
    track: str = TRACK_CN

    @property
    def is_us_track(self) -> bool:
        """是否美股独立轨（目前只有 NDX）。"""
        return str(self.track) == TRACK_US

    @property
    def track_label(self) -> str:
        return TRACK_LABELS.get(str(self.track), TRACK_LABELS[TRACK_CN])

    @property
    def entry_window(self) -> int:
        """首次买入的突破窗口：美股轨=自身 N；A 股轨=T3 的 0.75N（不足 1 日按 1 日）。"""
        try:
            original = int(self.breakout_window)
        except (TypeError, ValueError):
            return 1
        if original <= 0:
            return 1
        if self.is_us_track:
            return original
        return max(1, int(math.floor(T3_WINDOW_FACTOR * original)))

    @property
    def counts_against_core_slots(self) -> bool:
        """是否占用 V9 的 8 个核心指数槽位（美股轨独立，不占）。"""
        return not self.is_us_track

    @property
    def pyramid_enabled(self) -> bool:
        """是否参与 S1b+K6 的 62,500 盈利加仓（美股轨走 T5 辅助仓，不参与）。"""
        return not self.is_us_track

    @property
    def renewal_enabled(self) -> bool:
        """到期日是否按原始 N 判断续期（美股轨固定 20 日无条件清仓）。"""
        return not self.is_us_track

    @property
    def uses_etf_underlying(self) -> bool:
        """本只指数当前是否按 ETF 成交价评估（受模块开关 ``ETF_UNDERLYING`` 控制）。"""
        return bool(ETF_UNDERLYING and self.etf_code)

    @property
    def price_unit(self) -> str:
        """价格单位：ETF 标的为「元」，指数标的为「点」。"""
        return "元" if self.uses_etf_underlying else "点"

    @property
    def price_digits(self) -> int:
        """价格小数位：ETF 按 3 位（0.001 元一档），指数点位按 2 位。"""
        return 3 if self.uses_etf_underlying else INDEX_PRICE_DIGITS

    @property
    def clock_market(self) -> str:
        """算交易日用的市场：ETF 标的全部在 A 股场内成交，故一律按 A 股日历。"""
        return MARKET_CN if self.uses_etf_underlying else self.market

    @property
    def underlying_label(self) -> str:
        """表格「指数」列的显示名：ETF 口径下直接标明是 ETF。"""
        return f"{self.name}ETF" if self.uses_etf_underlying else self.name

    @property
    def underlying_code(self) -> str:
        return str(self.etf_code or "") if self.uses_etf_underlying else self.index_id

    @property
    def trail_enabled(self) -> bool:
        """跟踪止盈是否启用。启动倍数与峰值回撤比例都配齐才算启用。"""
        return self.trail_trigger_atr is not None and self.trail_drawdown_pct is not None

    def exit_label(self) -> str:
        """时间退出的中文展示文本（V9：基础到期 → 原始N续期 → E2a 延持≤10日）。"""
        base = (
            f"第{self.exit_after_bars}个持仓交易日（建仓后满{self.exit_after_bars - 1}个交易日）"
        )
        if self.is_us_track:
            return f"{base}收盘无条件清仓（美股独立轨：不做原始N续期、不延持）"
        if self.e2a_enabled:
            return (
                f"{base}先按原始N={self.breakout_window}日突破续期；不续期且盈利+强趋势时"
                f"最多延持{self.e2a_max_extend_days}个交易日，连续2日收盘<MA20 次日开盘退出"
            )
        return f"{base}收盘清仓（E2a 关闭）"

    def trail_label(self) -> str:
        """跟踪止损规则的中文展示文本（关闭时返回「关闭」）。"""
        if not self.trail_enabled:
            return "关闭"
        trigger = _fmt_num(self.trail_trigger_atr)
        return f"{trigger}倍ATR启动 · 峰值回撤{_fmt_num(self.trail_drawdown_pct)}%清仓"

    def stop_label(self) -> str:
        return f"{_fmt_num(self.initial_stop_atr)}ATR"


def _fmt_num(value: float) -> str:
    text = f"{float(value):.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


#: 顺序与用户给定的参数表严格一致（界面默认行序即此顺序）。
#:
#: 参数来源（模型 v1.0，2026-11-01）：
#: - N（``breakout_window``）、C（``initial_stop_atr``）、A（``trail_trigger_atr``）、
#:   B（``trail_drawdown_pct``）全部取自 ``best_params_v2.json`` 的**原始寻优参数**
#:   （2015-01 起全样本、5600 组网格 + 邻域精修、目标 CAGR）。
#:   用户 2026-11-01 明确要求：不采用在 w=20 模型下二次寻优得到的新参数
#:   （样本内 +2.76pp，但分半与年度 walk-forward 均未跑赢原参数）。
#: - 时间退出统一为「建仓后满 20 个交易日、当日收盘无条件清仓」，
#:   故 ``exit_after_bars`` 一律为 21（含建仓日计数）；旧模型的「第5日减半」已删除。
#: - 保本档与「风险预算」两列已在 2026-11-01 删除（前者模型里没有，后者与 20% 固定仓位
#:   互斥且数值与真实风险差 0.1~25 倍，见 ``docs/INDEX_STRATEGY.md``）。
#:   当前唯一的仓位口径是 ``PORTFOLIO_POSITION_CAP`` / ``PORTFOLIO_MAX_POSITIONS``。
INDEX_STRATEGY_CONFIGS: Tuple[IndexStrategyConfig, ...] = (
    IndexStrategyConfig(
        index_id="000016.SH",
        name="上证50",
        market=MARKET_CN,
        breakout_window=40,
        initial_stop_atr=0.5,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=8.0,
        # 510050 上证50ETF华夏（东财现名，2026-11-02 核对）
        etf_code="510050",
        etf_name="上证50ETF华夏(510050)",
    ),
    IndexStrategyConfig(
        index_id="000300.SH",
        name="沪深300",
        market=MARKET_CN,
        breakout_window=5,
        initial_stop_atr=0.25,
        trail_trigger_atr=1.0,
        exit_after_bars=21,
        trail_drawdown_pct=5.0,
        # 510300 沪深300ETF华泰柏瑞（2026-11-02 核对；全市场规模最大的沪深300ETF）
        etf_code="510300",
        etf_name="沪深300ETF华泰柏瑞(510300)",
    ),
    IndexStrategyConfig(
        index_id="000905.SH",
        name="中证500",
        market=MARKET_CN,
        breakout_window=20,
        initial_stop_atr=0.5,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=12.0,
        # 510500 中证500ETF南方（2026-11-02 核对）
        etf_code="510500",
        etf_name="中证500ETF南方(510500)",
    ),
    IndexStrategyConfig(
        index_id="000852.SH",
        name="中证1000",
        market=MARKET_CN,
        breakout_window=15,
        initial_stop_atr=0.5,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=12.0,
        # 512100 中证1000ETF南方（2026-11-02 核对）
        etf_code="512100",
        etf_name="中证1000ETF南方(512100)",
    ),
    IndexStrategyConfig(
        index_id="399001.SZ",
        name="深证成指",
        market=MARKET_CN,
        breakout_window=19,
        initial_stop_atr=0.5,
        trail_trigger_atr=7.0,
        exit_after_bars=21,
        trail_drawdown_pct=3.0,
        # 159943 深证成指ETF大成（2026-11-02 核对）
        etf_code="159943",
        etf_name="深证成指ETF大成(159943)",
    ),
    IndexStrategyConfig(
        index_id="399006.SZ",
        name="创业板指",
        market=MARKET_CN,
        breakout_window=32,
        initial_stop_atr=1.75,
        trail_trigger_atr=4.0,
        exit_after_bars=21,
        trail_drawdown_pct=5.0,
        # 159915 创业板ETF易方达（2026-11-02 核对；跟踪创业板指）
        etf_code="159915",
        etf_name="创业板ETF易方达(159915)",
    ),
    IndexStrategyConfig(
        index_id="000688.SH",
        name="科创50",
        market=MARKET_CN,
        breakout_window=4,
        initial_stop_atr=0.5,
        trail_trigger_atr=6.0,
        exit_after_bars=21,
        trail_drawdown_pct=4.0,
        # 588000 科创50ETF华夏（2026-11-02 核对）
        etf_code="588000",
        etf_name="科创50ETF华夏(588000)",
    ),
    IndexStrategyConfig(
        index_id="932000.CSI",
        name="中证2000",
        market=MARKET_CN,
        breakout_window=21,
        initial_stop_atr=0.75,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=10.0,
        # 159531 中证2000ETF南方（2026-11-02 核对）
        etf_code="159531",
        etf_name="中证2000ETF南方(159531)",
    ),
    IndexStrategyConfig(
        index_id="NDX",
        name="纳斯达克100",
        market=MARKET_US,
        breakout_window=18,
        initial_stop_atr=5.0,
        trail_trigger_atr=4.0,
        exit_after_bars=21,
        trail_drawdown_pct=3.0,
        # 159941 纳指ETF广发（东财现名；此前配置注释写成"国泰"，国泰那只是 513100，
        # 2026-11-02 用行情接口核对后更正）。QDII 场内成交，日历按 A 股。
        #
        # 2026-11-13 双轨：NDX 不再套 A 股的 T3（0.75N）、E2a、8 仓与 62,500 加仓。
        # N=18 / C=5 / A=4 / B=3 本来就是 NDX 自己的寻优值，现在原样生效：
        # 首次突破用满 18 日、满 20 个持仓交易日收盘无条件清仓、仓位走 T5 辅助仓。
        track=TRACK_US,
        etf_code="159941",
        etf_name="纳指ETF广发(159941)",
    ),
    IndexStrategyConfig(
        index_id="HSTECH",
        name="恒生科技",
        market=MARKET_HK,
        breakout_window=20,
        initial_stop_atr=4.0,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=5.0,
        etf_code="513180",
        etf_name="恒生科技ETF华夏(513180)",
    ),
    # ---------------------------------------------------------------- v5 新增 4 只
    # 全部为 A 股行业/风格指数，ETF 代码与跟踪标的已于 2026-11-02 逐只在天天基金
    # F10「基金基本概况」的「业绩比较基准 / 跟踪标的」字段核对过。
    IndexStrategyConfig(
        index_id="931152.CSI",
        name="中证创新药",
        market=MARKET_CN,
        # 159992 创新药ETF银华，成立 2020-03-20，管理费 0.50% + 托管 0.05%
        breakout_window=8,
        initial_stop_atr=2.0,
        trail_trigger_atr=3.0,
        exit_after_bars=21,
        trail_drawdown_pct=3.0,
        etf_code="159992",
        etf_name="创新药ETF银华(159992)",
    ),
    IndexStrategyConfig(
        index_id="H30269.CSI",
        name="中证红利低波",
        market=MARKET_CN,
        # 563020 红利低波ETF易方达，成立 2023-12-06，管理费 0.15% + 托管 0.05%
        # 这条是全池唯一「股息远大于管理费」的标的：2015-2026 实测股息 4.84%/年。
        breakout_window=5,
        initial_stop_atr=2.0,
        trail_trigger_atr=0.5,
        exit_after_bars=21,
        trail_drawdown_pct=5.0,
        etf_code="563020",
        etf_name="红利低波ETF易方达(563020)",
    ),
    IndexStrategyConfig(
        index_id="H30007.CSI",
        name="中证芯片产业",
        market=MARKET_CN,
        # 159599 芯片ETF东财，成立 2024-04-19，管理费 0.50% + 托管 0.05%
        # 注意：24.66% 的单标的 CAGR 高度依赖 2019-2021 与 2024-2025 两段行情，
        # 且 ETF 成立前的指数历史是回溯编制，实盘预期应大幅打折。
        breakout_window=5,
        initial_stop_atr=0.75,
        trail_trigger_atr=5.0,
        exit_after_bars=21,
        trail_drawdown_pct=8.0,
        etf_code="159599",
        etf_name="芯片ETF东财(159599)",
    ),
    IndexStrategyConfig(
        index_id="399997.SZ",
        name="中证白酒",
        market=MARKET_CN,
        # 161725 招商中证白酒指数(LOF)A，成立 2015-05-27，
        # 管理费 1.00% + 托管 0.20% —— 全池费率最高，已计入费用敏感性测算。
        breakout_window=10,
        initial_stop_atr=1.0,
        trail_trigger_atr=5.0,
        exit_after_bars=21,
        trail_drawdown_pct=3.0,
        etf_code="161725",
        etf_name="招商白酒LOF(161725)",
    ),
)

# V9：A 股 ETF 轨统一启用 E2a 到期延持；美股独立轨（NDX）固定关闭，
# 测试/手工构造的 cfg 默认也关闭。
INDEX_STRATEGY_CONFIGS = tuple(
    replace(cfg, e2a_enabled=bool(E2A_ENABLED and cfg.renewal_enabled))
    for cfg in INDEX_STRATEGY_CONFIGS
)

_BY_ID: Dict[str, IndexStrategyConfig] = {cfg.index_id: cfg for cfg in INDEX_STRATEGY_CONFIGS}


def all_configs() -> Tuple[IndexStrategyConfig, ...]:
    """按用户参数表顺序返回全部指数的冻结配置（v5 起为 14 只）。"""
    return INDEX_STRATEGY_CONFIGS


def get_config(index_id: str) -> IndexStrategyConfig:
    """按规范化指数 ID 取配置；未登记的 ID 抛 KeyError（不允许回退到旧参数）。"""
    try:
        return _BY_ID[str(index_id)]
    except KeyError:
        raise KeyError(f"未登记的指数策略配置: {index_id!r}") from None


def factor_spec(key: Optional[str]) -> Optional[FactorSpec]:
    if not key:
        return None
    return FACTOR_SPECS.get(str(key))


def factor_is_verified(key: Optional[str]) -> bool:
    spec = factor_spec(key)
    if spec is None:
        return True
    return bool(spec.verified)


def unverified_factor_keys() -> Tuple[str, ...]:
    return tuple(k for k, spec in FACTOR_SPECS.items() if not spec.verified)


