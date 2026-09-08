#!/usr/bin/env python3
"""策略助理 B：核心部位（core overlay）與虧損序列對 MDD 的貢獻，以及怎麼降低它。

只讀 repo，不改 repo。所有指標沿用 defensive_study 的 Book/measure/HEAD/row。
"""

from __future__ import annotations
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent

import math
import statistics as st
import sys
from datetime import date

sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'scripts'))

from defensive_study import CORE, HEAD, MA, Book, row              # noqa: E402
from hybrid_study import BELOW_LEV, hybrid, split_by_ma            # noqa: E402
from walk_forward_sizing import (OBJECTIVES, anchored,             # noqa: E402
                                 composite_metrics, seg_metrics)
from tx_data import login                                          # noqa: E402
from tw_backdraw.engine import moving_average                      # noqa: E402
from tw_backdraw.futures import (FuturesCost, FuturesEntry,        # noqa: E402
                                 FuturesTrade, core_overlay,
                                 fixed_leverage, vehicle_series)

COST = FuturesCost()
PERIODS = (("全期 1999-2026", date(1999, 1, 1), date(2027, 1, 1)),
           ("前段 1999-2012", date(1999, 1, 1), date(2013, 1, 1)),
           ("後段 2013-2026", date(2013, 1, 1), date(2027, 1, 1)))


# ---------------------------------------------------------------- 通用核心疊加
def flex_core(dates, continuous, entries, cost, index_close,
              on, lev, stop: float | None = None):
    """`core_overlay` 的一般化版本，語意與時序完全相同，但是：

    * `on[i]`  —— 核心開關條件（任意布林序列，不限 close > MA200）
    * `lev[i]` —— 第 i 天收盤決定的核心槓桿（可逐日變動 → 波動率目標）
    * `stop`   —— 核心自「本段持有起」的權益高點回落 `stop` 就關閉，
                  之後要等 `on` 先轉 False 再轉 True 才會重新持有。

    另外回傳逐日的損益歸屬（'pos' / 'core' / 'flat'）與成本對數，
    供回撤分解使用。逐日再平衡的槓桿變動也按 |Δ槓桿| 計一次單邊成本。
    """
    n = len(dates)
    by_entry = {e.entry_i: e for e in entries}
    nav = [1.0] * n
    detail: list[FuturesTrade] = []
    cat = ["flat"] * n                 # 當日報酬由誰產生
    cost_log = [0.0] * n               # 當日成本的對數（負值）
    who = [-1] * n                     # 當日在場的策略部位（entry_i），-1 = 無
    stops: list[int] = []              # 核心停損觸發日
    equity, active = 1.0, None
    eq_at_entry = eq_after_cost = f0 = 0.0
    holding_core, cur_lev, core_peak, blocked, exposed = False, 0.0, 0.0, False, 0

    for i in range(n):
        prev = nav[i - 1] if i > 0 else 1.0
        c = 0.0
        if active is not None:
            v = eq_after_cost * (1.0 + active.leverage * (continuous[i] / f0 - 1.0))
            exposed += 1
            closing = active.exit_i is not None and i == active.exit_i
            if closing:
                k = 1.0 - active.leverage * cost.one_way_rate(index_close[i])
                v *= k
                c += math.log(k)
            nav[i], cat[i], cost_log[i], who[i] = v, "pos", c, active.entry_i
            if closing:
                detail.append(FuturesTrade(
                    entry_date=dates[active.entry_i], exit_date=dates[i],
                    entry_index=active.entry_index, entry_futures=f0,
                    exit_futures=continuous[i], leverage=active.leverage,
                    stop_distance=active.stop_distance,
                    futures_return=continuous[i] / f0 - 1.0,
                    ret=v / eq_at_entry - 1.0))
                equity, active, holding_core = v, None, False
            continue

        day_cat = "flat"
        if i > 0 and holding_core:                  # 昨收就持有核心 → 賺今天
            equity *= 1.0 + cur_lev * (continuous[i] / continuous[i - 1] - 1.0)
            exposed += 1
            day_cat = "core"
            core_peak = max(core_peak, equity)

        e = by_entry.get(i)
        if e is not None:                           # 今收轉進策略部位
            if holding_core:
                k = 1.0 - cur_lev * cost.one_way_rate(index_close[i])
                equity *= k
                c += math.log(k)
                holding_core, cur_lev = False, 0.0
            active, eq_at_entry = e, equity
            k = 1.0 - e.leverage * cost.one_way_rate(index_close[i])
            eq_after_cost = equity * k
            c += math.log(k)
            f0 = continuous[i]
            nav[i], cat[i], cost_log[i] = eq_after_cost, day_cat, c
            exposed += 1
            continue

        stopped = (stop is not None and holding_core
                   and core_peak > 0 and equity / core_peak - 1.0 <= -stop)
        if stopped:
            blocked = True                           # 觸發核心停損 → 封鎖
            stops.append(i)
        if not on[i]:
            blocked = False                          # 條件轉 False → 解除封鎖
        want = bool(on[i]) and not blocked
        tgt = lev[i] if want else 0.0
        if abs(tgt - cur_lev) > 1e-12:               # 進出場與逐日再平衡的成本
            k = 1.0 - abs(tgt - cur_lev) * cost.one_way_rate(index_close[i])
            equity *= k
            c += math.log(k)
            if cur_lev == 0.0:                       # 新的一段核心，重設高點
                core_peak = equity
            cur_lev = tgt
            holding_core = tgt > 0.0
        nav[i], cat[i], cost_log[i] = equity, day_cat, c

    if active is not None:
        detail.append(FuturesTrade(
            entry_date=dates[active.entry_i], exit_date=None,
            entry_index=active.entry_index, entry_futures=f0,
            exit_futures=continuous[-1], leverage=active.leverage,
            stop_distance=active.stop_distance,
            futures_return=continuous[-1] / f0 - 1.0,
            ret=nav[-1] / eq_at_entry - 1.0))
    return nav, detail, exposed / n, cat, cost_log, who, stops


