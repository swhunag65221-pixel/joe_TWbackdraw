#!/usr/bin/env python3
"""策略助理 A：部位內回吐（in-trade give-back）研究。

在「混合半預算＋核心」之上測三類機制：
  (a) 移動停利幅度 trail_drawdown ∈ {0.04,0.05,0.06,0.08,0.10}（外加 0.99 = 關閉）
  (b) 獲利後降槓桿 step-down：權益 +X% → 降到 Y 倍（固定口數 → 部分平倉）
  (c) 權益口徑移動停利：部位權益自進場後高點回落 Z% → 出場

不修改 repo 既有檔案；所有變體實作在本檔。
"""
from __future__ import annotations
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent

import math
import pickle
import statistics as st
import sys
from dataclasses import replace
from datetime import date

sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'scripts'))

from tw_backdraw.config import PRESETS                                  # noqa: E402
from tw_backdraw.engine import Engine, moving_average                   # noqa: E402
from tw_backdraw.futures import (FuturesCost, FuturesEntry,             # noqa: E402
                                 entries_from_trades, fixed_leverage,
                                 trend_filter)

CACHE = (str(_LAB / 'tx.pkl'))
COST = FuturesCost()
MA = 200
CORE = 0.5
BELOW_LEV = 3.0
PERIODS = (("全期 1999–2026", date(1999, 1, 1), date(2027, 1, 1)),
           ("前段 1999–2012", date(1999, 1, 1), date(2013, 1, 1)),
           ("後段 2013–2026", date(2013, 1, 1), date(2027, 1, 1)))


# ---------------------------------------------------------------------------
# 統一模擬器：固定口數 + 可選 core / step-down / 權益移動停利
# ---------------------------------------------------------------------------
class Pos:
    """單筆部位的狀態（可因 step-down 重新錨定）。"""
    __slots__ = ("e", "eq_at_entry", "anchor_eq", "f0", "i0", "lev",
                 "peak", "trough", "stepped", "mfe_v", "mae_v")


