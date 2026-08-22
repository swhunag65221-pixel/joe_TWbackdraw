#!/usr/bin/env python3
"""五種增加進出場機會與在場時間的方案，看哪一種能維持勝率。

    python3 scripts/coverage_study.py

現行台指期策略 27.6 年只有 21 筆、在場比例 28%、勝率 52%。
問題不是訊號不準（在場日 Sharpe 1.43），是訊號太少。以下五個方向各自測試：

  A. 降低回檔門檻          —— 讓更淺的回檔也算數
  B. 放寬修復定義          —— 補回比例或天數放寬
  C. 多套門檻平行分身       —— N 組參數各佔 1/N 資金
  D. 空手期的趨勢核心部位    —— 沒訊號時只要指數在均線上就持有低槓桿
  E. 停損只減碼一半         —— 保留船票，收復主防線可補回

⚠️ 時序陷阱：方案 D 的均線判斷若用「當日收盤決定、當日報酬入帳」，
會產生前視偏誤，把純均線濾網的 CAGR 從 8.0% 灌水到 26.0%。
本腳本一律採用與策略相同的時序：**收盤判定 → 當日期貨收盤成交 → 報酬自隔日起算**。
"""

from __future__ import annotations

import math
import statistics as st
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402
from tw_backdraw.engine import Engine, moving_average               # noqa: E402
from tw_backdraw.futures import (FuturesCost, entries_from_trades,  # noqa: E402
                                 vehicle_series)

COST = FuturesCost()


class Book:
    """把資料與共用計算包起來，避免每個方案重複開檔。"""

    def __init__(self):
        self.bars, self.fs, _ = load_tx()
        self.dates = [b.d for b in self.bars]
        self.ic = [b.close for b in self.bars]
        self.years = (self.dates[-1] - self.dates[0]).days / 365.25

    def run(self, cfg, max_leverage: float = 5.0):
        res = Engine(cfg).run(self.bars, self.fs)
        ent = entries_from_trades(res.trades, self.bars, cfg, max_leverage,
                                  same_day=True)
        nav, det = vehicle_series(self.dates, self.fs, ent, COST, self.ic)
        return ent, nav, det

    def held_days(self, ent) -> set:
        s: set = set()
        for e in ent:
            b = e.exit_i if e.exit_i is not None else len(self.dates) - 1
            s.update(range(e.entry_i, b + 1))
        return s

    def stats(self, nav, n_trades, expo, win) -> dict:
        r = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav)) if nav[i - 1]]
        peak = dd = 0.0
        for x in nav:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1.0)
        c = nav[-1] ** (1 / self.years) - 1
        return dict(n=n_trades, ex=expo, win=win, cagr=c, mdd=dd,
                    sharpe=st.fmean(r) / st.pstdev(r) * math.sqrt(252),
                    calmar=c / abs(dd))

    def of(self, cfg, max_leverage: float = 5.0) -> dict:
        ent, nav, det = self.run(cfg, max_leverage)
        rt = [t.ret for t in det]
        return self.stats(nav, len(det), len(self.held_days(ent)) / len(self.dates),
                          sum(1 for x in rt if x > 0) / len(rt) if rt else 0.0)

    # ---- 方案 C：平行分身 ----
    def sleeves(self, cfgs) -> dict:
        navs, days, rts, n = [], set(), [], 0
        for c in cfgs:
            ent, nav, det = self.run(c)
            navs.append(nav)
            days |= self.held_days(ent)
            rts += [t.ret for t in det]
            n += len(det)
        rs = [[nav[i] / nav[i - 1] - 1 if nav[i - 1] else 0.0
               for i in range(1, len(nav))] for nav in navs]
        w = 1 / len(navs)
        blend = [1.0]
        for j in range(len(rs[0])):
            blend.append(blend[-1] * (1 + sum(w * s[j] for s in rs)))
        return self.stats(blend, n, len(days) / len(self.dates),
                          sum(1 for x in rts if x > 0) / len(rts))

    # ---- 方案 D：空手期核心部位 ----
    def with_core(self, ent, det, core_lev: float, ma_period: int) -> dict:
        """空手且指數在均線之上時持有 `core_lev` 倍的核心部位。

        時序與策略一致：`on[i]` 是**第 i 天收盤後**的判斷，
        部位在當天期貨收盤建立，因此第 i 天的報酬由 `on[i-1]` 決定。
        """
        ma = moving_average(self.bars, ma_period)
        on = [ma[i] is not None and self.ic[i] > ma[i] for i in range(len(self.dates))]
        by = {e.entry_i: e for e in ent}
        nav = [1.0] * len(self.dates)
        eq, active, eq_cost, f0, held, days = 1.0, None, 0.0, 0.0, False, 0

        for i in range(len(self.dates)):
            if active is not None:
                v = eq_cost * (1 + active.leverage * (self.fs[i] / f0 - 1))
                days += 1
                closing = active.exit_i is not None and i == active.exit_i
                if closing:
                    v *= 1 - active.leverage * COST.one_way_rate(self.ic[i])
                nav[i] = v
                if closing:
                    eq, active, held = v, None, False
                continue

            if i > 0 and held:                       # 昨收就持有核心 → 賺今天
                eq *= 1 + core_lev * (self.fs[i] / self.fs[i - 1] - 1)
                days += 1
            e = by.get(i)
            if e is not None:                        # 今收轉進策略部位
                if held:
                    eq *= 1 - core_lev * COST.one_way_rate(self.ic[i])
                    held = False
                active = e
                eq_cost = eq * (1 - e.leverage * COST.one_way_rate(self.ic[i]))
                f0 = self.fs[i]
                nav[i] = eq_cost
                continue
            if on[i] != held:
                eq *= 1 - core_lev * COST.one_way_rate(self.ic[i])
                held = on[i]
            nav[i] = eq

        rt = [t.ret for t in det]
        return self.stats(nav, len(det), days / len(self.dates),
                          sum(1 for x in rt if x > 0) / len(rt))

    # ---- 對照組：純均線，沒有回檔訊號 ----
    def ma_only(self, lev: float, ma_period: int) -> dict:
        ma = moving_average(self.bars, ma_period)
        on = [ma[i] is not None and self.ic[i] > ma[i] for i in range(len(self.dates))]
        eq, nav, held, days = 1.0, [1.0] * len(self.dates), on[0], 0
        for i in range(len(self.dates)):
            if i > 0 and held:
                eq *= 1 + lev * (self.fs[i] / self.fs[i - 1] - 1)
                days += 1
            if i > 0 and on[i] != held:
                eq *= 1 - lev * COST.one_way_rate(self.ic[i])
                held = on[i]
            nav[i] = eq
        return self.stats(nav, 0, days / len(self.dates), None)


