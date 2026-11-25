"""手动实网验证：腾讯未复权报价与日线（不进 pytest，不写数据库）。

    python tools/verify_market_live.py

对 510300（沪 ETF）、159915（深 ETF）、600519（沪股票）各拉一次即时报价与
最近已完成日线，打印 provider / 价格 / kind / trading_day / observed_at /
quality。任一标的失败即打印原因并以非 0 退出。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

# 手动脚本按 `python tools/verify_market_live.py` 执行：把仓库根加入导入路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.application.market_calendar import (SHANGHAI, CalendarUnavailable,  # noqa: E402
                                                 market_window)
from backend.infrastructure.market.tencent import TencentQuoteProvider  # noqa: E402

TARGETS = (
    {"id": "live-510300", "code": "510300", "market": "SH", "kind": "etf"},
    {"id": "live-159915", "code": "159915", "market": "SZ", "kind": "etf"},
    {"id": "live-600519", "code": "600519", "market": "SH", "kind": "stock"},
)


def show(label: str, quote: dict) -> None:
    print(f"  {label}: price={quote['price']} kind={quote['kind']} "
          f"trading_day={quote['trading_day']} quality={quote['quality']}")
    print(f"    source={quote['source']} price_basis={quote['price_basis']} "
          f"observed_at={quote['observed_at'].isoformat()}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    now = datetime.now(SHANGHAI)
    provider = TencentQuoteProvider()
    try:
        window = market_window(now)
        print(f"now={now.isoformat()} phase={window.phase} day={window.day} "
              f"latest_completed={window.latest_completed}")
        days = {window.latest_completed, window.previous}
    except CalendarUnavailable as exc:
        print(f"FAILED calendar unavailable: {exc}")
        return 2
    failures = 0
    try:
        for target in TARGETS:
            print(f"\n[{target['market']} {target['code']}] provider={provider.name}")
            try:
                show("spot", provider.spot(target, now))
            except Exception as exc:                       # noqa: BLE001 - 实网诊断
                print(f"  spot FAILED: {type(exc).__name__}: {exc}")
                failures += 1
            try:
                bars = provider.closes(target, days, now)
                if not bars:
                    print("  daily FAILED: provider returned no completed session")
                    failures += 1
                for bar in bars:
                    show("daily", bar)
            except Exception as exc:                       # noqa: BLE001 - 实网诊断
                print(f"  daily FAILED: {type(exc).__name__}: {exc}")
                failures += 1
    finally:
        provider.close()
    if failures:
        print(f"\n{failures} live check(s) failed")
        return 1
    print("\nall live checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
