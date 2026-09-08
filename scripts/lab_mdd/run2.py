#!/usr/bin/env python3
"""A 線第二段：前後段檢驗、回撤拆解與歸因、鄰域穩健性、逐筆錨定 walk-forward。"""
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
from A_giveback import *          # noqa: F403
from walk_forward_sizing import (anchored, composite_metrics,   # noqa: E402
                                 seg_metrics)

bars, fs = load()
labs = {t: Lab(bars, fs, t) for t in (0.05, 0.06, 0.07, 0.08, 0.09, 0.10)}
L = labs[0.08]
D = L.dates
HY = L.hybrid(5.0, 0.5)

CASES = [
    ("★基準 混合半預算＋核心", 0.08, dict(core=CORE)),
    ("a trail 5%＋核心", 0.05, dict(core=CORE)),
    ("a trail 6%＋核心", 0.06, dict(core=CORE)),
    ("a trail 10%＋核心", 0.10, dict(core=CORE)),
    ("b +50%→1.5x＋核心", 0.08, dict(core=CORE, step=(0.5, 1.5))),
    ("b +100%→1.5x＋核心", 0.08, dict(core=CORE, step=(1.0, 1.5))),
    ("b +100%→1x＋核心", 0.08, dict(core=CORE, step=(1.0, 1.0))),
    ("b +125%→1x＋核心", 0.08, dict(core=CORE, step=(1.25, 1.0))),
    ("b +150%→1x＋核心", 0.08, dict(core=CORE, step=(1.5, 1.0))),
    ("b +150%→1.5x＋核心", 0.08, dict(core=CORE, step=(1.5, 1.5))),
    ("c 權益−10%＋核心", 0.08, dict(core=CORE, eq_trail=0.10)),
    ("c 權益−15%＋核心", 0.08, dict(core=CORE, eq_trail=0.15)),
    ("c 權益−20%＋核心", 0.08, dict(core=CORE, eq_trail=0.20)),
    ("b+c +100%→1.5x ＋ 權益−15%", 0.08,
     dict(core=CORE, step=(1.0, 1.5), eq_trail=0.15)),
]
RUN = {}
for nm, t, kw in CASES:
    lb = labs[t]
    RUN[nm] = (lb, *lb.nav(lb.hybrid(5.0, 0.5), **kw))
RUN["現行 風險式≤5x"] = (L, *L.nav(L.base))
RUN["混合半預算（無核心）"] = (L, *L.nav(HY))
ORDER = ["現行 風險式≤5x", "混合半預算（無核心）"] + [c[0] for c in CASES]

print("=" * 155)
print("【1】前後段檢驗（1999–2012 ／ 2013–2026）\n")
for lab_p, lo, hi in PERIODS:
    print(f"  {lab_p}")
    print("  " + HEAD)
    for nm in ORDER:
        lb, nav, det, expo = RUN[nm]
        print("  " + row(nm, metrics(lb.dates, nav, det, expo, lo, hi)))
    print()

print("=" * 155)
print("【2】前三大回撤區段拆解\n")
for nm in ORDER:
    lb, nav, det, expo = RUN[nm]
    print_dd(lb.dates, nav, nm)
    print()


# ---------------------------------------------------------------- 回撤歸因
def attribute(dates, nav, det, a, b):
    """把 [a,b] 的淨值變化拆成「各筆部位期間」與「空手/核心期間」的乘積。"""
    ix = {d: i for i, d in enumerate(dates)}
    spans = []
    for t in det:
        e = ix[t["entry_date"]]
        x = ix[t["exit_date"]] if t["exit_date"] else len(dates) - 1
        spans.append((e, x, t))
    owner = [None] * len(dates)
    for e, x, t in spans:
        for i in range(e, x + 1):
            owner[i] = t["entry_date"]
    parts = {}
    for i in range(a + 1, b + 1):
        if nav[i - 1] <= 0:
            continue
        k = owner[i] or "空手/核心"
        parts[k] = parts.get(k, 1.0) * (nav[i] / nav[i - 1])
    return parts


