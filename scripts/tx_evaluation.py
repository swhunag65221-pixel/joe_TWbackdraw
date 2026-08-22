#!/usr/bin/env python3
"""台指期策略的全面體檢 —— 並說明每個指標為什麼適合（或不適合）這個策略。

    python3 scripts/tx_evaluation.py

這不是「把常見指標都印一遍」。這個策略有三個結構特徵，使得標準指標會誤導：

1. **72% 的時間空手** —— Sharpe 的分母（波動）與分子（平均報酬）同時被稀釋。
   全期 Sharpe 低估了訊號品質，在場 Sharpe 高估了資金效率，兩個都要看。
2. **27.6 年只有 21 筆交易** —— 任何比率的信賴區間都很寬。必須附上
   t 統計量與 bootstrap 區間，否則所有小數點後兩位都是假精確。
3. **報酬極度集中在少數幾筆** —— 平均值、勝率都不能代表這個策略。
   要看的是集中度與尾部。

另外加兩個這個策略特有的指標：**最長回檔期間（時間而非深度）**，
以及**停損執行落差**（實際虧損 ÷ 進場時假設的停損距離）。
"""

from __future__ import annotations

import argparse
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402
from tw_backdraw.engine import Engine                               # noqa: E402
from tw_backdraw.futures import (FuturesCost, entries_from_trades,  # noqa: E402
                                 trade_details, vehicle_series)

FLAT = 0.005          # 年報酬絕對值小於這個數就算「空白年」


def daily(s: list[float]) -> list[float]:
    return [s[i] / s[i - 1] - 1 for i in range(1, len(s)) if s[i - 1]]


def sortino(r: list[float]) -> float:
    """下檔偏差以 0 為門檻 —— 不是「負報酬之間的標準差」，那會算出比 Sharpe 還小的值。"""
    d = math.sqrt(sum(min(x, 0.0) ** 2 for x in r) / len(r))
    return st.fmean(r) / d * math.sqrt(252) if d else float("nan")


def ulcer(s: list[float]) -> float:
    peak, sq = s[0], 0.0
    for x in s:
        peak = max(peak, x)
        sq += ((x / peak - 1) * 100) ** 2
    return math.sqrt(sq / len(s))


def max_dd(s: list[float]) -> float:
    peak = dd = s[0]
    dd = 0.0
    for x in s:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1.0)
    return dd


def longest_dd(s: list[float], dates) -> tuple[int, object, object]:
    """自峰值到收復的最長期間（交易日）。"""
    peak, peak_i, longest, span = s[0], 0, 0, (0, 0)
    for i, x in enumerate(s):
        if x >= peak:
            if i - peak_i > longest:
                longest, span = i - peak_i, (peak_i, i)
            peak, peak_i = x, i
    if len(s) - 1 - peak_i > longest:
        longest, span = len(s) - 1 - peak_i, (peak_i, len(s) - 1)
    return longest, dates[span[0]], dates[span[1]]


def profile(s: list[float], years: float) -> dict:
    r = daily(s)
    tot = s[-1] / s[0] - 1
    c = (1 + tot) ** (1 / years) - 1
    dd, u = max_dd(s), ulcer(s)
    sd = st.pstdev(r)
    sr = sorted(r)
    k = max(1, int(0.05 * len(sr)))
    return dict(tot=tot, cagr=c, mdd=dd, ulcer=u, vol=sd * math.sqrt(252),
                sharpe=st.fmean(r) / sd * math.sqrt(252), sortino=sortino(r),
                calmar=c / abs(dd), martin=c / (u / 100),
                var=sr[k], cvar=st.fmean(sr[:k]), worst=min(r))


