#!/usr/bin/env python3
"""防守版：只做「進場日收盤低於 MA200」的訊號，並比較固定槓桿 3~7 倍。

    python3 scripts/defensive_study.py

兩個獨立的改動，分開驗證再合併看：

1. **逆勢濾網**：只在指數低於 MA200 時進場。與直覺相反 ——
   這是逆勢策略，要求站上均線會排除最深的回檔（見 docs/strategy.md §18）。
2. **固定槓桿**：不再用「風險預算 ÷ 停損距離」，所有部位同一個倍數。
   固定槓桿下每筆權益報酬 = 槓桿 × 期貨報酬，是線性的，所以高倍數會有**爆倉**風險
   —— 腳本會明確檢查權益是否觸及 0。

⚠️ 這個濾網是看著逐筆交易表反推出來的（後見之明）。
唯一支撐它的是前後段檢驗的方向一致，本腳本第 3 節就是在做那件事。
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
from tw_backdraw.engine import moving_average                      # noqa: E402
from tw_backdraw.futures import (FuturesCost, core_overlay,        # noqa: E402
                                 entries_from_trades, fixed_leverage,
                                 trade_details, trend_filter, vehicle_series)

COST = FuturesCost()
MA = 200
CORE = 0.5          # 空手期的核心部位槓桿（docs/coverage.md 驗收後保留的改良）
LEVS = (3.0, 4.0, 5.0, 6.0, 7.0)
PERIODS = (("全期 1999–2026", date(1999, 1, 1), date(2027, 1, 1)),
           ("前段 1999–2012", date(1999, 1, 1), date(2013, 1, 1)),
           ("後段 2013–2026", date(2013, 1, 1), date(2027, 1, 1)))


class Book:
    def __init__(self):
        self.cfg = PRESETS["tuned"]
        self.bars, self.fs, _ = load_tx()
        self.dates = [b.d for b in self.bars]
        self.ic = [b.close for b in self.bars]
        self.ma = moving_average(self.bars, MA)
        res = Engine(self.cfg).run(self.bars, self.fs)
        self.base = entries_from_trades(res.trades, self.bars, self.cfg, 5.0,
                                        same_day=True)
        self.filtered = trend_filter(self.base, self.bars, MA, below=True)

    def measure(self, entries, lo=None, hi=None, core: float = 0.0) -> dict:
        if core:
            nav, det, expo = core_overlay(self.dates, self.fs, entries, COST,
                                          self.ic, core, self.ma)
        else:
            nav, det = vehicle_series(self.dates, self.fs, entries, COST, self.ic)
            held = set()
            for e in entries:
                b = e.exit_i if e.exit_i is not None else len(self.dates) - 1
                held.update(range(e.entry_i, b + 1))
            expo = len(held) / len(self.dates)
        lo = lo or self.dates[0]
        hi = hi or (self.dates[-1].replace(year=self.dates[-1].year + 1))
        idx = [i for i, d in enumerate(self.dates) if lo <= d < hi]
        a, b = idx[0], idx[-1]
        base = nav[a]
        seg = [nav[i] / base for i in range(a, b + 1)] if base > 0 else [0.0]
        years = (self.dates[b] - self.dates[a]).days / 365.25
        r = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
        peak = dd = 0.0
        for x in seg:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1) if peak > 0 else dd
        cagr = seg[-1] ** (1 / years) - 1 if seg[-1] > 0 else -1.0
        rt = [t.ret for t in det if lo <= t.entry_date < hi] or [0.0]
        return dict(n=len(rt), ex=expo, win=sum(1 for x in rt if x > 0) / len(rt),
                    cagr=cagr, mdd=dd, calmar=cagr / abs(dd) if dd else 0.0,
                    sharpe=(st.fmean(r) / st.pstdev(r) * math.sqrt(252)
                            if len(r) > 1 and st.pstdev(r) else 0.0),
                    worst=min(rt), over=sum(1 for x in rt if x < -0.08),
                    ruin=min(nav) <= 0.0, det=det, nav=nav)


HEAD = (f'{"方案":<26}{"筆":>4}{"在場":>6}{"勝率":>7}{"CAGR":>8}{"MDD":>9}'
        f'{"Sharpe":>8}{"Calmar":>8}{"最差單筆":>10}{">8%":>5}')


def row(nm, m):
    flag = "  ⚠️爆倉" if m["ruin"] else ""
    return (f'{nm:<26}{m["n"]:>4}{m["ex"]:>6.0%}{m["win"]:>7.0%}{m["cagr"]:>8.1%}'
            f'{m["mdd"]:>9.1%}{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}'
            f'{m["worst"]:>10.1%}{m["over"]:>5}{flag}')


def main() -> int:
    login()
    bk = Book()
    print(f"期間 {bk.dates[0]} ~ {bk.dates[-1]}"
          f"　原始訊號 {len(bk.base)} 筆　通過 MA{MA} 逆勢濾網 {len(bk.filtered)} 筆\n")

    print("【1】濾網本身（維持原本的風險式槓桿）\n")
    print(HEAD)
    print(row("無濾網（現行）", bk.measure(bk.base)))
    print(row(f"只做 指數 < MA{MA}", bk.measure(bk.filtered)))

    print(f"\n【2】濾網 ＋ 固定槓桿\n")
    print(HEAD)
    for L in LEVS:
        print(row(f"濾網 ＋ 固定 {L:g}x",
                  bk.measure(fixed_leverage(bk.filtered, L))))
    print("  對照（沒有濾網）：")
    for L in LEVS:
        print("  " + row(f"固定 {L:g}x", bk.measure(fixed_leverage(bk.base, L))))

    print(f"\n【2b】再加上空手期的核心部位 {CORE:g}x（收盤 > MA{MA}）\n")
    print("  逆勢濾網只在收盤低於均線時進場，核心只在高於均線時持有 —— 兩者互斥。")
    print("  核心是每日再平衡的固定槓桿，與策略部位的固定口數不同。\n")
    print(HEAD)
    print(row("現行（無濾網、無核心）", bk.measure(bk.base)))
    print(row(f"濾網 ＋ 風險式 ＋ 核心", bk.measure(bk.filtered, core=CORE)))
    for L in LEVS:
        print(row(f"濾網 ＋ 固定 {L:g}x ＋ 核心",
                  bk.measure(fixed_leverage(bk.filtered, L), core=CORE)))

    print(f"\n【3】前後段檢驗 —— 後見之明的濾網唯一站得住的理由\n")
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        print("  " + row("無濾網", bk.measure(bk.base, lo, hi)))
        print("  " + row(f"只做 < MA{MA}", bk.measure(bk.filtered, lo, hi)))
        for L in (3.0, 5.0, 7.0):
            print("  " + row(f"濾網 ＋ 固定 {L:g}x",
                             bk.measure(fixed_leverage(bk.filtered, L), lo, hi)))
        for L in (3.0, 5.0):
            print("  " + row(f"濾網 ＋ 固定 {L:g}x ＋ 核心",
                             bk.measure(fixed_leverage(bk.filtered, L), lo, hi,
                                        core=CORE)))
        print()

    print("【4】通過濾網的逐筆交易，各槓桿下的權益報酬\n")
    nav, det = vehicle_series(bk.dates, bk.fs, bk.filtered, COST, bk.ic)
    D = trade_details(bk.dates, nav, bk.filtered, det)
    per = {L: {t.entry_date: t.ret for t in
               vehicle_series(bk.dates, bk.fs, fixed_leverage(bk.filtered, L),
                              COST, bk.ic)[1]} for L in LEVS}
    print(f'{"進場":<12}{"出場":<12}{"回檔":>7}{"期貨":>9}{"風險式":>9}'
          + "".join(f"{f'{L:g}x':>9}" for L in LEVS))
    for d in sorted(D, key=lambda x: x.trade.entry_date):
        t = d.trade
        print(f'{t.entry_date!s:<12}{str(t.exit_date or "持有中"):<12}'
              f'{d.setup.drop_pct:>7.1%}{t.futures_return:>9.1%}{t.ret:>9.1%}'
              + "".join(f'{per[L][t.entry_date]:>9.1%}' for L in LEVS))
    fr = [d.trade.futures_return for d in D]
    print(f'\n最差的期貨報酬 {min(fr):.1%} —— 固定槓桿 L 倍時的單筆虧損約 '
          f'{min(fr):.1%} × L：')
    print("  " + "　".join(f"{L:g}x → {min(fr) * L:.0%}" for L in LEVS))
    print(f'  權益歸零需要期貨跌 {1 / max(LEVS):.1%}（{max(LEVS):g}x）'
          f'～{1 / min(LEVS):.1%}（{min(LEVS):g}x）')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