def metrics(bk, nav, det, expo, lo=None, hi=None) -> dict:
    """與 defensive_study.Book.measure 完全相同的指標計算（照抄，只換輸入）。"""
    lo = lo or bk.dates[0]
    hi = hi or (bk.dates[-1].replace(year=bk.dates[-1].year + 1))
    idx = [i for i, d in enumerate(bk.dates) if lo <= d < hi]
    a, b = idx[0], idx[-1]
    base = nav[a]
    seg = [nav[i] / base for i in range(a, b + 1)] if base > 0 else [0.0]
    years = (bk.dates[b] - bk.dates[a]).days / 365.25
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


# ---------------------------------------------------------------- 回撤區段
def dd_episodes(nav, top=5):
    """把淨值切成互不重疊的回撤事件：(峰索引, 谷索引, 復原索引|None, 深度)。"""
    eps = []
    peak, peak_i, trough, trough_i, in_dd = nav[0], 0, nav[0], 0, False
    for i, x in enumerate(nav):
        if x >= peak:
            if in_dd:
                eps.append((peak_i, trough_i, i, trough / peak - 1.0))
                in_dd = False
            peak, peak_i, trough, trough_i = x, i, x, i
        else:
            if not in_dd:
                in_dd, trough, trough_i = True, x, i
            elif x < trough:
                trough, trough_i = x, i
    if in_dd:
        eps.append((peak_i, trough_i, None, trough / peak - 1.0))
    return sorted(eps, key=lambda e: e[3])[:top]


def window_depth(nav, a, b):
    """在 [a, b] 這段日子裡，從段內高點起算的最大跌幅。"""
    peak, dd = nav[a], 0.0
    for i in range(a, b + 1):
        peak = max(peak, nav[i])
        dd = min(dd, nav[i] / peak - 1.0)
    return dd


# ---------------------------------------------------------------- 開關與槓桿
def on_ma(bk):
    return [m is not None and c > m for c, m in zip(bk.ic, bk.ma)]


def on_slope(bk, lag=20):
    base = on_ma(bk)
    out = []
    for i in range(len(bk.dates)):
        ok = base[i] and i >= lag and bk.ma[i - lag] is not None \
            and bk.ma[i] > bk.ma[i - lag]
        out.append(bool(ok))
    return out


def on_ma50(bk):
    base, m50 = on_ma(bk), moving_average(bk.bars, 50)
    return [bool(base[i] and m50[i] is not None and bk.ma[i] is not None
                 and m50[i] > bk.ma[i]) for i in range(len(bk.dates))]


