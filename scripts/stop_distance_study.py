#!/usr/bin/env python3
"""檢驗「距離警戒線越遠、越容易獲利」這個假說（台指期版）。

    python3 scripts/stop_distance_study.py

輸出四段：
1. 逐筆的停損距離、回檔幅度、報酬（依停損距離排序）
2. 相關與**偏相關** —— 停損距離與回檔幅度高度共線，必須拆開看
3. 分組的勝率與賠率
4. 依這個假說調整策略（過濾近停損訊號、改固定槓桿）之後的實際績效

結論寫在 docs/strategy.md §14。
"""

from __future__ import annotations

import math
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402
from tw_backdraw.engine import Engine                               # noqa: E402
from tw_backdraw.futures import (FuturesCost, FuturesEntry,         # noqa: E402
                                 entries_from_trades, trade_details,
                                 vehicle_series)

SPLIT = 0.03        # 分組用的停損距離門檻
ERA = date(2014, 1, 1)


def corr(a, b) -> float:
    ma, mb = st.fmean(a), st.fmean(b)
    sa, sb = st.pstdev(a), st.pstdev(b)
    if not sa or not sb:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (sa * sb)


def partial(x, y, z) -> float:
    """x 與 y 在控制 z 之後的偏相關。"""
    rxy, rxz, ryz = corr(x, y), corr(x, z), corr(y, z)
    d = math.sqrt((1 - rxz ** 2) * (1 - ryz ** 2))
    return (rxy - rxz * ryz) / d if d else 0.0


def metrics(nav, dates) -> dict:
    yrs = (dates[-1] - dates[0]).days / 365.25
    r = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav)) if nav[i - 1]]
    peak = dd = 0.0
    for x in nav:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1.0)
    cagr = nav[-1] ** (1 / yrs) - 1
    return dict(tot=nav[-1] - 1, cagr=cagr, mdd=dd, calmar=cagr / abs(dd),
                sharpe=st.fmean(r) / st.pstdev(r) * math.sqrt(252))


def main() -> int:
    login()
    cfg = PRESETS["tuned"]
    bars, fs, _ = load_tx()
    dates, ic = [b.d for b in bars], [b.close for b in bars]
    res = Engine(cfg).run(bars, fs)
    base = entries_from_trades(res.trades, bars, cfg, 5.0, same_day=True)
    nav, det = vehicle_series(dates, fs, base, FuturesCost(), ic)
    D = trade_details(dates, nav, base, det)

    print("【1】逐筆（依停損距離排序）\n")
    print(f"{'進場':<12}{'停損距離':>9}{'回檔':>8}{'距前高':>8}{'槓桿':>7}"
          f"{'期貨報酬':>10}{'權益報酬':>10}")
    for d in sorted(D, key=lambda d: d.trade.stop_distance):
        t, s = d.trade, d.setup
        print(f"{t.entry_date!s:<12}{t.stop_distance:>9.2%}{s.drop_pct:>8.1%}"
              f"{d.from_peak:>8.1%}{t.leverage:>7.2f}"
              f"{t.futures_return:>10.1%}{t.ret:>10.1%}")

    sd = [d.trade.stop_distance for d in D]
    dp = [d.setup.drop_pct for d in D]
    fr = [d.trade.futures_return for d in D]
    print(f"\n【2】相關與偏相關（n={len(D)}）\n")
    print(f"  停損距離 vs 期貨報酬                {corr(sd, fr):+.3f}")
    print(f"  回檔幅度 vs 期貨報酬                {corr(dp, fr):+.3f}")
    print(f"  停損距離 vs 回檔幅度（共線程度）      {corr(sd, dp):+.3f}")
    print(f"  停損距離 vs 期貨報酬｜控制回檔幅度    {partial(sd, fr, dp):+.3f}")
    print(f"  回檔幅度 vs 期貨報酬｜控制停損距離    {partial(dp, fr, sd):+.3f}")
    for nm, g in (("2014 前", [d for d in D if d.trade.entry_date < ERA]),
                  ("2014 後", [d for d in D if d.trade.entry_date >= ERA])):
        c = corr([d.trade.stop_distance for d in g],
                 [d.trade.futures_return for d in g])
        print(f"  {nm}（n={len(g)}）停損距離 vs 期貨報酬  {c:+.3f}")

    print(f"\n【3】以停損距離 {SPLIT:.0%} 分組\n")
    for nm, g in ((f"停損近 <{SPLIT:.0%}", [d for d in D if d.trade.stop_distance < SPLIT]),
                  (f"停損遠 ≥{SPLIT:.0%}", [d for d in D if d.trade.stop_distance >= SPLIT])):
        f = [d.trade.futures_return for d in g]
        e = [d.trade.ret for d in g]
        win = [x for x in f if x > 0] or [0.0]
        los = [x for x in f if x <= 0] or [0.0]
        print(f"  {nm}　n={len(g)}　勝率 {sum(1 for x in f if x > 0) / len(g):.0%}"
              f"　平均槓桿 {st.fmean([d.trade.leverage for d in g]):.2f}x")
        print(f"     期貨：平均獲利 {st.fmean(win):+.1%}　平均虧損 {st.fmean(los):+.1%}"
              f"　賠率 {abs(st.fmean(win) / st.fmean(los)):.1f}　期望值 {st.fmean(f):+.1%}")
        print(f"     權益：期望值 {st.fmean(e):+.1%}　中位數 {st.median(e):+.1%}"
              f"　最佳 {max(e):+.1%}　最差 {min(e):+.1%}")

    print("\n【4】依這個假說調整之後的實際績效\n")
    print(f"{'方案':<30}{'筆數':>5}{'勝率':>7}{'總報酬':>11}{'CAGR':>8}"
          f"{'MDD':>8}{'Sharpe':>8}{'Calmar':>8}")

    def show(nm, ents):
        nv, dt = vehicle_series(dates, fs, ents, FuturesCost(), ic)
        m = metrics(nv, dates)
        w = sum(1 for t in dt if t.ret > 0) / len(dt) if dt else 0.0
        print(f"{nm:<30}{len(dt):>5}{w:>7.0%}{m['tot']:>11.0%}{m['cagr']:>8.1%}"
              f"{m['mdd']:>8.1%}{m['sharpe']:>8.2f}{m['calmar']:>8.2f}")

    def re_lev(e, L):
        return FuturesEntry(e.entry_i, e.exit_i, L, e.entry_index,
                            e.stop_distance, e.trade)

    show("現行：8% ÷ 停損距離 ≤5x", base)
    for L in (2.0, 2.5):
        show(f"固定槓桿 {L:g}x", [re_lev(e, L) for e in base])
    for thr in (0.025, 0.030, 0.040):
        show(f"只做停損距離 ≥{thr:.1%}",
             [e for e in base if e.stop_distance >= thr])
    for thr in (0.16, 0.18):
        show(f"只做回檔 ≥{thr:.0%}",
             [e for e in base if e.trade.setup.drop_pct >= thr])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
