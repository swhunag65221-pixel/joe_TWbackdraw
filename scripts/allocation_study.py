#!/usr/bin/env python3
"""三個市場的策略要怎麼配置新資金 —— 相關性、分段穩定性、混合績效。

    python3 scripts/allocation_study.py

比較台指期、UPRO、TQQQ 三條策略淨值曲線在共同期間（2016 起）的表現。
輸出：日報酬相關矩陣、在場重疊、各種配置的績效、前後半段的穩定性檢驗，
以及網格最佳解與它的平坦程度。

混合假設每日再平衡 —— 期貨是整數口數，實務做不到，
所以這裡的數字是**上界**，用來看方向而不是拿來當目標。
"""

from __future__ import annotations

import itertools
import math
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw import load_csv                                    # noqa: E402
from tw_backdraw.backtest import run_backtest                       # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402
from tw_backdraw.engine import Engine                               # noqa: E402
from tw_backdraw.futures import (FuturesCost, entries_from_trades,  # noqa: E402
                                 vehicle_series)

NAMES = ["TX期貨", "UPRO", "TQQQ"]
START = date(2016, 1, 4)          # UPRO / TQQQ 本地資料的起點


def build() -> tuple[dict, dict]:
    """回傳 (淨值曲線, 是否在場) 兩個 {名稱: {日期: 值}}。"""
    curves: dict = {}
    inpos: dict = {}

    cfg = PRESETS["tuned"]
    bars, fs, _ = load_tx()
    dates, ic = [b.d for b in bars], [b.close for b in bars]
    res = Engine(cfg).run(bars, fs)
    ent = entries_from_trades(res.trades, bars, cfg, 5.0, same_day=True)
    nav, _ = vehicle_series(dates, fs, ent, FuturesCost(), ic)
    curves["TX期貨"] = dict(zip(dates, nav))
    held: set = set()
    for e in ent:
        b = e.exit_i if e.exit_i is not None else len(dates) - 1
        held.update(dates[e.entry_i:b + 1])
    inpos["TX期貨"] = {d: d in held for d in dates}

    for ix, ef, pre, nm in (("gspc.csv", "UPRO.csv", "us_tuned", "UPRO"),
                            ("ixic.csv", "TQQQ.csv", "nq_tuned", "TQQQ")):
        idx = load_csv(ROOT / "data" / ix)
        e = {b.d: b.close for b in load_csv(ROOT / "data" / ef)}
        bs = [b for b in idx if b.d in e]
        r, _ = run_backtest(bs, PRESETS[pre], [e[b.d] for b in bs])
        curves[nm] = dict(r.equity_curve)
        ecd = [d for d, _ in r.equity_curve]
        pos = {d: False for d in ecd}
        for t in r.trades:
            if t.entry_date is None:
                continue
            on = False
            for d in ecd:
                if d == t.entry_date:
                    on = True
                if on:
                    pos[d] = True
                if t.exit_date and d == t.exit_date:
                    break
        inpos[nm] = pos
    return curves, inpos


