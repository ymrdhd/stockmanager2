"""指数 ↔ 行情标识的显式映射（绝不从代码里抽数字猜指数）。

原项目 ``subroutine/index_strategy/symbols.py`` 用一张大别名表加拒绝清单来避免
"000001.SZ 是平安银行，不是上证综指"这类事故。迁移后仍保留同样的原则，只是把
别名表压缩成本策略真正用到的 14 个指数 + 14 个 ETF：

- ``INDEX_SYMBOLS``：每个指数的行情标识（腾讯 ``sz/sh`` 代码、中证官网代码、
  以及"这个指数在新浪/腾讯有没有行情"的事实）。
- ``REJECTED_TOKENS``：明确拒绝的输入，给出可读原因，不允许静默归一。
- 指数代码必须显式登记；未登记的抛 ``IndexCodeError``，不回退到任何猜测。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import MARKET_CN, MARKET_HK, MARKET_US, IndexStrategyConfig, all_configs


class IndexCodeError(ValueError):
    """无法识别的指数代码 / 已知被拒绝的代码。"""


@dataclass(frozen=True)
class IndexSymbol:
    """一个指数在各类行情源里的标识。"""

    index_id: str
    name: str
    market: str
    #: 腾讯/新浪通用行情代码，例如 ``sh000016`` / ``sz399001`` / ``usNDX`` / ``hkHSTECH``。
    quote_code: str
    #: 中证指数官网 ``indexCode``（仅中证系列与中证编制的沪深指数有）。
    csindex_code: Optional[str] = None
    #: 主流行情源（腾讯、新浪）是否提供该指数日线。
    vendor_covered: bool = True
    #: 该行情是不是指数本身（False 表示只能拿到代理，禁止用于正式信号）。
    is_index_level: bool = True

    @property
    def price_unit(self) -> str:
        return "点"


INDEX_SYMBOLS: Dict[str, IndexSymbol] = {
    # A 股 / 深证 / 中证：腾讯与新浪都覆盖
    "000016.SH": IndexSymbol("000016.SH", "上证50", MARKET_CN, "sh000016", "000016"),
    "000300.SH": IndexSymbol("000300.SH", "沪深300", MARKET_CN, "sh000300", "000300"),
    "000905.SH": IndexSymbol("000905.SH", "中证500", MARKET_CN, "sh000905", "000905"),
    "000852.SH": IndexSymbol("000852.SH", "中证1000", MARKET_CN, "sh000852", "000852"),
    "399001.SZ": IndexSymbol("399001.SZ", "深证成指", MARKET_CN, "sz399001", None),
    "399006.SZ": IndexSymbol("399006.SZ", "创业板指", MARKET_CN, "sz399006", None),
    "000688.SH": IndexSymbol("000688.SH", "科创50", MARKET_CN, "sh000688", "000688"),
    # 中证 93xxxx / H30xxx 系列：腾讯、新浪都没有日线，只能走中证指数官网
    "932000.CSI": IndexSymbol("932000.CSI", "中证2000", MARKET_CN, "", "932000",
                             vendor_covered=False),
    "931152.CSI": IndexSymbol("931152.CSI", "中证创新药", MARKET_CN, "", "931152",
                             vendor_covered=False),
    "H30269.CSI": IndexSymbol("H30269.CSI", "中证红利低波", MARKET_CN, "", "H30269",
                             vendor_covered=False),
    "H30007.CSI": IndexSymbol("H30007.CSI", "中证芯片产业", MARKET_CN, "", "H30007",
                             vendor_covered=False),
    "399997.SZ": IndexSymbol("399997.SZ", "中证白酒", MARKET_CN, "sz399997", None),
    # 海外指数：腾讯提供（usNDX / hkHSTECH），名称需断言，避免拿综合指数冒充
    "NDX": IndexSymbol("NDX", "纳斯达克100", MARKET_US, "usNDX", None),
    "HSTECH": IndexSymbol("HSTECH", "恒生科技", MARKET_HK, "hkHSTECH", None),
}

#: 腾讯返回的指数名称必须包含其中之一，否则视为"不是该指数"。
OVERSEAS_NAME_ASSERTIONS: Dict[str, Tuple[str, ...]] = {
    "NDX": ("纳斯达克100", "纳斯达克-100", "NASDAQ-100", "NASDAQ 100", "NDX"),
    "HSTECH": ("恒生科技", "HSTECH", "HANG SENG TECH"),
}

#: 明确拒绝的输入：给出原因，绝不"尽力解释成某个指数"。
REJECTED_TOKENS: Dict[str, str] = {
    "000001.SH": "上证综指已被上证50（000016.SH）替代；本策略不交易上证综指",
    "000001.SZ": "000001.SZ 是平安银行股票，不是上证综指",
    "^IXIC": "纳指综合（^IXIC）不是纳斯达克100（NDX），两者成分与价格都不同",
    "IXIC": "纳指综合（IXIC）不是纳斯达克100（NDX）",
    "SH000001": "上证综指已被上证50（000016.SH）替代",
}

#: ETF 代码 → 指数：来自原 ``etf_link.py`` 逐只在基金档案核对过的跟踪标的。
ETF_INDEX_MAP: Dict[str, str] = {
    "510050": "000016.SH",
    "510300": "000300.SH",
    "510500": "000905.SH",
    "512100": "000852.SH",
    "159943": "399001.SZ",
    "159915": "399006.SZ",
    "588000": "000688.SH",
    "159531": "932000.CSI",
    "159941": "NDX",
    "513180": "HSTECH",
    "159992": "931152.CSI",
    "563020": "H30269.CSI",
    "159599": "H30007.CSI",
    "161725": "399997.SZ",
}

ETF_MAP_ENV = "INDEX_STRATEGY_ETF_MAP"


def etf_quote_code(code: str) -> str:
    """把 6 位 ETF 代码变成腾讯行情代码：5xx → 沪市，1xx → 深市。"""
    text = str(code or "").strip()
    if len(text) != 6 or not text.isdigit():
        return ""
    return ("sh" if text.startswith("5") else "sz") + text


def etf_map_overrides() -> Dict[str, str]:
    """``INDEX_STRATEGY_ETF_MAP="588000:000688.SH,..."`` 形式的人工覆盖。"""
    raw = os.environ.get(ETF_MAP_ENV, "").strip()
    out: Dict[str, str] = {}
    if not raw:
        return out
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part or ":" not in part:
            continue
        code, index_id = (value.strip() for value in part.split(":", 1))
        if code and index_id in INDEX_SYMBOLS:
            out[code] = index_id
    return out


def normalize_index_id(value: str) -> str:
    """把用户/配置里的指数标识归一成 ``INDEX_SYMBOLS`` 的键。"""
    text = str(value or "").strip()
    if not text:
        raise IndexCodeError("指数代码为空")
    upper = text.upper()
    if upper in REJECTED_TOKENS:
        raise IndexCodeError(REJECTED_TOKENS[upper])
    if text in INDEX_SYMBOLS:
        return text
    if upper in INDEX_SYMBOLS:
        return upper
    # 中文名反查（只认登记过的名字，不做模糊匹配）
    for index_id, symbol in INDEX_SYMBOLS.items():
        if text == symbol.name:
            return index_id
    raise IndexCodeError(f"未登记的指数代码：{text}")


def symbol_of(index_id: str) -> IndexSymbol:
    return INDEX_SYMBOLS[normalize_index_id(index_id)]


def config_of(index_id: str) -> IndexStrategyConfig:
    from .config import get_config

    return get_config(normalize_index_id(index_id))


def peers_of(index_id: str) -> Tuple[str, ...]:
    """同市场的其他指数（供广度因子使用），不含自身。"""
    target = normalize_index_id(index_id)
    market = INDEX_SYMBOLS[target].market
    return tuple(other for other, symbol in INDEX_SYMBOLS.items()
                 if symbol.market == market and other != target)


def index_for_etf(code: str) -> Optional[str]:
    """ETF 代码 → 指数（先看人工覆盖，再看内置映射）。"""
    text = str(code or "").strip()
    if not text:
        return None
    overrides = etf_map_overrides()
    if text in overrides:
        return overrides[text]
    return ETF_INDEX_MAP.get(text)


def etf_code_of(index_id: str) -> Optional[str]:
    target = normalize_index_id(index_id)
    for code, mapped in {**ETF_INDEX_MAP, **etf_map_overrides()}.items():
        if mapped == target:
            return code
    return None


def configured_index_ids() -> Tuple[str, ...]:
    return tuple(cfg.index_id for cfg in all_configs())


__all__ = [
    "ETF_INDEX_MAP",
    "ETF_MAP_ENV",
    "INDEX_SYMBOLS",
    "OVERSEAS_NAME_ASSERTIONS",
    "REJECTED_TOKENS",
    "IndexCodeError",
    "IndexSymbol",
    "config_of",
    "configured_index_ids",
    "etf_code_of",
    "etf_map_overrides",
    "etf_quote_code",
    "index_for_etf",
    "normalize_index_id",
    "peers_of",
    "symbol_of",
]
