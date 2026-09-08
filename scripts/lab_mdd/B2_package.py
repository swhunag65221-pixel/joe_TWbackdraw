#!/usr/bin/env python3
"""策略助理 B 第二輪：組裝並評估最終可採用套件。

1. step-down 釋出名目的去向（減碼後投入核心 0.5x vs 閒置）
2. 核心的不對稱回補（出場 MA200／回補 MA50、MA100、跌破後 20 日）
3. 最終套件的完整評估（含 walk-forward 與 §21 折扣後的前瞻區間）

不改 repo 也不改 A 的檔案；A 的 `simulate()` 直接 import 重用。
"""

from __future__ import annotations
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent

import math
import statistics as st
import sys
from datetime import date

LAB = (str(_LAB))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'scripts'))
sys.path.insert(0, LAB)

import A_giveback as A                                              # noqa: E402
from defensive_study import CORE, MA, Book                          # noqa: E402
from hybrid_study import BELOW_LEV, hybrid                          # noqa: E402
from walk_forward_sizing import (anchored, composite_metrics,       # noqa: E402
                                 seg_metrics)
from tx_data import login                                           # noqa: E402
from tw_backdraw.engine import moving_average                       # noqa: E402
from tw_backdraw.futures import (FuturesCost, core_overlay,         # noqa: E402
                                 fixed_leverage)

COST = FuturesCost()


class Pos:
    """單筆部位狀態；比 A.Pos 多一個 pos_eq（只含部位、不含疊加核心的權益）。"""
    __slots__ = ("e", "eq_at_entry", "anchor_eq", "f0", "i0", "lev", "peak",
                 "trough", "stepped", "mfe_v", "mae_v", "pos_eq")


STEP = (1.50, 1.0)          # 助理 A 選定：權益 +150% → 降到 1x
PERIODS = (("前段 1999-2012", date(1999, 1, 1), date(2013, 1, 1)),
           ("後段 2013-2026", date(2013, 1, 1), date(2027, 1, 1)))