def simulate(dates, cont, ic, entries, cost, core=0.0, ma=None,
             step=None, eq_trail=None, trigger="fut"):
    """回傳 (nav, detail, exposure)。

    detail 每筆為 dict：entry_date/exit_date/lev/ret/mfe/mae/maxdd/reason。

    時序（與 repo 一致）：以**收盤**判定、當日**期貨收盤**成交。

    `trigger` 決定 step-down / 權益停利用哪條序列判定部位權益：
      "fut" —— 期貨連續序列（預設）。權益是真的，止損位是一個價位，
               實務上可盤中掛單，收盤成交只會更保守。
      "idx" —— 加權「報酬」指數的權益代理。⚠️ 該指數含息、期貨不含，
               3x 部位持有 10 個月會被灌水約 +9pp，會讓觸發時點失真；
               只保留為敏感度對照，不是主口徑。
    """
    n = len(dates)
    on = ([m is not None and c > m for c, m in zip(ic, ma)] if core
          else [False] * n)
    by_entry = {e.entry_i: e for e in entries}
    nav = [1.0] * n
    detail = []
    equity = 1.0
    p: Pos | None = None
    holding_core = False
    exposed = 0

    def close_trade(i, v, reason):
        detail.append(dict(entry_date=dates[p.e.entry_i], exit_date=dates[i],
                           lev=p.e.leverage, final_lev=p.lev,
                           ret=v / p.eq_at_entry - 1.0,
                           mfe=p.mfe_v / p.eq_at_entry - 1.0,
                           mae=p.mae_v / p.eq_at_entry - 1.0,
                           maxdd=p.trough, reason=reason,
                           bars=i - p.e.entry_i))

    for i in range(n):
        if p is not None:
            v = p.anchor_eq * (1.0 + p.lev * (cont[i] / p.f0 - 1.0))
            v_idx = (v if trigger == "fut" else
                     p.anchor_eq * (1.0 + p.lev * (ic[i] / p.i0 - 1.0)))
            exposed += 1
            p.mfe_v = max(p.mfe_v, v)
            p.mae_v = min(p.mae_v, v)
            p.peak = max(p.peak, v_idx)
            p.trough = min(p.trough, v_idx / p.peak - 1.0)

            hit_trail = (eq_trail is not None
                         and v_idx <= p.peak * (1.0 - eq_trail))
            scheduled = p.e.exit_i is not None and i == p.e.exit_i
            closing = scheduled or hit_trail
            if closing:
                v *= 1.0 - p.lev * cost.one_way_rate(ic[i])
                nav[i] = v
                close_trade(i, v, "權益停利" if hit_trail and not scheduled
                            else "引擎出場")
                equity, p, holding_core = v, None, False
                continue

            # step-down：權益相對進場達 +X% → 降到 Y 倍（只降不升）
            if (step is not None and not p.stepped
                    and v_idx >= p.eq_at_entry * (1.0 + step[0])
                    and p.lev > step[1]):
                rate = cost.one_way_rate(ic[i])
                cur_notional = p.lev * p.anchor_eq * cont[i] / p.f0
                delta = max(cur_notional - step[1] * v, 0.0)
                v -= delta * rate
                p.anchor_eq, p.f0, p.i0, p.lev = v, cont[i], ic[i], step[1]
                p.stepped = True
                p.mfe_v = max(p.mfe_v, v)
            nav[i] = v
            continue

        # ---- 空手 ----
        if i > 0 and holding_core:
            equity *= 1.0 + core * (cont[i] / cont[i - 1] - 1.0)
            exposed += 1

        e = by_entry.get(i)
        if e is not None:
            if holding_core:
                equity *= 1.0 - core * cost.one_way_rate(ic[i])
                holding_core = False
            p = Pos()
            p.e, p.eq_at_entry = e, equity
            p.anchor_eq = equity * (1.0 - e.leverage * cost.one_way_rate(ic[i]))
            p.f0, p.i0, p.lev = cont[i], ic[i], e.leverage
            p.peak = p.mfe_v = p.mae_v = p.anchor_eq
            p.trough, p.stepped = 0.0, False
            nav[i] = p.anchor_eq
            exposed += 1
            continue

        if core and on[i] != holding_core:
            equity *= 1.0 - core * cost.one_way_rate(ic[i])
            holding_core = on[i]
        nav[i] = equity

    if p is not None:
        close_trade(n - 1, nav[n - 1], "未平倉")
    return nav, detail, exposed / n


# ---------------------------------------------------------------------------
# Book：可接受任意 cfg（trail_drawdown 變體）
# ---------------------------------------------------------------------------
class Lab:
    def __init__(self, bars, fs, trail: float):
        base_cfg = PRESETS["tuned"]
        self.cfg = replace(base_cfg, exit=replace(base_cfg.exit,
                                                  trail_drawdown=trail))
        self.trail = trail
        self.bars, self.fs = bars, fs
        self.dates = [b.d for b in bars]
        self.ic = [b.close for b in bars]
        self.ma = moving_average(bars, MA)
        res = Engine(self.cfg).run(bars, fs)
        self.base = entries_from_trades(res.trades, bars, self.cfg, 5.0,
                                        same_day=True)
        self.filtered = trend_filter(self.base, bars, MA, below=True)

    def split_by_ma(self):
        below, above = [], []
        for e in self.base:
            m = self.ma[e.entry_i]
            (below if m is not None and self.ic[e.entry_i] < m
             else above).append(e)
        return below, above

    def hybrid(self, above_cap=5.0, above_scale=1.0):
        below, above = self.split_by_ma()
        out = list(fixed_leverage(below, BELOW_LEV))
        out += [FuturesEntry(e.entry_i, e.exit_i,
                             min(e.leverage * above_scale, above_cap),
                             e.entry_index, e.stop_distance, e.trade)
                for e in above]
        return sorted(out, key=lambda e: e.entry_i)

    def nav(self, entries, core=0.0, step=None, eq_trail=None, trigger="fut"):
        return simulate(self.dates, self.fs, self.ic, entries, COST,
                        core=core, ma=self.ma, step=step, eq_trail=eq_trail,
                        trigger=trigger)

    def measure(self, entries, lo=None, hi=None, core=0.0, step=None,
                eq_trail=None, trigger="fut", nav_det=None):
        nav, det, expo = nav_det or self.nav(entries, core, step, eq_trail,
                                             trigger)
        return metrics(self.dates, nav, det, expo, lo, hi)