def main() -> int:
    ap = argparse.ArgumentParser(description="台指期策略體檢")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                    help="只餵這天之後的資料（偵測器的高點錨也從那裡重新起算）")
    args = ap.parse_args()

    login()
    cfg = PRESETS["tuned"]
    bars, fs, _ = load_tx()
    if args.start:
        cut = date.fromisoformat(args.start)
        keep = [i for i, b in enumerate(bars) if b.d >= cut]
        if not keep:
            raise SystemExit(f"{cut} 之後沒有資料")
        bars, fs = bars[keep[0]:], fs[keep[0]:]
        print(f"⚠ 只使用 {cut} 之後的資料 —— 更早的歷史完全不存在，"
              f"包括它記錄過的所有大虧損。\n")
    dates, ic = [b.d for b in bars], [b.close for b in bars]
    res = Engine(cfg).run(bars, fs)
    ent = entries_from_trades(res.trades, bars, cfg, 5.0, same_day=True)
    nav, det = vehicle_series(dates, fs, ent, FuturesCost(), ic)
    D = trade_details(dates, nav, ent, det)
    years = (dates[-1] - dates[0]).days / 365.25

    inpos = [False] * len(dates)
    for e in ent:
        b = e.exit_i if e.exit_i is not None else len(dates) - 1
        for i in range(e.entry_i, b + 1):
            inpos[i] = True
    expo = sum(inpos) / len(dates)

    print(f"期間 {dates[0]} ~ {dates[-1]}（{years:.1f} 年，{len(dates):,} 交易日）"
          f"　在場比例 {expo:.0%}\n")

    series = (("策略 TX ≤5x", nav), ("買進持有 台指期", [x / fs[0] for x in fs]),
              ("加權指數", [x / ic[0] for x in ic]))
    P = {nm: profile(s, years) for nm, s in series}

    print("【1】三方對照\n")
    print(f"{'':<22}" + "".join(f"{nm:>18}" for nm, _ in series))
    for lab, k, f in (("總報酬", "tot", "{:.0%}"), ("CAGR", "cagr", "{:.1%}"),
                      ("最大回檔", "mdd", "{:.1%}"), ("Ulcer Index", "ulcer", "{:.1f}"),
                      ("年化波動", "vol", "{:.1%}"), ("Sharpe", "sharpe", "{:.2f}"),
                      ("Sortino", "sortino", "{:.2f}"), ("Calmar", "calmar", "{:.2f}"),
                      ("Martin", "martin", "{:.2f}"), ("日 VaR95", "var", "{:.2%}"),
                      ("日 CVaR95", "cvar", "{:.2%}"), ("最差單日", "worst", "{:.1%}")):
        print(f"{lab:<22}" + "".join(f"{f.format(P[nm][k]):>18}" for nm, _ in series))

    for nm, s in series:
        n, a, b = longest_dd(s, dates)
        print(f"{'最長回檔期間' if nm == series[0][0] else '':<22}"
              f"{nm}: {n:,} 日 ≈ {n / 252:.1f} 年（{a} → {b}）")

    print("\n【2】空手稀釋了什麼 —— 全期 vs 在場\n")
    rin = [nav[i] / nav[i - 1] - 1 for i in range(1, len(dates)) if inpos[i] and nav[i - 1]]
    print(f"  在場日年化波動 {st.pstdev(rin) * math.sqrt(252):.1%}"
          f"　（全期 {P[series[0][0]]['vol']:.1%}）")
    print(f"  在場日 Sharpe {st.fmean(rin) / st.pstdev(rin) * math.sqrt(252):.2f}"
          f"　（全期 {P[series[0][0]]['sharpe']:.2f}）")
    print(f"  曝險調整報酬 CAGR ÷ 在場比例 = {P[series[0][0]]['cagr'] / expo:.0%}"
          f"　（買持在場 100%，CAGR {P['買進持有 台指期']['cagr']:.1%}）")

    print("\n【3】相對買進持有\n")
    R, RF = daily(nav), daily([x / fs[0] for x in fs])
    ma, mb = st.fmean(R), st.fmean(RF)
    beta = sum((x - ma) * (y - mb) for x, y in zip(R, RF)) / len(R) / st.pvariance(RF)
    up = [(x, y) for x, y in zip(R, RF) if y > 0]
    dn = [(x, y) for x, y in zip(R, RF) if y < 0]
    print(f"  Beta {beta:.2f}　年化 Alpha {(ma - beta * mb) * 252:+.1%}")
    print(f"  上漲捕獲 {st.fmean([x for x, _ in up]) / st.fmean([y for _, y in up]):.0%}"
          f"　下跌捕獲 {st.fmean([x for x, _ in dn]) / st.fmean([y for _, y in dn]):.0%}")

    print("\n【4】交易層面\n")
    rt = [d.trade.ret for d in D]
    win = [x for x in rt if x > 0]
    los = [x for x in rt if x <= 0]
    lg = sorted((math.log1p(x) for x in rt), reverse=True)
    tl = sum(lg)
    streak = mx = 0
    for x in rt:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    print(f"  筆數 {len(rt)}（{len(rt) / years:.2f} 筆/年）　勝率 {len(win) / len(rt):.0%}"
          f"　最大連續虧損 {mx} 筆")
    print(f"  平均獲利 {st.fmean(win):+.1%}　平均虧損 {st.fmean(los):+.1%}"
          f"　賠率 {abs(st.fmean(win) / st.fmean(los)):.1f}"
          f"　Profit Factor {sum(win) / abs(sum(los)):.2f}")
    print(f"  期望值 {st.fmean(rt):+.1%}　中位數 {st.median(rt):+.1%}"
          f"　最佳 {max(rt):+.1%}　最差 {min(rt):+.1%}")
    print(f"  報酬集中度：最大一筆佔 {lg[0] / tl:.0%}　前 3 筆 {sum(lg[:3]) / tl:.0%}"
          f"　前 5 筆 {sum(lg[:5]) / tl:.0%}")
    gaps = [(D[i].trade.entry_date - D[i - 1].trade.entry_date).days / 365.25
            for i in range(1, len(D))]
    print(f"  進場間隔：中位數 {st.median(gaps):.1f} 年　最長 {max(gaps):.1f} 年")

    print("\n【5】停損執行落差（這個策略特有的風險）\n")
    i_of = {b.d: i for i, b in enumerate(bars)}
    rows = []
    for d in D:
        t = d.trade
        if t.ret > 0:
            continue
        a = i_of[t.entry_date]
        b = i_of[t.exit_date] if t.exit_date else len(bars) - 1
        real = (bars[b].close - bars[a].close) / bars[a].close
        rows.append((t.entry_date, t.stop_distance, real,
                     abs(real) / t.stop_distance, t.leverage, t.ret))
    rows.sort(key=lambda x: -x[3])
    over = sum(1 for x in rows if x[5] < -cfg.sizing.risk_per_trade)
    print(f"  虧損 {len(rows)} 筆，實際跌幅 ÷ 假設停損："
          f"中位數 {st.median([x[3] for x in rows]):.1f} 倍　最大 {rows[0][3]:.1f} 倍")
    for x in rows[:3]:
        print(f"    {x[0]}　假設 -{x[1]:.2%} → 實際 {x[2]:+.2%}（{x[3]:.1f}x）"
              f"　槓桿 {x[4]:.1f}x → 權益 {x[5]:.1%}")
    print(f"  單筆虧損超出 {cfg.sizing.risk_per_trade:.0%} 風險預算："
          f"{over}/{len(rows)} 筆")

    print("\n【6】統計可信度（樣本量太小時唯一誠實的一節）\n")
    n = len(rt)
    t = st.fmean(rt) / (st.stdev(rt) / math.sqrt(n))
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    print(f"  單筆報酬 t = {t:.2f}（n={n}）　雙尾 p ≈ {p:.3f}")
    random.seed(0)
    boots = []
    for _ in range(5000):
        eq = 1.0
        for _ in range(n):
            eq *= 1 + rt[random.randrange(n)]
        boots.append(eq ** (1 / years) - 1)
    boots.sort()
    print(f"  Bootstrap CAGR 95% 區間 [{boots[125]:.1%}, {boots[4875]:.1%}]"
          f"　中位數 {boots[len(boots) // 2]:.1%}")
    print(f"  重抽後 CAGR < 0 的機率 {sum(1 for x in boots if x < 0) / len(boots):.1%}")

    print("\n【7】年度\n")
    byy = defaultdict(list)
    for i in range(1, len(dates)):
        byy[dates[i].year].append(nav[i] / nav[i - 1] - 1)
    rows2 = []
    for y in sorted(byy):
        v = 1.0
        for x in byy[y]:
            v *= 1 + x
        rows2.append((y, v - 1))
    pos = sum(1 for _, v in rows2 if v > FLAT)
    flat = sum(1 for _, v in rows2 if abs(v) <= FLAT)
    print(f"  {len(rows2)} 個年度：正報酬 {pos}　空白（整年沒交易）{flat}"
          f"　負報酬 {len(rows2) - pos - flat}")
    print("  " + "　".join(f"{y}:{v:+.0%}" for y, v in rows2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