def rescale_lev(bk, lev, on, mean=0.5):
    """把逐日槓桿等比例縮放，使「核心持有日的平均槓桿」等於 mean。

    波動率目標在 cap=0.75 下平均槓桿會高於 0.5，直接比較等於偷加曝險；
    這個版本把平均拉回 0.5，才是「同樣的注、不同的分配方式」的對照。
    """
    act = [lev[i] for i in range(len(lev)) if on[i]]
    k = mean / st.fmean(act)
    return [x * k for x in lev]


def const_lev(bk, x):
    return [x] * len(bk.dates)


def vol_target_lev(bk, target, cap=0.75, win=20):
    """核心槓桿 = min(cap, 目標年化波動 ÷ 指數 20 日已實現年化波動)。"""
    r = [0.0] + [bk.ic[i] / bk.ic[i - 1] - 1.0 for i in range(1, len(bk.ic))]
    out = []
    for i in range(len(bk.ic)):
        if i < win:
            out.append(cap)
            continue
        sd = st.pstdev(r[i - win + 1:i + 1])
        rv = sd * math.sqrt(252)
        out.append(cap if rv <= 0 else min(cap, target / rv))
    return out


# ---------------------------------------------------------------- 序列風險規則
def trade_ret(bk, e: FuturesEntry) -> float:
    """單筆權益報酬（與 vehicle_series 同式；與權益水位無關）。"""
    b = e.exit_i if e.exit_i is not None else len(bk.dates) - 1
    fr = bk.fs[b] / bk.fs[e.entry_i] - 1.0
    v = (1.0 - e.leverage * COST.one_way_rate(bk.ic[e.entry_i])) * (1.0 + e.leverage * fr)
    if e.exit_i is not None:
        v *= 1.0 - e.leverage * COST.one_way_rate(bk.ic[b])
    return v - 1.0


def seq_hybrid(bk, above_cap=5.0, base_scale=0.5, punish=0.5, only_above=False):
    """上一筆虧損 → 下一筆均線之上的預算再乘 `punish`（贏了就恢復）。

    ⚠️ 高過擬合風險：規則是看著 2004-04 / 2004-08 連虧設計的。
    """
    below, _ = split_by_ma(bk)
    below_set = {e.entry_i for e in below}
    out, prev_loss, prev_above_loss = [], False, False
    for e in sorted(bk.base, key=lambda x: x.entry_i):
        if e.entry_i in below_set:
            lev = BELOW_LEV
        else:
            bad = prev_above_loss if only_above else prev_loss
            lev = min(e.leverage * base_scale * (punish if bad else 1.0), above_cap)
        ne = FuturesEntry(e.entry_i, e.exit_i, lev, e.entry_index,
                          e.stop_distance, e.trade)
        out.append(ne)
        r = trade_ret(bk, ne)
        prev_loss = r < 0
        if e.entry_i not in below_set:
            prev_above_loss = r < 0
    return out


# ---------------------------------------------------------------- 變體選單
def build(bk):
    """(名稱 → (entries, on, lev, stop))；on=None 代表完全不加核心。"""
    H = hybrid(bk, 5.0, 0.5)
    v = {}
    v["現行 風險式≤5x"] = (bk.base, None, None, None)
    v["混合半預算（無核心）"] = (H, None, None, None)
    for x in (0.25, 0.5, 0.75):
        v[f"＋核心 {x:g}x MA200"] = (H, on_ma(bk), const_lev(bk, x), None)
    for x in (0.25, 0.5, 0.75):
        v[f"＋核心 {x:g}x 斜率濾網"] = (H, on_slope(bk), const_lev(bk, x), None)
    for x in (0.25, 0.5, 0.75):
        v[f"＋核心 {x:g}x MA50>MA200"] = (H, on_ma50(bk), const_lev(bk, x), None)
    for s in (0.08, 0.12):
        for x in (0.5, 0.75):
            v[f"＋核心 {x:g}x 停損{s:.0%}"] = (H, on_ma(bk), const_lev(bk, x), s)
    for t in (0.10, 0.15):
        v[f"＋核心 波動目標{t:.0%}"] = (H, on_ma(bk), vol_target_lev(bk, t), None)
    for t in (0.10, 0.15):
        v[f"＋核心 波動目標{t:.0%}+停損12%"] = (H, on_ma(bk), vol_target_lev(bk, t), 0.12)
    for t in (0.10, 0.15):
        v[f"＋核心 波動目標{t:.0%}(均槓桿0.5)"] = (
            H, on_ma(bk), rescale_lev(bk, vol_target_lev(bk, t), on_ma(bk)), None)
    v["＋核心 0.5x 斜率+停損12%"] = (H, on_slope(bk), const_lev(bk, 0.5), 0.12)
    S = seq_hybrid(bk)
    v["序列規則（無核心）"] = (S, None, None, None)
    v["序列規則＋核心0.5x"] = (S, on_ma(bk), const_lev(bk, 0.5), None)
    v["序列規則(只看上檔)＋核心"] = (seq_hybrid(bk, only_above=True),
                                     on_ma(bk), const_lev(bk, 0.5), None)
    return v