# ---------------------------------------------------------------- 模擬器（超集）
def simulate2(dates, cont, ic, entries, cost, core=0.0, on=None,
              step=None, redeploy=False):
    """A.simulate 的超集：`on` 可傳任意布林序列（不對稱回補），
    `redeploy=True` 時，step-down 之後把釋出的名目改投入核心 `core`
    （條件同 `on`），與殘餘的 1x 部位並存到出場為止。

    redeploy=False 且 on = [close > MA200] 時，與 A.simulate 逐日完全相同
    （main 的 §0 有斷言驗證）。

    帳務：部位是固定口數（線性），核心是每日再平衡（比例），兩者同帳戶：
        E_i = E_{i-1} + Δ部位（固定口數） + 核心槓桿 × E_{i-1} × 期貨日報酬
    逐筆明細的報酬／MFE／MAE 只算部位本身，不含疊加的核心。
    """
    n = len(dates)
    on = on if on is not None else [False] * n
    by_entry = {e.entry_i: e for e in entries}
    nav = [1.0] * n
    detail = []
    equity = 1.0
    p = None
    holding_core = False        # 空手期的核心
    in_core = False             # 部位期間、step-down 之後的核心
    exposed = 0

    for i in range(n):
        if p is not None:
            pos_prev = p.pos_eq
            p.pos_eq = p.anchor_eq * (1.0 + p.lev * (cont[i] / p.f0 - 1.0))
            v = equity + (p.pos_eq - pos_prev)
            if in_core and i > 0:
                v += core * equity * (cont[i] / cont[i - 1] - 1.0)
            exposed += 1
            p.mfe_v = max(p.mfe_v, p.pos_eq)
            p.mae_v = min(p.mae_v, p.pos_eq)
            p.peak = max(p.peak, p.pos_eq)
            p.trough = min(p.trough, p.pos_eq / p.peak - 1.0)

            if p.e.exit_i is not None and i == p.e.exit_i:
                cst = p.lev * cost.one_way_rate(ic[i])
                if in_core:
                    cst += core * cost.one_way_rate(ic[i])
                v *= 1.0 - cst
                nav[i] = v
                detail.append(dict(
                    entry_date=dates[p.e.entry_i], exit_date=dates[i],
                    lev=p.e.leverage, final_lev=p.lev,
                    ret=p.pos_eq * (1 - p.lev * cost.one_way_rate(ic[i]))
                    / p.eq_at_entry - 1.0,
                    mfe=p.mfe_v / p.eq_at_entry - 1.0,
                    mae=p.mae_v / p.eq_at_entry - 1.0,
                    maxdd=p.trough, reason="引擎出場",
                    bars=i - p.e.entry_i, stepped=p.stepped))
                equity, p, holding_core, in_core = v, None, False, False
                continue

            if (step is not None and not p.stepped
                    and p.pos_eq >= p.eq_at_entry * (1.0 + step[0])
                    and p.lev > step[1]):
                rate = cost.one_way_rate(ic[i])
                cur_notional = p.lev * p.anchor_eq * cont[i] / p.f0
                delta = max(cur_notional - step[1] * p.pos_eq, 0.0)
                v -= delta * rate
                p.pos_eq -= delta * rate
                p.anchor_eq, p.f0, p.lev = p.pos_eq, cont[i], step[1]
                p.stepped = True
                p.mfe_v = max(p.mfe_v, p.pos_eq)

            if redeploy and p.stepped and core:      # 釋出的名目 → 核心
                want = bool(on[i])
                if want != in_core:
                    v *= 1.0 - core * cost.one_way_rate(ic[i])
                    in_core = want
            equity = v
            nav[i] = v
            continue

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
            p.pos_eq = p.anchor_eq
            equity = p.anchor_eq
            nav[i] = p.anchor_eq
            exposed += 1
            continue

        if core and on[i] != holding_core:
            equity *= 1.0 - core * cost.one_way_rate(ic[i])
            holding_core = on[i]
        nav[i] = equity

    if p is not None:
        detail.append(dict(
            entry_date=dates[p.e.entry_i], exit_date=None,
            lev=p.e.leverage, final_lev=p.lev,
            ret=p.pos_eq / p.eq_at_entry - 1.0,
            mfe=p.mfe_v / p.eq_at_entry - 1.0,
            mae=p.mae_v / p.eq_at_entry - 1.0,
            maxdd=p.trough, reason="未平倉",
            bars=n - 1 - p.e.entry_i, stepped=p.stepped))
    return nav, detail, exposed / n


# ---------------------------------------------------------------- 開關序列
def on_ma200(bk):
    return [m is not None and c > m for c, m in zip(bk.ic, bk.ma)]


def on_asym(bk, mode, lag=20):
    """出場：收盤 < MA200 就關；回補：依 mode 決定（狀態機，只看價格）。

    "ma50" / "ma100"：關閉後，收盤 > MA50（或 MA100）就回補（可能仍在 MA200 之下）
    "wait20"        ：關閉後至少等 `lag` 個交易日，且收盤 > MA200 才回補（對照組）
    """
    m50 = moving_average(bk.bars, 50)
    m100 = moving_average(bk.bars, 100)
    out, on, off_since = [], False, -10 ** 9
    for i in range(len(bk.dates)):
        m200 = bk.ma[i]
        c = bk.ic[i]
        if m200 is None:
            out.append(False)
            continue
        if on:
            if c < m200:
                on, off_since = False, i
        else:
            if mode == "ma50":
                on = m50[i] is not None and c > m50[i]
            elif mode == "ma100":
                on = m100[i] is not None and c > m100[i]
            elif mode == "wait20":
                on = (i - off_since >= lag) and c > m200
            else:
                raise ValueError(mode)
        out.append(on)
    return out


# ---------------------------------------------------------------- 指標
def metrics(bk, nav, det, expo, lo=None, hi=None):
    return A.metrics(bk.dates, nav, det, expo, lo, hi)


