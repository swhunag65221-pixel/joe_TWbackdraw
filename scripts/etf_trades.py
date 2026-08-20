#!/usr/bin/env python3
"""列出槓桿 ETF 版的逐筆交易明細（含進場條件與期間極值）。

    python3 scripts/etf_trades.py --market us --since 2018-01-01
    python3 scripts/etf_trades.py --market nq --since 2018-01-01
    python3 scripts/etf_trades.py --market tw --since 2018-01-01

與 `futures_trades.py` 的差別：期貨版的風險刻度是**槓桿倍數**（由停損距離決定），
ETF 版的槓桿固定（00631L 2 倍、UPRO/TQQQ 3 倍），能調的是**部位水位**。
所以這裡印的是水位與「有效槓桿 = 水位 × ETF 槓桿」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import load_csv                                    # noqa: E402
from tw_backdraw.backtest import review_trades, run_backtest        # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402

MARKETS = {
    "tw": dict(index="taiex.csv", etf="00631L.csv", preset="tuned",
               index_name="加權指數", etf_name="00631L", leverage=2.0),
    "us": dict(index="gspc.csv", etf="UPRO.csv", preset="us_tuned",
               index_name="S&P 500", etf_name="UPRO", leverage=3.0),
    "nq": dict(index="ixic.csv", etf="TQQQ.csv", preset="nq_tuned",
               index_name="NASDAQ 綜合", etf_name="TQQQ", leverage=3.0),
}


def load_pair(m: dict):
    """指數日線與 ETF 收盤，取兩者都有的交易日。"""
    idx = load_csv(ROOT / "data" / m["index"])
    etf = {b.d: b.close for b in load_csv(ROOT / "data" / m["etf"])}
    bars = [b for b in idx if b.d in etf]
    return bars, [etf[b.d] for b in bars]


def main() -> int:
    ap = argparse.ArgumentParser(description="槓桿 ETF 逐筆交易明細")
    ap.add_argument("--market", choices=list(MARKETS), default="us")
    ap.add_argument("--preset", default=None, help="預設用該市場的調參結果")
    ap.add_argument("--since", default=None, help="只列出這天之後進場的交易")
    args = ap.parse_args()

    m = MARKETS[args.market]
    cfg = PRESETS[args.preset or m["preset"]]
    bars, etf = load_pair(m)
    print(f"{m['index_name']} 訊號 → {m['etf_name']}（{m['leverage']:g}x）"
          f"　參數組 {args.preset or m['preset']}")
    print(f"期間 {bars[0].d} ~ {bars[-1].d}（{len(bars)} 個交易日）")

    result, _ = run_backtest(bars, cfg, etf)
    reviews = review_trades(result)
    if args.since:
        cut = datetime.strptime(args.since, "%Y-%m-%d").date()
        reviews = [r for r in reviews if r.trade.entry_date >= cut]
        print(f"（只列出 {cut} 之後進場的交易）")
    if not reviews:
        print("\n這段期間沒有交易。")
        return 0

    print()
    for n, r in enumerate(reviews, 1):
        t, s, lv = r.trade, r.setup, r.levels
        exit_txt = str(t.exit_date) if t.exit_reason != "open" else "持有中（尚未平倉）"
        print(f"[{n}] {t.entry_date} → {exit_txt}　持有 {r.bars_held} 個交易日")
        print(f"    進場條件：{s.peak_date} 高點 {s.peak:,.0f} → {s.trough_date} 谷底 "
              f"{s.trough:,.0f}（回檔 {s.drop_pct:.1%}），"
              f"{s.bars_to_repair} 個交易日補回 {s.repair_fraction:.0%}")
        print(f"              訊號日指數 {s.trigger_close:,.0f}（距前高 {r.from_peak:+.1%}）"
              f"　停損線 {lv.stop_line:,.0f}"
              + (f"（警戒線 {lv.warn_line:,.0f} 再扣假跌破緩衝）"
                 if lv.stop_line < lv.warn_line else "（警戒線）")
              + f"　失效線 {lv.invalidation:,.0f}")
        print(f"    距離停損 {r.stop_distance:.2%}　→　部位水位 {r.weight:.0%}"
              f"（有效槓桿 {r.weight * m['leverage']:.2f}x）")
        print(f"    權益報酬 {t.ret:+.1%}　最大報酬 {r.mfe:+.1%}"
              f"　最大不利 {r.mae:+.1%}　期間最大回撤 {r.max_drawdown:.1%}")
        print(f"    出場原因：{r.exit_reason}\n")

    rets = [r.trade.ret for r in reviews]
    wins = sum(1 for x in rets if x > 0)
    dds = [r.max_drawdown for r in reviews]
    print(f"共 {len(rets)} 筆　勝 {wins} 敗 {len(rets) - wins}"
          f"（勝率 {wins / len(rets):.0%}）")
    print(f"權益報酬：中位數 {sorted(rets)[len(rets) // 2]:+.1%}　"
          f"最佳 {max(rets):+.1%}　最差 {min(rets):+.1%}")
    print(f"單筆期間最大回撤：中位數 {sorted(dds)[len(dds) // 2]:.1%}　最深 {min(dds):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