print("=" * 155)
print("【3】基準前三大回撤的歸因（區段內乘積分解，總乘積＝1+深度）\n")
for nm in ["★基準 混合半預算＋核心", "b +100%→1.5x＋核心", "b +150%→1x＋核心",
           "a trail 5%＋核心"]:
    lb, nav, det, expo = RUN[nm]
    ix = {d: i for i, d in enumerate(lb.dates)}
    print(f"  {nm}")
    for k, (pd_, td, _rd, dep) in enumerate(dd_episodes(lb.dates, nav, 3), 1):
        a, b = ix[pd_], ix[td]
        parts = attribute(lb.dates, nav, det, a, b)
        items = sorted(parts.items(), key=lambda kv: kv[1])
        print(f"    #{k} {pd_} → {td}　深度 {dep:6.1%}")
        for key, f in items:
            print(f"        {str(key):<14} × {f:6.3f}　({f - 1:+7.1%})")
    print()

print("=" * 155)
print("【4】基準逐筆 vs 兩個 step-down 變體\n")
lb, nav, det, expo = RUN["★基準 混合半預算＋核心"]
b15 = {t["entry_date"]: t for t in RUN["b +100%→1.5x＋核心"][2]}
b10 = {t["entry_date"]: t for t in RUN["b +150%→1x＋核心"][2]}
print(f'{"進場":<12}{"出場":<12}{"槓桿":>6}{"MFE":>9}{"實現":>9}{"期間DD":>8}'
      f' │ +100→1.5x{"實現":>9}{"期間DD":>8}'
      f' │ +150→1x{"實現":>9}{"期間DD":>8}')
for t in sorted(det, key=lambda x: x["entry_date"]):
    u, w = b15[t["entry_date"]], b10[t["entry_date"]]
    print(f'{t["entry_date"]!s:<12}{t["exit_date"]!s:<12}{t["lev"]:>6.2f}'
          f'{t["mfe"]:>9.1%}{t["ret"]:>9.1%}{t["maxdd"]:>8.1%}'
          f' │          {u["ret"]:>9.1%}{u["maxdd"]:>8.1%}'
          f' │        {w["ret"]:>9.1%}{w["maxdd"]:>8.1%}')

print("\n" + "=" * 155)
print("【5】step-down 鄰域穩健性（混合半預算＋核心，trail 8%）\n")
XS = (0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00)
YS = (1.0, 1.5, 2.0, 2.5)
GRID = {(X, Y): L.measure(HY, core=CORE, step=(X, Y)) for X in XS for Y in YS}
base_m = metrics(D, *RUN["★基準 混合半預算＋核心"][1:])
for metric, pct in (("cagr", True), ("mdd", True), ("calmar", False),
                    ("sharpe", False)):
    print(f"  {metric}")
    print("  " + f'{"X＼Y":<8}' + "".join(f"{f'{y:g}x':>9}" for y in YS)
          + f'{"基準":>10}')
    for X in XS:
        cells = "".join((f'{GRID[(X,Y)][metric]:>9.1%}' if pct
                         else f'{GRID[(X,Y)][metric]:>9.2f}') for Y in YS)
        tail = (f'{base_m[metric]:>10.1%}' if pct else f'{base_m[metric]:>10.2f}')
        print("  " + f'{f"+{X:.0%}":<8}' + cells + tail)
    print()

print("  trail 鄰域（混合半預算＋核心）")
print("  " + HEAD)
for t in (0.05, 0.06, 0.07, 0.08, 0.09, 0.10):
    lb2 = labs[t]
    print("  " + row(f"trail {t:.0%}",
                     lb2.measure(lb2.hybrid(5.0, 0.5), core=CORE)))

print("\n  觸發統計：各 (X,Y) 有幾筆部位真的降過槓桿")
for X in XS:
    _, dt, _ = L.nav(HY, core=CORE, step=(X, 1.5))
    hit = [t for t in dt if t["final_lev"] < t["lev"] - 1e-9]
    fr = sum(1 for t in hit if t["entry_date"].year < 2013)
    print(f"    +{X:.0%}　觸發 {len(hit):>2} 筆（前段 {fr}／後段 {len(hit)-fr}）"
          f"：" + "、".join(str(t["entry_date"]) for t in hit))

