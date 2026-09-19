#!/usr/bin/env python3
"""最長水下期間（time underwater）能不能縮短？—— 四類槓桿的聚焦檢驗。

    python3 scripts/underwater_study.py

最終套件（混合注碼＋核心 0.5x ±2%＋step-down）的最長水下約 7.5～8 年
（2012-03 → 2020-06）。§22 的診斷：2012-01-18 那筆 3x 從 MFE +39.7% 回吐到 +12.3%
造出一個高點，之後 8 年只有兩筆小虧的訊號，核心 0.5x 的累積不足以填回。

本腳本對四類可能的槓桿各測一個小網格，每個候選同時報：
最長水下、其間回撤、CAGR、MDD、Sharpe、Calmar、前後段、逐筆錨定 walk-forward。
判定標準與 §18–§23 相同：前後段方向一致是必要條件，walk-forward 未通過就標記。

  A. 訊號頻率：repair_fraction ∈ {0.50, 0.55, 0.60}、max_repair_bars ∈ {30, 40}
     （動訊號層 → 21 筆全變，風險最高；grid search 當初以報酬÷回檔選了 0.60/30）
  B. 核心倍率：0.5 / 0.75 / 1.0（§22 已知：縮短水下但 MDD 線性惡化）
  C. 減碼門檻：+150%→1x（現行）、+100%→1.5x、+50%→1.5x
  D. 移動停利：6% / 8%（現行）/ 10%

⚠️ 所有候選都是在同一批資料上設計與檢驗的（第五代後見之明）。
"""

from __future__ import annotations

import math
import statistics as st
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw.config import PRESETS                              # noqa: E402
from tw_backdraw.engine import Engine, moving_average               # noqa: E402
from tw_backdraw.futures import (FuturesCost, core_overlay,         # noqa: E402
                                 entries_from_trades, hybrid_entries)
from walk_forward_sizing import OBJECTIVES, seg_metrics              # noqa: E402

COST = FuturesCost()
SPLIT = date(2013, 1, 1)


class Study:
    def __init__(self):
        self.bars, self.fs, _ = load_tx()
        self.dates = [b.d for b in self.bars]
        self.ic = [b.close for b in self.bars]
        self.ma = moving_average(self.bars, 200)
        self.n = len(self.dates)
        self.cut = next(i for i, d in enumerate(self.dates) if d >= SPLIT)
        self.base_cfg = PRESETS["tuned"]

    def run(self, repair=0.60, bars_=30, trail=0.08, core=0.5, band=0.02,
            step=(1.5, 1.0)):
        cfg = replace(self.base_cfg,
                      setup=replace(self.base_cfg.setup, repair_fraction=repair,
                                    max_repair_bars=bars_),
                      exit=replace(self.base_cfg.exit, trail_drawdown=trail))
        res = Engine(cfg).run(self.bars, self.fs)
        ent = hybrid_entries(entries_from_trades(res.trades, self.bars, cfg, 5.0, True),
                             self.bars, 200, 3.0, 0.5)
        sg, sl = (step if step else (None, 1.0))
        nav, det, _ = core_overlay(self.dates, self.fs, ent, COST, self.ic, core,
                                   self.ma, step_gain=sg, step_leverage=sl, band=band)
        return nav, det, ent

    # ---- 指標 ----
    def underwater(self, nav):
        """(最長水下天數, 起日索引, 收復日索引, 其間最深回撤)。"""
        pk, pkk, best = nav[0], 0, (0, 0, 0, 0.0)
        cur_dd = 0.0
        for k, x in enumerate(nav):
            if x >= pk:
                if k - pkk > best[0]:
                    best = (k - pkk, pkk, k, cur_dd)
                pk, pkk, cur_dd = x, k, 0.0
            else:
                cur_dd = min(cur_dd, x / pk - 1)
        if self.n - 1 - pkk > best[0]:
            best = (self.n - 1 - pkk, pkk, self.n - 1, cur_dd)
        return best

    def metrics(self, nav, det, a=0, b=None):
        b = self.n - 1 if b is None else b
        s = nav[a:b + 1]
        yrs = (self.dates[b] - self.dates[a]).days / 365.25
        cagr = (s[-1] / s[0]) ** (1 / yrs) - 1
        pk = dd = 0.0
        for x in s:
            pk = max(pk, x)
            dd = min(dd, x / pk - 1)
        r = [s[i] / s[i - 1] - 1 for i in range(1, len(s))]
        sharpe = st.fmean(r) / st.pstdev(r) * math.sqrt(252)
        rets = [t.ret for t in det if a <= self.dates.index(t.entry_date) <= b] or [0.0]
        return dict(cagr=cagr, mdd=dd, sharpe=sharpe, calmar=cagr / abs(dd),
                    n=len(rets), worst=min(rets), over=sum(x < -0.08 for x in rets))

    def row(self, name, nav, det, base=None):
        m = self.metrics(nav, det)
        f = self.metrics(nav, det, 0, self.cut - 1)
        g = self.metrics(nav, det, self.cut, self.n - 1)
        L, a, b, dd = self.underwater(nav)
        verdict = ""
        if base is not None:
            bf, bg = base
            up_f = (f["sharpe"] - bf["sharpe"] > 0.005) or (f["calmar"] - bf["calmar"] > 0.005)
            up_g = (g["sharpe"] - bg["sharpe"] > 0.005) or (g["calmar"] - bg["calmar"] > 0.005)
            dn_f = (f["sharpe"] - bf["sharpe"] < -0.005) or (f["calmar"] - bf["calmar"] < -0.005)
            dn_g = (g["sharpe"] - bg["sharpe"] < -0.005) or (g["calmar"] - bg["calmar"] < -0.005)
            if (up_f and dn_g) or (dn_f and up_g):
                verdict = "✗ 前後段不一致"
            elif dn_f or dn_g:
                verdict = "✗ 兩段皆劣或一段劣"
            elif up_f or up_g:
                verdict = "✓ 前後段一致改善"
            else:
                verdict = "＝ 無差異"
        print(f"{name:<24}{m['n']:>3}{L / 252:>6.1f}年 {self.dates[a]}→{self.dates[b]}"
              f"{dd:>8.1%}{m['cagr']:>7.1%}{m['mdd']:>8.1%}{m['sharpe']:>6.2f}"
              f"{m['calmar']:>6.2f}{m['worst']:>7.1%}{m['over']:>3} | "
              f"{f['sharpe']:.2f}/{f['calmar']:.2f}  {g['sharpe']:.2f}/{g['calmar']:.2f}  {verdict}")
        return f, g