HEAD = (f'{"方案":<30}{"筆":>4}{"在場":>6}{"勝率":>7}{"CAGR":>8}{"MDD":>9}'
        f'{"Sharpe":>8}{"Calmar":>8}{"最差單筆":>10}{">8%":>5}')


def row(nm, m):
    flag = "  ⚠️爆倉" if m["ruin"] else ""
    return (f'{nm:<30}{m["n"]:>4}{m["ex"]:>6.0%}{m["win"]:>7.0%}{m["cagr"]:>8.1%}'
            f'{m["mdd"]:>9.1%}{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}'
            f'{m["worst"]:>10.1%}{m["over"]:>5}{flag}')


def dd_episodes(nav, top=5):
    eps, peak, peak_i, trough, trough_i, in_dd = [], nav[0], 0, nav[0], 0, False
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


def longest_underwater(bk, nav):
    """最長水下期間：從某個歷史新高到它被收復為止（回傳 天數, 峰日, 復原日）。"""
    best, peak, peak_i = (0, None, None), nav[0], 0
    for i, x in enumerate(nav):
        if x >= peak:
            d = (bk.dates[i] - bk.dates[peak_i]).days
            if d > best[0]:
                best = (d, bk.dates[peak_i], bk.dates[i])
            peak, peak_i = x, i
    d = (bk.dates[-1] - bk.dates[peak_i]).days
    if d > best[0]:
        best = (d, bk.dates[peak_i], None)
    return best


def max_consec_losses(det):
    cur = best = 0
    seq = []
    for t in sorted(det, key=lambda x: x["entry_date"]):
        if t["ret"] < 0:
            cur += 1
            seq.append(t["entry_date"])
            if cur > best:
                best, best_seq = cur, list(seq)
        else:
            cur, seq = 0, []
    return (best, best_seq) if best else (0, [])


def fmt_wf(m):
    return (f"總報酬 {m['total']:+10.1%}  MDD {m['mdd']:6.1%}  "
            f"Sharpe {m['sharpe']:4.2f}  Calmar(總報酬÷MDD) {m['calmar']:6.1f}")


