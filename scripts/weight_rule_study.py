#!/usr/bin/env python3
"""UPRO / TQQQ 的部位水位該不該跟著警戒線調整？

    python3 scripts/weight_rule_study.py

ETF 版目前的水位公式是 `風險預算 ÷ (槓桿 × 停損距離)`，但 `min_stop_distance = 5%`
的下限在絕大多數交易裡都會綁住，結果水位幾乎固定在 53%。
本腳本放寬下限，讓水位真的隨「訊號日到警戒線的距離」變動，然後回答兩個問題：

1. 放寬下限之後績效變好，是因為**規則更聰明**，還是只是**曝險變大**？
   → 拿「同樣平均曝險的固定水位」當對照組。這是關鍵的一組比較。
2. 部位規則真正的目的是讓每筆虧損大小一致。虧損筆的離散度有變小嗎？
"""

from __future__ import annotations

import math
import statistics as st
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import load_csv                                # noqa: E402
from tw_backdraw.backtest import review_trades, run_backtest    # noqa: E402
from tw_backdraw.config import PRESETS                          # noqa: E402

MARKETS = (("gspc.csv", "UPRO.csv", "us_tuned", "S&P 500 → UPRO"),
           ("ixic.csv", "TQQQ.csv", "nq_tuned", "NASDAQ → TQQQ"))


def load(ix: str, ef: str):
    idx = load_csv(ROOT / "data" / ix)
    e = {b.d: b.close for b in load_csv(ROOT / "data" / ef)}
    bs = [b for b in idx if b.d in e]
    return bs, [e[b.d] for b in bs]


def metrics(result) -> dict:
    ec = result.equity_curve
    yrs = (ec[-1][0] - ec[0][0]).days / 365.25
    eq = [v for _, v in ec]
    r = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    peak = dd = 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1.0)
    c = eq[-1] ** (1 / yrs) - 1
    return dict(tot=eq[-1] - 1, cagr=c, mdd=dd, calmar=c / abs(dd),
                sharpe=st.fmean(r) / st.pstdev(r) * math.sqrt(252))


def exposure(reviews) -> float:
    """以持有天數加權的平均水位 —— 對照組要對齊的就是這個數。"""
    tot = sum(x.bars_held for x in reviews)
    return sum(x.weight * x.bars_held for x in reviews) / tot if tot else 0.0


def variable(base, msd: float):
    return replace(base, sizing=replace(base.sizing, min_stop_distance=msd))


def constant(base, w: float):
    """強制固定水位 w：把下限拉到遠高於任何實際停損距離，再用風險預算反推。"""
    big = 0.50
    return replace(base, sizing=replace(base.sizing, min_stop_distance=big,
                                        risk_per_trade=base.sizing.leverage * big * w,
                                        max_weight=1.0))


HEAD = f"{'方案':<34}{'平均水位':>9}{'CAGR':>8}{'MDD':>9}{'Sharpe':>8}{'Calmar':>8}"


def line(lab, ex, m):
    return (f"{lab:<34}{ex:>9.1%}{m['cagr']:>8.1%}{m['mdd']:>9.1%}"
            f"{m['sharpe']:>8.2f}{m['calmar']:>8.2f}")


def main() -> int:
    for ix, ef, pre, nm in MARKETS:
        bs, px = load(ix, ef)
        base = PRESETS[pre]
        print(f"\n{'=' * 78}\n{nm}\n")

        print("【1】放寬下限 vs 同曝險的固定水位\n")
        print(HEAD)
        for msd in (0.05, 0.04, 0.03, 0.02, 0.0):
            r, _ = run_backtest(bs, variable(base, msd), px)
            rv = review_trades(r)
            ex = exposure(rv)
            tag = f"依停損距離（下限 {msd:.0%}）" + ("　←現行" if msd == 0.05 else "")
            print(line(tag, ex, metrics(r)))
            r2, _ = run_backtest(bs, constant(base, ex), px)
            print(line("  → 同曝險的固定水位對照", ex, metrics(r2)))

        print("\n【2】純固定水位的參考點\n")
        print(HEAD)
        for w in (0.40, 0.53, 0.70, 0.85):
            r, _ = run_backtest(bs, constant(base, w), px)
            print(line(f"固定水位 {w:.0%}", w, metrics(r)))

        print("\n【3】虧損筆的離散度（部位規則的真正目的）\n")
        for msd, lab in ((0.05, "現行（下限 5%）"), (0.03, "依停損距離（下限 3%）"),
                         (0.0, "純依停損距離（無下限）")):
            r, _ = run_backtest(bs, variable(base, msd), px)
            rv = review_trades(r)
            ex = exposure(rv)
            r2, _ = run_backtest(bs, constant(base, ex), px)
            for tag, v in ((lab, rv), ("  → 同曝險固定水位", review_trades(r2))):
                los = [x.trade.ret for x in v if x.trade.ret <= 0] or [0.0]
                mae = [x.mae for x in v]
                print(f"  {tag:<28}虧損 {len(los)} 筆　平均 {st.fmean(los):+.1%}"
                      f"　標準差 {st.pstdev(los):.1%}　最差 {min(los):+.1%}"
                      f"　MAE 標準差 {st.pstdev(mae):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