HEAD = (f"{'方案':<24}{'筆':>3}{'最長水下':>8} {'起→收復':<23}{'其間DD':>8}{'CAGR':>7}"
        f"{'MDD':>8}{'Sharpe':>6}{'Calmar':>6}{'最差':>7}{'>8%':>3} | 前段 Sh/Ca   後段 Sh/Ca")


def anchored_oos(S: Study, navs: dict, burn: int = 10):
    """逐筆錨定：以基準訊號的進場日切段，每段用「之前」的表現選候選。"""
    _, _, base_ent = S.run()
    ents = sorted(base_ent, key=lambda e: e.entry_i)
    out = {}
    for obj in ("calmar", "sharpe"):
        keyf = OBJECTIVES[obj]
        daily = [1.0]
        acc = 1.0
        picks = []
        for k in range(burn, len(ents)):
            e_i = ents[k].entry_i
            nxt = ents[k + 1].entry_i - 1 if k + 1 < len(ents) else S.n - 1
            best = max(navs, key=lambda nm: keyf(seg_metrics(navs[nm], 0, e_i - 1)))
            picks.append(best)
            basev = navs[best][e_i - 1]
            for i in range(e_i, nxt + 1):
                daily.append(acc * navs[best][i] / basev)
            acc *= navs[best][nxt] / basev
        oos_a = ents[burn].entry_i - 1
        comp = seg_metrics(daily, 0, len(daily) - 1)
        out[obj] = (comp, picks, oos_a)
    return out


def main() -> int:
    login()
    S = Study()
    print(f"期間 {S.dates[0]} ~ {S.dates[-1]}（{S.n} 個交易日）\n")

    nav0, det0, _ = S.run()
    print("【0】基準：最終套件（混合注碼＋核心 0.5x ±2%＋step-down +150%→1x）\n")
    print(HEAD)
    base = S.row("最終套件", nav0, det0)
    navs = {"最終套件": nav0}

    print("\n【A】訊號頻率（動訊號層：全部 21 筆都會變）\n")
    print(HEAD)
    for bars_ in (30, 40):
        for rep in (0.50, 0.55, 0.60):
            if (rep, bars_) == (0.60, 30):
                continue
            nav, det, _ = S.run(repair=rep, bars_=bars_)
            nm = f"補回≥{rep:.0%}／{bars_}日"
            S.row(nm, nav, det, base)
            navs[nm] = nav

    print("\n【B】核心倍率\n")
    print(HEAD)
    for core in (0.75, 1.0):
        nav, det, _ = S.run(core=core)
        nm = f"核心 {core:g}x"
        S.row(nm, nav, det, base)
        navs[nm] = nav

    print("\n【C】減碼門檻\n")
    print(HEAD)
    for step, nm in (((1.0, 1.5), "減碼 +100%→1.5x"), ((0.5, 1.5), "減碼 +50%→1.5x"),
                     ((0.5, 1.0), "減碼 +50%→1x")):
        nav, det, _ = S.run(step=step)
        S.row(nm, nav, det, base)
        navs[nm] = nav

    print("\n【D】移動停利\n")
    print(HEAD)
    for trail in (0.06, 0.10):
        nav, det, _ = S.run(trail=trail)
        nm = f"移動停利 {trail:.0%}"
        S.row(nm, nav, det, base)
        navs[nm] = nav

    print("\n【E】逐筆錨定 walk-forward（燒入 10 筆；候選＝以上全部＋基準）\n")
    res = anchored_oos(S, navs)
    for obj, (comp, picks, oos_a) in res.items():
        uniq = []
        for p in picks:
            if not uniq or uniq[-1] != p:
                uniq.append(p)
        print(f"  目標 {obj}　OOS {S.dates[oos_a]} 起　路徑：{' → '.join(uniq)}")
        print(f"    複合（真樣本外）  總報酬 {comp['total']:+8.1%}  MDD {comp['mdd']:6.1%}  Sharpe {comp['sharpe']:.2f}")
        for nm in ("最終套件",) + tuple(x for x in uniq if x != "最終套件"):
            m = seg_metrics(navs[nm], oos_a, S.n - 1)
            print(f"    {nm:<20}  總報酬 {m['total']:+8.1%}  MDD {m['mdd']:6.1%}  Sharpe {m['sharpe']:.2f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