def run(bk, spec):
    entries, on, lev, stop = spec
    if on is None:
        nav, det = vehicle_series(bk.dates, bk.fs, entries, COST, bk.ic)
        held = set()
        for e in entries:
            b = e.exit_i if e.exit_i is not None else len(bk.dates) - 1
            held.update(range(e.entry_i, b + 1))
        who = [-1] * len(bk.dates)
        cat = ["flat"] * len(bk.dates)
        for e in entries:
            b = e.exit_i if e.exit_i is not None else len(bk.dates) - 1
            for i in range(e.entry_i + 1, b + 1):
                who[i], cat[i] = e.entry_i, "pos"
        return nav, det, len(held) / len(bk.dates), cat, [0.0] * len(bk.dates), who, []
    return flex_core(bk.dates, bk.fs, entries, COST, bk.ic, on, lev, stop)


# ---------------------------------------------------------------- main
def main() -> int:
    login()
    bk = Book()
    H = hybrid(bk, 5.0, 0.5)
    print(f"期間 {bk.dates[0]} ~ {bk.dates[-1]}　交易日 {len(bk.dates)}　"
          f"訊號 {len(bk.base)} 筆\n")

    # ---- 0. 一致性驗證：flex_core 必須重現 core_overlay ----
    print("【0】實作驗證：flex_core（常數槓桿、close>MA200）vs 原生 core_overlay\n")
    nav0, det0, ex0 = core_overlay(bk.dates, bk.fs, H, COST, bk.ic, CORE, bk.ma)
    nav1, det1, ex1, cat1, cl1, who1, _ = flex_core(
        bk.dates, bk.fs, H, COST, bk.ic, on_ma(bk), const_lev(bk, CORE))
    dmax = max(abs(a / b - 1) for a, b in zip(nav0, nav1))
    print(f"  淨值最大相對差 {dmax:.3e}　在場比例 {ex0:.4f} vs {ex1:.4f}　"
          f"筆數 {len(det0)} vs {len(det1)}")
    m_ref = bk.measure(H, core=CORE)
    m_new = metrics(bk, nav1, det1, ex1)
    print(f"  CAGR {m_ref['cagr']:.4%} vs {m_new['cagr']:.4%}　"
          f"MDD {m_ref['mdd']:.4%} vs {m_new['mdd']:.4%}　"
          f"Sharpe {m_ref['sharpe']:.4f} vs {m_new['sharpe']:.4f}")
    print("  → 一致，後續變體都用 flex_core\n")

    # ---- 1. MDD 分解 ----
    nav_nc, det_nc = vehicle_series(bk.dates, bk.fs, H, COST, bk.ic)
    print("【1】基準（混合半預算＋核心 0.5x）前 5 大回撤區段的來源分解\n")
    print("  沿峰→谷逐日累計對數報酬，按當日損益的產生者歸類：")
    print("  pos＝策略部位持有日、core＝核心持有日、cost＝進出場成本、flat＝空手日\n")
    hdr = (f'  {"#":<3}{"峰":<12}{"谷":<12}{"深度":>8}{"天":>6}'
           f'{"部位段":>9}{"核心段":>9}{"成本":>8}{"部位日":>7}{"核心日":>7}'
           f'{"無核心同窗":>11}{"差":>8}')
    print(hdr)
    eps = dd_episodes(nav1, top=5)
    for k, (a, b, rec, depth) in enumerate(eps, 1):
        pos = sum(math.log(nav1[i] / nav1[i - 1]) - cl1[i]
                  for i in range(a + 1, b + 1) if cat1[i] == "pos")
        core = sum(math.log(nav1[i] / nav1[i - 1]) - cl1[i]
                   for i in range(a + 1, b + 1) if cat1[i] == "core")
        flat = sum(math.log(nav1[i] / nav1[i - 1]) - cl1[i]
                   for i in range(a + 1, b + 1) if cat1[i] == "flat")
        cst = sum(cl1[i] for i in range(a + 1, b + 1))
        npos = sum(1 for i in range(a + 1, b + 1) if cat1[i] == "pos")
        ncore = sum(1 for i in range(a + 1, b + 1) if cat1[i] == "core")
        d_nc = window_depth(nav_nc, a, b)
        print(f'  {k:<3}{bk.dates[a]!s:<12}{bk.dates[b]!s:<12}{depth:>8.1%}'
              f'{b - a:>6}{math.expm1(pos):>9.1%}{math.expm1(core):>9.1%}'
              f'{math.expm1(cst):>8.2%}{npos:>7}{ncore:>7}'
              f'{d_nc:>11.1%}{(depth - d_nc) * 100:>6.1f}pp')
        assert abs(flat) < 1e-9, flat
        chk = math.expm1(pos + core + cst)
        print(f'     檢核：三段合計 {chk:+.2%}　'
              f'實際峰→谷 {nav1[b] / nav1[a] - 1:+.2%}　'
              f'復原日 {bk.dates[rec] if rec is not None else "尚未復原"}')
        byt: dict[int, float] = {}
        for i in range(a + 1, b + 1):
            if who1[i] >= 0:
                byt[who1[i]] = byt.get(who1[i], 0.0) + math.log(nav1[i] / nav1[i - 1])
        if byt:
            print("     部位段的逐筆貢獻（窗內對數報酬換算成 %）：" + "　".join(
                f"{bk.dates[k]} {math.expm1(v):+.1%}"
                for k, v in sorted(byt.items())))
    print()
    print("  同一段時間，無核心版本自己的前 5 大回撤：")
    print(f'  {"#":<3}{"峰":<12}{"谷":<12}{"深度":>8}')
    for k, (a, b, rec, depth) in enumerate(dd_episodes(nav_nc, top=5), 1):
        print(f'  {k:<3}{bk.dates[a]!s:<12}{bk.dates[b]!s:<12}{depth:>8.1%}')
    print()

    # ---- 1b. 部位自己的回吐（MFE → 出場）----
    from tw_backdraw.futures import trade_details
    print("【1b】混合半預算的逐筆 MFE 與回吐（無核心淨值，回撤的另一個來源）\n")
    D = trade_details(bk.dates, nav_nc, H, det_nc)
    print(f'  {"進場":<12}{"出場":<12}{"槓桿":>6}{"權益報酬":>10}{"MFE":>9}'
          f'{"MAE":>9}{"期間最大回撤":>13}')
    for d in sorted(D, key=lambda x: x.trade.entry_date):
        t = d.trade
        print(f'  {t.entry_date!s:<12}{str(t.exit_date or "持有中"):<12}'
              f'{t.leverage:>6.2f}{t.ret:>10.1%}{d.mfe:>9.1%}{d.mae:>9.1%}'
              f'{d.max_drawdown:>13.1%}')
    gb = sorted(D, key=lambda d: d.max_drawdown)[:3]
    print("  期間內回吐最深的三筆："
          + "、".join(f"{d.trade.entry_date} {d.max_drawdown:.1%}"
                      f"（MFE {d.mfe:+.0%} → 出場 {d.trade.ret:+.0%}）" for d in gb))
    print()

    # ---- 2. 全期表 ----
    var = build(bk)
    res = {nm: run(bk, sp) for nm, sp in var.items()}
    M = {nm: metrics(bk, r[0], r[1], r[2]) for nm, r in res.items()}
    print("【2】全期 1999-2026：所有變體\n")
    print(HEAD)
    for nm in var:
        print(row(nm, M[nm]))
    print()

    # ---- 2b. 核心單獨（拿掉策略部位）：純粹看疊加層本身 ----
    print("【2b】核心單獨（沒有策略部位）的表現 —— 疊加層本身的風險長相\n")
    print(f'  {"核心設定":<26}{"在場":>6}{"CAGR":>8}{"MDD":>9}{"Sharpe":>8}'
          f'{"Calmar":>8}   前三大回撤')
    solo = {}
    for nm, on_, lv_, sp_ in (
            ("0.25x close>MA200", on_ma(bk), const_lev(bk, 0.25), None),
            ("0.5x close>MA200", on_ma(bk), const_lev(bk, 0.5), None),
            ("0.75x close>MA200", on_ma(bk), const_lev(bk, 0.75), None),
            ("0.5x 斜率濾網", on_slope(bk), const_lev(bk, 0.5), None),
            ("0.5x MA50>MA200", on_ma50(bk), const_lev(bk, 0.5), None),
            ("0.5x 停損8%", on_ma(bk), const_lev(bk, 0.5), 0.08),
            ("0.5x 停損12%", on_ma(bk), const_lev(bk, 0.5), 0.12),
            ("0.75x 停損8%", on_ma(bk), const_lev(bk, 0.75), 0.08),
            ("波動目標10%", on_ma(bk), vol_target_lev(bk, 0.10), None),
            ("波動目標15%", on_ma(bk), vol_target_lev(bk, 0.15), None),
            ("波動目標10%(均槓桿0.5)", on_ma(bk),
             rescale_lev(bk, vol_target_lev(bk, 0.10), on_ma(bk)), None),
            ("波動目標15%(均槓桿0.5)", on_ma(bk),
             rescale_lev(bk, vol_target_lev(bk, 0.15), on_ma(bk)), None),
            ("買進持有 1x（對照）", [True] * len(bk.dates), const_lev(bk, 1.0), None)):
        nv_, dt_, ex_, *_ = flex_core(bk.dates, bk.fs, [], COST, bk.ic, on_, lv_, sp_)
        m = metrics(bk, nv_, dt_, ex_)
        solo[nm] = m
        e3 = "　".join(f"{bk.dates[a]}→{bk.dates[b]} {d:.0%}"
                       for a, b, _, d in dd_episodes(nv_, top=3))
        print(f'  {nm:<26}{m["ex"]:>6.0%}{m["cagr"]:>8.1%}{m["mdd"]:>9.1%}'
              f'{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}   {e3}')
    print()

    # ---- 3. 前後段檢驗 ----
    print("【3】前後段檢驗（1999-2012 / 2013-2026），基準＝混合半預算＋核心0.5x\n")
    BASE = "＋核心 0.5x MA200"
    seg = {}
    for nm in var:
        seg[nm] = {}
        for lab, lo, hi in PERIODS[1:]:
            seg[nm][lab] = metrics(bk, res[nm][0], res[nm][1], res[nm][2], lo, hi)
    for lab, lo, hi in PERIODS[1:]:
        print(f"  {lab}")
        print("  " + HEAD)
        for nm in var:
            print("  " + row(nm, seg[nm][lab]))
        print()
    f_lab, b_lab = PERIODS[1][0], PERIODS[2][0]

    def cmp_table(ref):
        print(f"  vs「{ref}」的變化（Δ = 變體 − 基準）\n")
        print(f'  {"方案":<26}{"前ΔSharpe":>11}{"後ΔSharpe":>11}'
              f'{"前ΔCalmar":>11}{"後ΔCalmar":>11}{"前ΔMDD":>9}{"後ΔMDD":>9}  判定')
        for nm in var:
            if nm == ref:
                continue
            ds = [seg[nm][l]["sharpe"] - seg[ref][l]["sharpe"] for l in (f_lab, b_lab)]
            dc = [seg[nm][l]["calmar"] - seg[ref][l]["calmar"] for l in (f_lab, b_lab)]
            dm = [seg[nm][l]["mdd"] - seg[ref][l]["mdd"] for l in (f_lab, b_lab)]
            eps = 5e-3
            sgn = lambda x: 0 if abs(x) < eps else (1 if x > 0 else -1)
            ss, sc = [sgn(x) for x in ds], [sgn(x) for x in dc]
            if ss[0] * ss[1] < 0 or sc[0] * sc[1] < 0:
                v = "未通過（方向不一致）"
            elif ss[0] > 0 or sc[0] > 0 or ss[1] > 0 or sc[1] > 0:
                v = "通過（方向一致且較優）"
            elif ss == [0, 0] and sc == [0, 0]:
                v = "無差異（規則從未觸發）"
            else:
                v = "方向一致但兩段皆劣（不採用）"
            print(f'  {nm:<26}{ds[0]:>11.2f}{ds[1]:>11.2f}{dc[0]:>11.2f}'
                  f'{dc[1]:>11.2f}{dm[0]:>9.1%}{dm[1]:>9.1%}  {v}')
        print()

    cmp_table(BASE)
    cmp_table("混合半預算（無核心）")

    # ---- 4. 各變體前三大回撤 ----
    print("【4】各變體的前三大回撤區段（峰 → 谷，深度）\n")
    for nm in var:
        e3 = dd_episodes(res[nm][0], top=3)
        s = "　".join(f"{bk.dates[a]}→{bk.dates[b]} {d:.1%}" for a, b, _, d in e3)
        print(f"  {nm:<26}{s}")
    print()

    # ---- 4b. 機制檢查：核心停損觸發了幾次、波動目標的槓桿長什麼樣 ----
    print("【4b】核心停損的觸發次數與波動目標的實際槓桿\n")
    for nm, sp in var.items():
        if sp[3] is None:
            continue
        st_i = res[nm][6]
        print(f"  {nm:<26}停損觸發 {len(st_i)} 次"
              + ("　" + "、".join(str(bk.dates[i]) for i in st_i[:8]) if st_i else ""))
    on = on_ma(bk)
    for t in (0.10, 0.15):
        lv = vol_target_lev(bk, t)
        act = [lv[i] for i in range(len(lv)) if on[i]]
        print(f"  波動目標{t:.0%}：核心持有日的槓桿 平均 {st.fmean(act):.2f}　"
              f"中位 {st.median(act):.2f}　最小 {min(act):.2f}　"
              f"觸頂(=0.75) 佔 {sum(1 for x in act if x >= 0.7499) / len(act):.0%}")
    print()

    # ---- 5. 序列規則細節 ----
    print("【5】序列風險規則的逐筆檢視　⚠️ 高過擬合風險：規則是看著連虧設計的\n")
    base_map = {e.entry_i: e for e in H}
    seq = {e.entry_i: e for e in seq_hybrid(bk)}
    seq2 = {e.entry_i: e for e in seq_hybrid(bk, only_above=True)}
    below_set = {e.entry_i for e in split_by_ma(bk)[0]}
    print(f'  {"進場":<12}{"位置":<8}{"基準槓桿":>9}{"序列A":>8}{"序列B":>8}'
          f'{"基準報酬":>10}{"序列A報酬":>11}{"序列B報酬":>11}')
    print("  序列A＝上一筆（不分位置）虧損就砍半；序列B＝上一筆「均線之上」的訊號虧損才砍半")
    for k in sorted(base_map):
        b, a1, a2 = base_map[k], seq[k], seq2[k]
        print(f'  {bk.dates[k]!s:<12}'
              f'{"均線下" if k in below_set else "均線上":<8}'
              f'{b.leverage:>9.2f}{a1.leverage:>8.2f}{a2.leverage:>8.2f}'
              f'{trade_ret(bk, b):>10.1%}{trade_ret(bk, a1):>11.1%}'
              f'{trade_ret(bk, a2):>11.1%}')
    chg1 = [k for k in base_map if abs(seq[k].leverage - base_map[k].leverage) > 1e-9]
    chg2 = [k for k in base_map if abs(seq2[k].leverage - base_map[k].leverage) > 1e-9]
    print(f"  序列A 實際改動 {len(chg1)} 筆："
          + "、".join(str(bk.dates[k]) for k in sorted(chg1)))
    print(f"  序列B 實際改動 {len(chg2)} 筆："
          + "、".join(str(bk.dates[k]) for k in sorted(chg2)))
    print()

    # ---- 6. 逐筆錨定 walk-forward ----
    print("【6】逐筆錨定 walk-forward（仿 walk_forward_sizing.py）\n")
    print("  ⚠️ 這裡的 Calmar 沿用 walk_forward_sizing.seg_metrics 的定義："
          "「區段總報酬 ÷ |MDD|」，")
    print("  不是【2】表的年化 CAGR÷|MDD|，兩者不可互相比較。\n")
    nv_all = {nm: res[nm][0] for nm in var}
    SMALL = ["現行 風險式≤5x", "混合半預算（無核心）", "＋核心 0.25x MA200",
             "＋核心 0.5x MA200", "＋核心 0.75x MA200", "＋核心 0.5x 斜率濾網",
             "＋核心 0.5x 停損8%", "＋核心 波動目標15%", "序列規則＋核心0.5x"]
    entries = sorted(bk.base, key=lambda e: e.entry_i)
    fmt = lambda m: (f"總報酬 {m['total']:+10.1%}  MDD {m['mdd']:6.1%}  "
                     f"Sharpe {m['sharpe']:4.2f}  Calmar {m['calmar']:6.1f}")
    for menu_nm, nv in ((f"大選單（全部 {len(nv_all)} 個候選）", nv_all),
                        (f"小選單（{len(SMALL)} 個候選）", {k: nv_all[k] for k in SMALL})):
        print(f"  === {menu_nm} ===")
        for obj in ("calmar", "sharpe"):
            for burn in (8, 10, 12):
                growth, picks, start_i = anchored(bk, nv, nv, obj, burn)
                comp = composite_metrics(bk, nv, growth, picks, start_i,
                                         entries[burn:])
                oos_a = entries[burn].entry_i - 1
                oos = {nm: seg_metrics(nv[nm], oos_a, len(bk.dates) - 1) for nm in nv}
                uniq = []
                for _, nm in picks:
                    if not uniq or uniq[-1] != nm:
                        uniq.append(nm)
                bestn = max(oos, key=lambda nm: oos[nm][obj])
                print(f"  目標 {obj}／燒入 {burn} 筆　OOS {bk.dates[oos_a]} 起"
                      f"（{len(growth)} 筆）　切換 {len(uniq) - 1} 次")
                print("    路徑：" + " → ".join(uniq))
                print(f"    {'複合（真樣本外）':<26}{fmt(comp)}")
                print(f"    {'事後最佳（' + bestn + '）':<26}{fmt(oos[bestn])}")
                print(f"    {'基準 ＋核心0.5x':<26}{fmt(oos['＋核心 0.5x MA200'])}")
                print(f"    {'基準 混合半預算無核心':<26}"
                      f"{fmt(oos['混合半預算（無核心）'])}")
                print(f"    {'基準 現行':<26}{fmt(oos['現行 風險式≤5x'])}")
                print()
    print("  完整候選在 OOS 段的表現（燒入 10 筆、OOS 2009-07-01 起）：")
    oos_a = entries[10].entry_i - 1
    for nm in nv_all:
        print(f"    {nm:<26}"
              f"{fmt(seg_metrics(nv_all[nm], oos_a, len(bk.dates) - 1))}")
    print()

    # ---- 7. 目標檢查 ----
    print("【7】有沒有變體達到 CAGR > 30% 且 Calmar > 4？\n")
    hit = [nm for nm in var if M[nm]["cagr"] > 0.30 and M[nm]["calmar"] > 4.0]
    print(f"  達標者：{hit if hit else '無（一個都沒有）'}\n")
    print(f'  {"":<26}{"CAGR":>8}{"MDD":>9}{"Calmar":>8}   離目標的距離')
    for nm in sorted(var, key=lambda x: -M[x]["calmar"])[:5]:
        m = M[nm]
        print(f'  {nm:<26}{m["cagr"]:>8.1%}{m["mdd"]:>9.1%}{m["calmar"]:>8.3f}'
              f'   CAGR 差 {0.30 - m["cagr"]:.1%}　Calmar 差 {4.0 - m["calmar"]:.2f}'
              f'（要 CAGR 30% 且 MDD ≤ 7.5%）')
    top_c = max(var, key=lambda nm: M[nm]["cagr"])
    print(f'  {"（最高 CAGR）" + top_c:<26}{M[top_c]["cagr"]:>8.1%}'
          f'{M[top_c]["mdd"]:>9.1%}{M[top_c]["calmar"]:>8.3f}')
    print(f'\n  全體最淺 MDD = {max(M[nm]["mdd"] for nm in var):.1%}'
          f'（{max(var, key=lambda nm: M[nm]["mdd"])}），'
          f'離 −7.5% 還差 {abs(max(M[nm]["mdd"] for nm in var)) - 0.075:.1%}')
    print("  後段 2013–2026 單獨看，最高 CAGR 是 "
          f'{max(seg, key=lambda nm: seg[nm][b_lab]["cagr"])}：'
          f'{max(seg[nm][b_lab]["cagr"] for nm in seg):.1%}／'
          f'Calmar {seg[max(seg, key=lambda nm: seg[nm][b_lab]["cagr"])][b_lab]["calmar"]:.2f}'
          " —— 即使只挑最好的一段，Calmar 也只有 1.3 級別。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