# ---------------------------------------------------------------- main
def main() -> int:
    login()
    bk = Book()
    H = hybrid(bk, 5.0, 0.5)
    on200 = on_ma200(bk)
    D = bk.dates
    print(f"期間 {D[0]} ~ {D[-1]}　交易日 {len(D)}　訊號 {len(bk.base)} 筆\n")

    # ---- 0. 三方一致性驗證 ----
    print("【0】實作驗證：repo core_overlay ／ A.simulate ／ B.simulate2 三方對帳\n")
    nav_r, _, ex_r = core_overlay(D, bk.fs, H, COST, bk.ic, CORE, bk.ma)
    nav_a, det_a, ex_a = A.simulate(D, bk.fs, bk.ic, H, COST, core=CORE, ma=bk.ma)
    nav_b, det_b, ex_b = simulate2(D, bk.fs, bk.ic, H, COST, core=CORE, on=on200)
    print(f"  core_overlay vs A.simulate  最大相對差 "
          f"{max(abs(a / b - 1) for a, b in zip(nav_r, nav_a)):.3e}")
    print(f"  A.simulate  vs B.simulate2  最大相對差 "
          f"{max(abs(a / b - 1) for a, b in zip(nav_a, nav_b)):.3e}")
    na, _, _ = A.simulate(D, bk.fs, bk.ic, H, COST, core=CORE, ma=bk.ma, step=STEP)
    nb, _, _ = simulate2(D, bk.fs, bk.ic, H, COST, core=CORE, on=on200, step=STEP)
    print(f"  加 step-down 後 A vs B      最大相對差 "
          f"{max(abs(a / b - 1) for a, b in zip(na, nb)):.3e}")
    print(f"  在場比例 {ex_r:.4f} / {ex_a:.4f} / {ex_b:.4f}\n")

    # ---- 建構所有方案 ----
    def run(entries, core=0.0, on=None, step=None, redeploy=False):
        nav, det, ex = simulate2(D, bk.fs, bk.ic, entries, COST, core=core,
                                 on=on, step=step, redeploy=redeploy)
        return nav, det, ex

    V = {}
    V["現行 風險式≤5x"] = run(bk.base)
    V["濾網＋固定3x"] = run(fixed_leverage(bk.filtered, BELOW_LEV))
    V["混合半預算（無核心）"] = run(H)
    V["混合半預算＋核心0.5x"] = run(H, CORE, on200)
    V["＋step-down（閒置）"] = run(H, CORE, on200, STEP)
    V["＋step-down（釋出→核心）"] = run(H, CORE, on200, STEP, redeploy=True)
    for mode, nm in (("ma50", "回補 MA50"), ("ma100", "回補 MA100"),
                     ("wait20", "回補 等20日(對照)")):
        V[f"核心{nm}"] = run(H, CORE, on_asym(bk, mode))
        V[f"核心{nm}＋step-down"] = run(H, CORE, on_asym(bk, mode), STEP)
    M = {nm: metrics(bk, *V[nm]) for nm in V}
    S = {nm: {lab: metrics(bk, *V[nm], lo, hi) for lab, lo, hi in PERIODS}
         for nm in V}

    # ---- 1. step-down 的資金去向 ----
    print("【1】任務一：step-down 釋出名目的去向\n")
    print("  規則：權益達 +150% → 部位降到 1x；釋出的名目改持核心 0.5x")
    print("  （條件同核心：收盤 > MA200，每日再平衡，與殘餘 1x 部位並存到出場）\n")
    print(HEAD)
    for nm in ("混合半預算＋核心0.5x", "＋step-down（閒置）", "＋step-down（釋出→核心）"):
        print(row(nm, M[nm]))
    print()
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        for nm in ("混合半預算＋核心0.5x", "＋step-down（閒置）",
                   "＋step-down（釋出→核心）"):
            print("  " + row(nm, S[nm][lab]))
        print()
    print("  觸發 step-down 的筆數與再投入的日子：")
    _, det_sd, _ = V["＋step-down（釋出→核心）"]
    stepped = [t for t in det_sd if t.get("stepped")]
    print(f"    {len(stepped)} 筆觸發："
          + "、".join(f'{t["entry_date"]}（{t["lev"]:.0f}x→{t["final_lev"]:.0f}x，'
                      f'報酬 {t["ret"]:+.0%}）' for t in stepped))
    print()

    # ---- 2. 核心的不對稱回補 ----
    print("【2】任務二：核心的不對稱回補（出場一律 收盤 < MA200）\n")
    for mode, nm in (("ma50", "回補 MA50"), ("ma100", "回補 MA100"),
                     ("wait20", "回補 等20日(對照)")):
        o = on_asym(bk, mode)
        base_on = on200
        diff = sum(1 for a, b in zip(o, base_on) if a != b)
        print(f"  {nm}：核心開啟日 {sum(o)} 天（現行 {sum(base_on)} 天），"
              f"與現行不同的日子 {diff} 天（{diff / len(D):.1%}）")
    print()
    print(HEAD)
    print(row("混合半預算＋核心0.5x（基準）", M["混合半預算＋核心0.5x"]))
    for nm in V:
        if nm.startswith("核心回補") and "step" not in nm:
            print(row(nm, M[nm]))
    print()
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        print("  " + row("混合半預算＋核心0.5x（基準）", S["混合半預算＋核心0.5x"][lab]))
        for nm in V:
            if nm.startswith("核心回補") and "step" not in nm:
                print("  " + row(nm, S[nm][lab]))
        print()
    print("  【核心單獨】把策略部位整個拿掉，只看疊加層自己"
          "（直接檢驗「回補快 → 核心自己變好」的假說）\n")
    print(f'  {"核心設定":<26}{"在場":>6}{"CAGR":>8}{"MDD":>9}{"Sharpe":>8}'
          f'{"Calmar":>8}   最大回撤區段')
    for nm, o in (("現行 出場/回補都用 MA200", on200),
                  ("回補 MA50", on_asym(bk, "ma50")),
                  ("回補 MA100", on_asym(bk, "ma100")),
                  ("回補 等20日(對照)", on_asym(bk, "wait20"))):
        nv, dt, ex = simulate2(D, bk.fs, bk.ic, [], COST, core=CORE, on=o)
        m = metrics(bk, nv, dt, ex)
        a, b, rec, dep = dd_episodes(nv, 1)[0]
        print(f'  {nm:<26}{m["ex"]:>6.0%}{m["cagr"]:>8.1%}{m["mdd"]:>9.1%}'
              f'{m["sharpe"]:>8.2f}{m["calmar"]:>8.2f}   {D[a]}→{D[b]} {dep:.1%}')
    print()

    B0 = "混合半預算＋核心0.5x"
    FINAL = "＋step-down（閒置）"
    FINALP = "核心回補 MA100＋step-down"

    def verdict(ref):
        print(f"  vs「{ref}」的前後段方向（Δ = 變體 − 基準；方向不一致 → 未通過）\n")
        print(f'  {"方案":<30}{"前ΔSharpe":>11}{"後ΔSharpe":>11}{"前ΔCalmar":>11}'
              f'{"後ΔCalmar":>11}{"前ΔMDD":>9}{"後ΔMDD":>9}  判定')
        out = {}
        for nm in V:
            if nm == ref:
                continue
            ds = [S[nm][l]["sharpe"] - S[ref][l]["sharpe"] for l, _, _ in PERIODS]
            dc = [S[nm][l]["calmar"] - S[ref][l]["calmar"] for l, _, _ in PERIODS]
            dm = [S[nm][l]["mdd"] - S[ref][l]["mdd"] for l, _, _ in PERIODS]
            eps = 5e-3
            sgn = lambda x: 0 if abs(x) < eps else (1 if x > 0 else -1)
            ss, sc = [sgn(x) for x in ds], [sgn(x) for x in dc]
            if ss[0] * ss[1] < 0 or sc[0] * sc[1] < 0:
                v = "未通過（方向不一致）"
            elif ss[0] > 0 or sc[0] > 0 or ss[1] > 0 or sc[1] > 0:
                v = "通過（方向一致且較優）"
            elif ss == [0, 0] and sc == [0, 0]:
                v = "無差異"
            else:
                v = "方向一致但兩段皆劣"
            out[nm] = v
            print(f'  {nm:<30}{ds[0]:>11.2f}{ds[1]:>11.2f}{dc[0]:>11.2f}'
                  f'{dc[1]:>11.2f}{dm[0]:>9.1%}{dm[1]:>9.1%}  {v}')
        print()
        return out

    verdict(B0)
    print("  【隔離檢驗】把 step-down 固定住，只看回補規則的邊際貢獻：")
    verdict(FINAL)
    print("  三個回補變體加了 step-down 之後全部「通過」，但那是 step-down 自己的功勞；")
    print("  上面這張以最終套件為基準的表才是回補規則自己的證據。\n")

    print("  加上 step-down 之後的全期與前後段（回補變體）\n")
    grp = [B0, FINAL, "核心回補 MA50＋step-down", FINALP,
           "核心回補 等20日(對照)＋step-down"]
    print(HEAD)
    for nm in grp:
        print(row(nm, M[nm]))
    print()
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        for nm in grp:
            print("  " + row(nm, S[nm][lab]))
        print()

    # ---- 3. 最終套件 ----
    print(f"【3】最終套件 = 混合半預算 ＋ 核心 0.5x ＋ step-down +150%→1x\n")
    print(HEAD)
    LINE = ("現行 風險式≤5x", "濾網＋固定3x", "混合半預算（無核心）",
            "混合半預算＋核心0.5x", FINAL, "＋step-down（釋出→核心）", FINALP)
    for nm in LINE:
        print(row(nm, M[nm]))
    print()
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        for nm in LINE:
            print("  " + row(nm, S[nm][lab]))
        print()

    nav_f, det_f, ex_f = V[FINAL]
    print("  最終套件的逐筆交易：\n")
    print(f'  {"進場":<12}{"出場":<12}{"持有":>5}{"進場槓桿":>9}{"最終槓桿":>9}'
          f'{"權益報酬":>10}{"MFE":>9}{"MAE":>9}{"期間最大回撤":>13}  step')
    for t in sorted(det_f, key=lambda x: x["entry_date"]):
        print(f'  {t["entry_date"]!s:<12}{str(t["exit_date"] or "持有中"):<12}'
              f'{t["bars"]:>5}{t["lev"]:>9.2f}{t["final_lev"]:>9.2f}'
              f'{t["ret"]:>10.1%}{t["mfe"]:>9.1%}{t["mae"]:>9.1%}'
              f'{t["maxdd"]:>13.1%}  {"✔" if t.get("stepped") else ""}')
    print()
    for nm in (B0, FINAL, FINALP):
        print(f"  【{nm}】前五大回撤區段")
        for k, (a, b, rec, dep) in enumerate(dd_episodes(V[nm][0], 5), 1):
            print(f"    #{k} 峰 {D[a]} → 谷 {D[b]}　深度 {dep:6.1%}　"
                  f"復原 {D[rec] if rec is not None else '未復原'}")
        d, pk, rc = longest_underwater(bk, V[nm][0])
        print(f"    最長水下：{d} 天（{d / 365.25:.1f} 年）　{pk} → "
              f"{rc if rc else '尚未收復'}")
        n_l, seq = max_consec_losses(V[nm][1])
        print(f"    最長連虧：{n_l} 筆　" + "、".join(str(x) for x in seq))
        print()

    print("  【診斷】OOS 窗（2009-07 起）的 MDD 由哪一段決定？"
          "各方案在 2012-03-02 ~ 2020-06-02 這段的窗內最大跌幅：")
    a = next(i for i, d in enumerate(D) if d >= date(2012, 3, 2))
    b = next(i for i, d in enumerate(D) if d >= date(2020, 6, 2))
    for nm in (B0, FINAL, FINALP):
        nv = V[nm][0]
        pk = dep = 0.0
        for i in range(a, b + 1):
            pk = max(pk, nv[i])
            dep = min(dep, nv[i] / pk - 1.0)
        print(f"    {nm:<30}{dep:>8.1%}")
    print("    （2012-01-18 那筆從未達 +150%，step-down 對這一段完全沒有作用）\n")

    # ---- 4. 逐筆錨定 walk-forward ----
    print("【4】逐筆錨定 walk-forward（候選 6 個，仿 walk_forward_sizing.py）\n")
    print("  ⚠️ 本節 Calmar = 區段總報酬 ÷ |MDD|（seg_metrics 定義），"
          "與上表年化 Calmar 不可互比。\n")
    BASEMENU = ["現行 風險式≤5x", "濾網＋固定3x", "混合半預算（無核心）", B0, FINAL]
    entries = sorted(bk.base, key=lambda e: e.entry_i)
    for tag, keys in (("選單 A：5 個候選（不含被否決的回補變體）", BASEMENU),
                      ("選單 B：6 個候選（加上 核心回補 MA100＋step-down）",
                       BASEMENU + [FINALP])):
      menu = {nm: V[nm][0] for nm in keys}
      print(f"  === {tag} ===\n")
      for obj in ("calmar", "sharpe"):
        for burn in (8, 10, 12):
              growth, picks, start_i = anchored(bk, menu, menu, obj, burn)
              comp = composite_metrics(bk, menu, growth, picks, start_i,
                                       entries[burn:])
              oos_a = entries[burn].entry_i - 1
              uniq = []
              for _, nm in picks:
                  if not uniq or uniq[-1] != nm:
                      uniq.append(nm)
              print(f"  目標 {obj}／燒入 {burn} 筆　OOS {D[oos_a]} 起"
                    f"（{len(growth)} 筆）　切換 {len(uniq) - 1} 次")
              print("    路徑：" + " → ".join(uniq))
              print(f'    {"複合（真樣本外）":<30}{fmt_wf(comp)}')
              for nm in menu:
                  print(f"    {nm:<30}"
                        f"{fmt_wf(seg_metrics(menu[nm], oos_a, len(D) - 1))}")
              print()

    # ---- 5. §21 折扣後的前瞻期望值 ----
    print("【5】套用 §21「帳面 3～4 倍樣本內灌水」折扣後的前瞻期望值區間\n")
    yrs = (D[-1] - D[0]).days / 365.25
    print(f"  期間 {yrs:.1f} 年。做法：把**總報酬（%）**除以 3 與 4，再換回年化；")
    print("  MDD 不折扣（§21：風險比報酬更能外推，訓練→測試相關 +0.610）。\n")
    print(f'  {"方案":<30}{"帳面總報酬":>12}{"帳面CAGR":>10}{"÷3 CAGR":>10}'
          f'{"÷4 CAGR":>10}{"MDD":>9}{"÷3 Calmar":>11}{"÷4 Calmar":>11}')
    for nm in ("現行 風險式≤5x", "混合半預算（無核心）", B0, FINAL, FINALP):
        nav = V[nm][0]
        tot = nav[-1] / nav[0] - 1.0
        m = M[nm]
        out = [m["cagr"]]
        for k in (3.0, 4.0):
            g = 1.0 + tot / k
            out.append(g ** (1 / yrs) - 1.0)
        print(f'  {nm:<30}{tot:>12.0%}{out[0]:>10.1%}{out[1]:>10.1%}'
              f'{out[2]:>10.1%}{m["mdd"]:>9.1%}'
              f'{out[1] / abs(m["mdd"]):>11.2f}{out[2] / abs(m["mdd"]):>11.2f}')
    print()

    # ---- 6. 目標檢查 ----
    print("【6】離 CAGR > 30% 且 Calmar > 4 還差多少\n")
    for FN in (FINAL, FINALP):
        m = M[FN]
        nav = V[FN][0]
        tot = nav[-1] / nav[0] - 1.0
        print(f'  【{FN}】帳面 CAGR {m["cagr"]:.1%}　MDD {m["mdd"]:.1%}　'
              f'Calmar {m["calmar"]:.3f}　→ CAGR 差 {0.30 - m["cagr"]:.1%}、'
              f'Calmar 差 {4.0 - m["calmar"]:.2f}')
        for k in (3.0, 4.0):
            c = (1 + tot / k) ** (1 / yrs) - 1
            print(f'      折扣 ÷{k:.0f}：CAGR {c:.1%}　'
                  f'Calmar {c / abs(m["mdd"]):.2f}')
    m = M[FINAL]
    print(f'  最終套件（帳面）：CAGR {m["cagr"]:.1%}　MDD {m["mdd"]:.1%}　'
          f'Calmar {m["calmar"]:.3f}')
    print(f'    → CAGR 差 {0.30 - m["cagr"]:.1%}；Calmar 差 {4.0 - m["calmar"]:.2f}；'
          f'要 Calmar 4 且 CAGR 30%，MDD 必須 ≤ 7.5%（目前 {m["mdd"]:.1%}）')
    nav = V[FINAL][0]
    tot = nav[-1] / nav[0] - 1.0
    for k in (3.0, 4.0):
        c = (1 + tot / k) ** (1 / yrs) - 1
        print(f'    折扣 ÷{k:.0f} 後：CAGR {c:.1%}　Calmar {c / abs(m["mdd"]):.2f}')
    best = max(M, key=lambda x: M[x]["calmar"])
    print(f'  全體最高年化 Calmar：{best} {M[best]["calmar"]:.3f}'
          f'（CAGR {M[best]["cagr"]:.1%}、MDD {M[best]["mdd"]:.1%}）')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