HEAD = (f"{'方案':<38}{'筆數':>5}{'在場':>7}{'勝率':>7}{'CAGR':>8}"
        f"{'MDD':>9}{'Sharpe':>8}{'Calmar':>8}")


def show(nm: str, s: dict) -> None:
    w = f"{s['win']:.0%}" if s["win"] is not None else "—"
    n = f"{s['n']}" if s["n"] else "—"
    print(f"{nm:<38}{n:>5}{s['ex']:>7.0%}{w:>7}{s['cagr']:>8.1%}"
          f"{s['mdd']:>9.1%}{s['sharpe']:>8.2f}{s['calmar']:>8.2f}")


def main() -> int:
    login()
    bk = Book()
    B = PRESETS["tuned"]
    print(f"期間 {bk.dates[0]} ~ {bk.dates[-1]}（{bk.years:.1f} 年）\n")
    print(HEAD)
    show("現行 tuned（基準）", bk.of(B))

    print("\n— A. 降低回檔門檻 —")
    for md in (0.08, 0.07, 0.06, 0.05):
        show(f"A. min_drawdown {md:.0%}",
             bk.of(replace(B, setup=replace(B.setup, min_drawdown=md))))

    print("\n— B. 放寬修復定義 —")
    for rf, mb in ((0.55, 30), (0.50, 30), (0.60, 45), (0.50, 45)):
        show(f"B. 補回 {rf:.0%} / {mb} 日內",
             bk.of(replace(B, setup=replace(B.setup, repair_fraction=rf,
                                            max_repair_bars=mb))))

    print("\n— C. 多套門檻平行分身（各佔 1/N 資金）—")
    for lab, mds in (("7/10%", (0.07, 0.10)), ("7/10/13%", (0.07, 0.10, 0.13)),
                     ("5/7/10/13/16%", (0.05, 0.07, 0.10, 0.13, 0.16))):
        show(f"C. 分身 {lab}",
             bk.sleeves([replace(B, setup=replace(B.setup, min_drawdown=m))
                         for m in mds]))

    print("\n— D. 空手期的趨勢核心部位 —")
    ent0, _, det0 = bk.run(B)
    for lev in (0.5, 1.0, 1.5):
        for mp in (120, 200):
            show(f"D. 空手核心 {lev:g}x（指數 > MA{mp}）",
                 bk.with_core(ent0, det0, lev, mp))
    print("  對照（沒有回檔訊號，只有均線）：")
    for lev in (1.0, 2.74):
        for mp in (120, 200):
            show(f"    純均線 {lev:g}x（MA{mp}）", bk.ma_only(lev, mp))

    print("\n— E. 停損只減碼一部分，保留船票 —")
    for f in (0.5, 0.6, 0.7):
        show(f"E. 警戒減碼 {f:.0%}",
             bk.of(replace(B, exit=replace(B.exit, warn_derisk_fraction=f))))

    print("\n— 組合 —")
    combo = replace(B, setup=replace(B.setup, min_drawdown=0.07),
                    exit=replace(B.exit, warn_derisk_fraction=0.5))
    show("A7 + E50", bk.of(combo))
    e2, _, d2 = bk.run(combo)
    for lev in (0.5, 1.0):
        show(f"A7 + E50 + 核心 {lev:g}x（MA200）",
             bk.with_core(e2, d2, lev, 200))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