def metrics(dates, nav, det, expo, lo=None, hi=None):
    lo = lo or dates[0]
    hi = hi or dates[-1].replace(year=dates[-1].year + 1)
    idx = [i for i, d in enumerate(dates) if lo <= d < hi]
    a, b = idx[0], idx[-1]
    base = nav[a]
    seg = [nav[i] / base for i in range(a, b + 1)] if base > 0 else [0.0]
    years = (dates[b] - dates[a]).days / 365.25
    r = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
    peak = dd = 0.0
    for x in seg:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1) if peak > 0 else dd
    cagr = seg[-1] ** (1 / years) - 1 if seg[-1] > 0 else -1.0
    rt = [t["ret"] for t in det if lo <= t["entry_date"] < hi] or [0.0]
    mf = [t["mfe"] for t in det if lo <= t["entry_date"] < hi] or [0.0]
    gb = [t["mfe"] - t["ret"] for t in det if lo <= t["entry_date"] < hi] or [0.0]
    return dict(n=len(rt), ex=expo, win=sum(1 for x in rt if x > 0) / len(rt),
                cagr=cagr, mdd=dd, calmar=cagr / abs(dd) if dd else 0.0,
                sharpe=(st.fmean(r) / st.pstdev(r) * math.sqrt(252)
                        if len(r) > 1 and st.pstdev(r) else 0.0),
                worst=min(rt), over=sum(1 for x in rt if x < -0.08),
                ruin=min(nav) <= 0.0, mfe=st.fmean(mf), giveback=st.fmean(gb),
                det=det, nav=nav)


HEAD = (f'{"方案":<28}{"筆":>4}{"在場":>6}{"勝率":>7}{"CAGR":>8}{"MDD":>9}'
        f'{"Sharpe":>8}{"Calmar":>8}{"最差單筆":>10}{">8%":>5}'
        f'{"均MFE":>9}{"均回吐":>9}')


def row(nm, m):
    flag = "  ⚠️爆倉" if m["ruin"] else ""
    return (f'{nm:<28}{m["n"]:>4}{m["ex"]:>6.0%}{m["win"]:>7.0%}{m["cagr"]:>8.1%}'
            f'{m["mdd"]:>9.1%}{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}'
            f'{m["worst"]:>10.1%}{m["over"]:>5}{m["mfe"]:>9.1%}'
            f'{m["giveback"]:>9.1%}{flag}')


def dd_episodes(dates, nav, topn=3):
    """前 N 大回撤區段：(峰日, 谷日, 復原日 or None, 深度)。"""
    eps = []
    peak, peak_i = nav[0], 0
    trough, trough_i, inep = nav[0], 0, False
    for i, x in enumerate(nav):
        if x >= peak:
            if inep:
                eps.append((peak_i, trough_i, i, trough / peak - 1.0))
                inep = False
            peak, peak_i = x, i
        else:
            if not inep:
                inep, trough, trough_i = True, x, i
            elif x < trough:
                trough, trough_i = x, i
    if inep:
        eps.append((peak_i, trough_i, None, trough / peak - 1.0))
    eps.sort(key=lambda e: e[3])
    return [(dates[a], dates[b], dates[c] if c is not None else None, d)
            for a, b, c, d in eps[:topn]]


def print_dd(dates, nav, label, topn=3):
    print(f"  {label}")
    for k, (pd_, td, rd, dep) in enumerate(dd_episodes(dates, nav, topn), 1):
        rec = str(rd) if rd else "未復原"
        print(f"    #{k} 峰 {pd_} → 谷 {td}　深度 {dep:6.1%}　復原 {rec}")


def load():
    with open(CACHE, "rb") as f:
        bars, fs, _ = pickle.load(f)
    return bars, fs
