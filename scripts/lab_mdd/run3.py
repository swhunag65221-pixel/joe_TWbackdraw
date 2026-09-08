#!/usr/bin/env python3
"""A 線第三段：回吐控制的理論上限（oracle）、以及 CAGR–Calmar 前緣。"""
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
from A_giveback import *          # noqa: F403
from tw_backdraw.futures import FuturesEntry                    # noqa: E402

bars, fs = load()
L = Lab(bars, fs, 0.08)
D = L.dates
HY = L.hybrid(5.0, 0.5)

print("=" * 120)
print("【1】oracle 上限：假設每筆部位期間內完全沒有回吐（路徑取 cummax）\n")
print("  這不是可執行的策略，是「任何部位內回吐控制」的下界 ——")
print("  它把每筆的期間回撤全部拿掉（等於每筆都在 MFE 出場），剩下的 MDD")
print("  就是**部位內機制碰不到**的部分：虧損筆、空手期、核心部位。\n")


def oracle(dates, nav, det):
    """每筆部位期間的路徑改成 cummax（零回吐），期間外沿用真實日報酬。

    注意：期間**外**的每一步（含出場日的隔天）都必須用真實 nav 的比值，
    只有期間**內**的步用 cummax 的比值 —— 否則出場後那一步會把整段獲利吐回去。
    """
    ix = {d: i for i, d in enumerate(dates)}
    n = len(nav)
    m = list(nav)
    inside = [False] * n          # inside[i]=True → 第 i 步屬於某筆部位期間
    for t in det:
        e = ix[t["entry_date"]]
        x = ix[t["exit_date"]] if t["exit_date"] else n - 1
        run = nav[e]
        for i in range(e, x + 1):
            run = max(run, nav[i])
            m[i] = run
            if i > e:
                inside[i] = True
    out = [nav[0]]
    for i in range(1, n):
        if inside[i]:
            r = m[i] / m[i - 1] if m[i - 1] > 0 else 1.0
        else:
            r = nav[i] / nav[i - 1] if nav[i - 1] > 0 else 1.0
        out.append(out[-1] * r)
    return out


def straighten(dates, nav, det):
    """把每筆部位期間的路徑拉成等比直線（實現報酬完全不變，只拿掉期間內起伏）。

    比 oracle 嚴格：oracle 連虧損筆都變成零損失（等於用未卜先知改變實現報酬），
    拉直則保留每一筆真實的實現報酬，只問「期間內的來回」對 MDD 貢獻多少。
    """
    ix = {d: i for i, d in enumerate(dates)}
    n = len(nav)
    inside, ratio = [False] * n, [1.0] * n
    for t in det:
        e = ix[t["entry_date"]]
        x = ix[t["exit_date"]] if t["exit_date"] else n - 1
        if x <= e or nav[e] <= 0:
            continue
        g = (nav[x] / nav[e]) ** (1.0 / (x - e))
        for i in range(e + 1, x + 1):
            inside[i], ratio[i] = True, g
    out = [nav[0]]
    for i in range(1, n):
        r = ratio[i] if inside[i] else (nav[i] / nav[i - 1]
                                        if nav[i - 1] > 0 else 1.0)
        out.append(out[-1] * r)
    return out


for nm, kw in (("★基準 混合半預算＋核心", dict(core=CORE)),
               ("混合半預算（無核心）", dict()),
               ("現行 風險式≤5x", None)):
    es = L.base if kw is None else HY
    kw = kw or {}
    nav, det, expo = L.nav(es, **kw)
    orc, stg = oracle(D, nav, det), straighten(D, nav, det)
    print(f"  {nm}　（最差單筆／>8%／MFE 三欄沿用實際交易，對兩條假想線無意義）")
    print("  " + HEAD)
    print("  " + row("實際", metrics(D, nav, det, expo)))
    print("  " + row("路徑拉直（實現報酬不變）", metrics(D, stg, det, expo)))
    print("  " + row("oracle 在 MFE 出場（未卜先知）", metrics(D, orc, det, expo)))
    print_dd(D, stg, "  路徑拉直後的前三大回撤 ＝ 部位內回吐**碰不到**的部分")
    print()

print("=" * 120)
print("【2】CAGR–Calmar 前緣：把整組槓桿等比放大 k 倍\n")
print("  問題：CAGR>30% 且 Calmar>4 需要什麼？下面掃 k，看兩者怎麼移動。\n")


def scale(entries, k, cap=None):
    return [FuturesEntry(e.entry_i, e.exit_i,
                         min(e.leverage * k, cap) if cap else e.leverage * k,
                         e.entry_index, e.stop_distance, e.trade)
            for e in entries]


print("  a) 混合半預算＋核心 0.5x，策略部位 ×k")
print("  " + HEAD)
for k in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
    print("  " + row(f"×{k:g}", L.measure(scale(HY, k), core=CORE)))
print("\n  b) 同上再加 step-down +150%→(1x×k)")
print("  " + HEAD)
for k in (1.0, 1.5, 2.0, 2.5, 3.0):
    print("  " + row(f"×{k:g} ＋step", L.measure(scale(HY, k), core=CORE,
                                                step=(1.5, 1.0 * k))))
print("\n  c) 核心槓桿 c（策略部位不變）")
print("  " + HEAD)
for c in (0.0, 0.5, 1.0, 1.5, 2.0):
    print("  " + row(f"核心 {c:g}x", L.measure(HY, core=c)))

print("\n" + "=" * 120)
print("【3】敏感度：把 step-down／權益停利的觸發序列換成含息指數代理\n")
print("  " + HEAD)
for nm, kw in (("step +150%→1x（期貨口徑）", dict(core=CORE, step=(1.5, 1.0))),
               ("step +150%→1x（含息指數）",
                dict(core=CORE, step=(1.5, 1.0), trigger="idx")),
               ("權益−15%（期貨口徑）", dict(core=CORE, eq_trail=0.15)),
               ("權益−15%（含息指數）",
                dict(core=CORE, eq_trail=0.15, trigger="idx"))):
    print("  " + row(nm, L.measure(HY, **kw)))