print("\n  權益停利觸發統計")
for z in (0.10, 0.15, 0.20):
    _, dt, _ = L.nav(HY, core=CORE, eq_trail=z)
    hit = [t for t in dt if t["reason"] == "權益停利"]
    fr = sum(1 for t in hit if t["entry_date"].year < 2013)
    print(f"    −{z:.0%}　提前出場 {len(hit):>2} 筆（前段 {fr}／後段 {len(hit)-fr}）")

print("\n" + "=" * 155)
print("【6】逐筆錨定 walk-forward（候選＝變體＋基準）\n")


class Shim:
    pass


sh = Shim()
sh.dates, sh.base = D, L.base
MENU = {
    "現行≤5x": RUN["現行 風險式≤5x"][1],
    "混半＋核心": RUN["★基準 混合半預算＋核心"][1],
    "trail5%": RUN["a trail 5%＋核心"][1],
    "trail6%": RUN["a trail 6%＋核心"][1],
    "step+50→1.5": RUN["b +50%→1.5x＋核心"][1],
    "step+100→1.5": RUN["b +100%→1.5x＋核心"][1],
    "step+150→1x": RUN["b +150%→1x＋核心"][1],
    "權益−15%": RUN["c 權益−15%＋核心"][1],
}
entries = sorted(L.base, key=lambda e: e.entry_i)
fmt = (lambda m: f"總報酬 {m['total']:+9.1%}  MDD {m['mdd']:6.1%}  "
                 f"Sharpe {m['sharpe']:4.2f}  Calmar {m['calmar']:6.2f}")
for obj in ("calmar", "sharpe"):
    for burn in (8, 10, 12):
        growth, picks, start_i = anchored(sh, MENU, MENU, obj, burn)
        comp = composite_metrics(sh, MENU, growth, picks, start_i,
                                 entries[burn:])
        a0 = entries[burn].entry_i - 1
        oos = {nm: seg_metrics(MENU[nm], a0, len(D) - 1) for nm in MENU}
        uniq = []
        for _, nm in picks:
            if not uniq or uniq[-1] != nm:
                uniq.append(nm)
        sw = sum(1 for i in range(1, len(picks)) if picks[i][1] != picks[i-1][1])
        print(f"  目標 {obj}／燒入 {burn} 筆　OOS {D[a0]} 起（{len(growth)} 筆）")
        print(f"    路徑（切換 {sw} 次）：" + " → ".join(uniq))
        print(f"    {'複合（真樣本外）':<14}  {fmt(comp)}")
        for nm in MENU:
            print(f"    {nm:<14}  {fmt(oos[nm])}")
        print()

print("=" * 155)
print("【7】目標檢核：有沒有 CAGR > 30% 且 Calmar > 4？\n")
ALL = {}
for nm in ORDER:
    lb, nav, det, expo = RUN[nm]
    ALL[nm] = metrics(lb.dates, nav, det, expo)
for (X, Y), m in GRID.items():
    ALL[f"step +{X:.0%}→{Y:g}x＋核心"] = m
best_c = max(ALL.items(), key=lambda kv: kv[1]["cagr"])
best_k = max(ALL.items(), key=lambda kv: kv[1]["calmar"])
print(f"  變體總數（含網格）：{len(ALL)}")
print(f"  達標（CAGR>30% 且 Calmar>4）："
      f"{sum(1 for m in ALL.values() if m['cagr'] > 0.30 and m['calmar'] > 4)} 個")
print(f"  最高 CAGR　：{best_c[0]}　{best_c[1]['cagr']:.1%}"
      f"（距 30% 還差 {0.30 - best_c[1]['cagr']:.1%} 個百分點）")
print(f"  最高 Calmar：{best_k[0]}　{best_k[1]['calmar']:.2f}"
      f"（距 4 還差 {4 - best_k[1]['calmar']:.2f}，需 MDD 縮到 "
      f"{abs(best_k[1]['cagr'])/4:.1%} 才成立）")
print("\n  全期 Calmar 前 8 名")
print("  " + HEAD)
for nm, m in sorted(ALL.items(), key=lambda kv: -kv[1]["calmar"])[:8]:
    print("  " + row(nm, m))