def corr(a, b) -> float:
    ma, mb = st.fmean(a), st.fmean(b)
    sa, sb = st.pstdev(a), st.pstdev(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (sa * sb) if sa and sb else 0.0


def main() -> int:
    login()
    curves, inpos = build()
    common = sorted(set.intersection(*[set(curves[n]) for n in NAMES]))
    common = [d for d in common if d >= START]
    yrs = (common[-1] - common[0]).days / 365.25
    print(f"共同期間 {common[0]} ~ {common[-1]}（{len(common)} 天，{yrs:.1f} 年）\n")

    R = {}
    for n in NAMES:
        c = [curves[n][d] for d in common]
        R[n] = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]

    print("【1】日報酬相關矩陣")
    print("        " + "".join(f"{n:>10}" for n in NAMES))
    for a in NAMES:
        print(f"{a:<8}" + "".join(f"{corr(R[a], R[b]):>10.2f}" for b in NAMES))

    print("\n【2】在場比例與重疊")
    for n in NAMES:
        print(f"  {n:<10}{sum(1 for d in common if inpos[n].get(d)) / len(common):>6.0%}")
    for a, b in itertools.combinations(NAMES, 2):
        both = sum(1 for d in common if inpos[a].get(d) and inpos[b].get(d))
        print(f"  {a} + {b} 同時在場 {both / len(common):>5.0%}")
    allin = sum(1 for d in common if all(inpos[n].get(d) for n in NAMES))
    none = sum(1 for d in common if not any(inpos[n].get(d) for n in NAMES))
    print(f"  三者同時在場 {allin / len(common):.0%}　三者皆空手 {none / len(common):.0%}")

    def blend(w, lo=0, hi=None):
        hi = len(R[NAMES[0]]) if hi is None else hi
        return [sum(w[i] * R[n][j] for i, n in enumerate(NAMES)) for j in range(lo, hi)]

    def metrics(r, years):
        eq, path = 1.0, [1.0]
        for x in r:
            eq *= 1 + x
            path.append(eq)
        peak = dd = 0.0
        for x in path:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1.0)
        c = path[-1] ** (1 / years) - 1
        sd = st.pstdev(r)
        return dict(cagr=c, mdd=dd, vol=sd * math.sqrt(252),
                    sharpe=st.fmean(r) / sd * math.sqrt(252), calmar=c / abs(dd))

    MIXES = [("100% TX期貨", (1, 0, 0)), ("100% UPRO", (0, 1, 0)),
             ("100% TQQQ", (0, 0, 1)), ("等權 1/3 each", (1 / 3, 1 / 3, 1 / 3)),
             ("TX 50 / UPRO 25 / TQQQ 25", (.5, .25, .25)),
             ("TX 40 / UPRO 30 / TQQQ 30", (.4, .3, .3)),
             ("TX 20 / UPRO 40 / TQQQ 40", (.2, .4, .4)),
             ("UPRO 50 / TQQQ 50", (0, .5, .5))]

    print(f"\n【3】配置績效（每日再平衡；期貨整數口數做不到，數字是上界）\n")
    print(f"{'配置':<28}{'CAGR':>8}{'MDD':>9}{'年化波動':>10}{'Sharpe':>8}{'Calmar':>8}")
    for nm, w in MIXES:
        m = metrics(blend(w), yrs)
        print(f"{nm:<28}{m['cagr']:>8.1%}{m['mdd']:>9.1%}{m['vol']:>10.1%}"
              f"{m['sharpe']:>8.2f}{m['calmar']:>8.2f}")

    print("\n【4】穩定性：前後半段")
    mid = len(common) // 2
    for lab, lo, hi in (("前半 " + str(common[0]) + "~" + str(common[mid]), 0, mid),
                        ("後半 " + str(common[mid]) + "~" + str(common[-1]), mid, len(common) - 1)):
        y = (common[min(hi, len(common) - 1)] - common[lo]).days / 365.25
        print(f"\n  {lab}")
        print(f"  {'配置':<26}{'CAGR':>8}{'MDD':>9}{'Sharpe':>8}{'Calmar':>8}")
        for nm, w in MIXES[:5]:
            m = metrics(blend(w, lo, hi), y)
            print(f"  {nm:<26}{m['cagr']:>8.1%}{m['mdd']:>9.1%}"
                  f"{m['sharpe']:>8.2f}{m['calmar']:>8.2f}")

    print("\n【5】網格最佳解與平坦程度（Calmar 前 8 名）")
    allw = []
    for a in range(11):
        for b in range(11 - a):
            w = (a / 10, b / 10, (10 - a - b) / 10)
            allw.append((metrics(blend(w), yrs), w))
    allw.sort(key=lambda x: -x[0]["calmar"])
    for m, w in allw[:8]:
        print(f"  TX {w[0]:>4.0%} UPRO {w[1]:>4.0%} TQQQ {w[2]:>4.0%}"
              f"　Calmar {m['calmar']:.2f}　CAGR {m['cagr']:.1%}"
              f"　MDD {m['mdd']:.1%}　Sharpe {m['sharpe']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
