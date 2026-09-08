#!/usr/bin/env python3
"""列出台指期策略的逐筆交易明細（含進場條件與期間極值）。

    python3 scripts/futures_trades.py --since 2016-08-20

每筆列出：買賣日期、持有天數、進場當初的實際條件（高點／谷底／回檔幅度／
修復天數與比例／訊號日指數與距前高）、警戒線與失效線、距離停損 %、
據此算出的槓桿，以及報酬、最大報酬（MFE）、最大不利（MAE）、期間最大回撤。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.tx_data import load_tx, login                     # noqa: E402
from tw_backdraw.config import PRESETS                          # noqa: E402
from tw_backdraw.engine import Engine                           # noqa: E402
from tw_backdraw.futures import (FuturesCost, entries_from_trades,  # noqa: E402
                                 format_trade_details, hybrid_entries,
                                 trade_details, vehicle_series)


def summary(details) -> str:
    rets = [d.trade.ret for d in details]
    wins = sum(1 for r in rets if r > 0)
    levs = [d.trade.leverage for d in details]
    dds = [d.max_drawdown for d in details]
    return (f"共 {len(rets)} 筆　勝 {wins} 敗 {len(rets) - wins}"
            f"（勝率 {wins / len(rets):.0%}）\n"
            f"權益報酬：中位數 {sorted(rets)[len(rets) // 2]:+.1%}　"
            f"最佳 {max(rets):+.1%}　最差 {min(rets):+.1%}\n"
            f"槓桿：中位數 {sorted(levs)[len(levs) // 2]:.2f}x　"
            f"範圍 {min(levs):.2f}~{max(levs):.2f}x\n"
            f"單筆期間最大回撤：中位數 {sorted(dds)[len(dds) // 2]:.1%}　"
            f"最深 {min(dds):.1%}")


def main() -> int:
    ap = argparse.ArgumentParser(description="台指期逐筆交易明細")
    ap.add_argument("--preset", default="tuned")
    ap.add_argument("--max-leverage", type=float, default=5.0)
    ap.add_argument("--since", default=None, help="只列出這天之後進場的交易 YYYY-MM-DD")
    ap.add_argument("--next-day-fill", action="store_true",
                    help="改用隔一個交易日收盤成交（預設為當天期貨收盤）")
    ap.add_argument("--sizing", default="hybrid", choices=("hybrid", "risk"),
                    help="hybrid＝最終套件（均線下 3x／均線上半預算，預設）；risk＝舊版 8%% ÷ 停損距離")
    ap.add_argument("--no-step-down", action="store_true",
                    help="關閉 step-down（權益 +150%% → 降到 1x）")
    args = ap.parse_args()

    login()
    cfg = PRESETS[args.preset]
    bars, fseries, missing = load_tx()
    print(f"期間 {bars[0].d} ~ {bars[-1].d}（{len(bars)} 個交易日）"
          f"　成交時點：{'隔一個交易日收盤' if args.next_day_fill else '當天期貨收盤'}")
    print("注碼：" + ("均線下固定 3x／均線上 4% ÷ 停損距離（≤2.5x）" if args.sizing == "hybrid"
                    else f"舊版 8% ÷ 停損距離（≤{args.max_leverage:g}x）")
          + ("　減碼 +150%→1x" if not args.no_step_down else "　不減碼"))
    if missing:
        print(f"⚠ {len(missing)} 個換倉日缺次月報價：{missing[:5]}")

    result = Engine(cfg).run(bars, fseries)
    entries = entries_from_trades(result.trades, bars, cfg, args.max_leverage,
                                  same_day=not args.next_day_fill)
    if args.sizing == "hybrid":
        entries = hybrid_entries(entries, bars, 200, 3.0, 0.5)
    dates = [b.d for b in bars]
    nav, detail = vehicle_series(dates, fseries, entries, FuturesCost(),
                                 [b.close for b in bars],
                                 step_gain=None if args.no_step_down else 1.5)
    details = trade_details(dates, nav, entries, detail)

    if args.since:
        cut: date = datetime.strptime(args.since, "%Y-%m-%d").date()
        details = [d for d in details if d.trade.entry_date >= cut]
        print(f"（只列出 {cut} 之後進場的交易）")

    print()
    print(format_trade_details(details))
    print(summary(details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
