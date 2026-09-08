#!/usr/bin/env python3
"""A 線主實驗：(a) 移動停利幅度、(b) 獲利後降槓桿、(c) 權益口徑停利。"""
import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
from A_giveback import *          # noqa: F403

TRAILS = (0.04, 0.05, 0.06, 0.08, 0.10, 0.99)
STEPS = ((0.5, 1.5), (0.5, 2.0), (1.0, 1.5), (1.0, 2.0))
ZS = (0.10, 0.15, 0.20)

bars, fs = load()
labs = {t: Lab(bars, fs, t) for t in TRAILS}
L = labs[0.08]
D = L.dates

print(f"期間 {D[0]} ~ {D[-1]}　共 {len(D)} 個交易日\n")
print("各 trail_drawdown 下引擎產生的訊號數（出場時點改變 → 後續訊號可能被吃掉/放行）")
for t in TRAILS:
    lb = labs[t]
    b, a = lb.split_by_ma()
    lbl = "關閉(99%)" if t > 0.5 else f"{t:.0%}"
    print(f"  trail {lbl:>9}　訊號 {len(lb.base):>3} 筆"
          f"（MA200 下 {len(b)} ／ 上 {len(a)}）")

# 收集所有變體 → 供第 5 節總表與 walk-forward 使用
RESULTS = {}          # name -> dict(lab, entries, kw, nav, det, expo)


def add(name, lab, entries, **kw):
    nav, det, expo = lab.nav(entries, **kw)
    RESULTS[name] = dict(lab=lab, entries=entries, kw=kw,
                         nav=nav, det=det, expo=expo)
    return metrics(lab.dates, nav, det, expo)


def m_of(name, lo=None, hi=None):
    r = RESULTS[name]
    return metrics(r["lab"].dates, r["nav"], r["det"], r["expo"], lo, hi)


# ---------------------------------------------------------------- 基準
print("\n" + "=" * 150)
print("【0】基準\n")
print(HEAD)
print(row("現行 風險式≤5x", add("現行", L, L.base)))
print(row("混合半預算（無核心）", add("混合半預算", L, L.hybrid(5.0, 0.5))))
print(row("★混合半預算＋核心", add("★基準", L, L.hybrid(5.0, 0.5), core=CORE)))

# ---------------------------------------------------------------- (a)
print("\n" + "=" * 150)
print("【a】移動停利幅度 trail_drawdown（指數口徑，引擎層）\n")
print("  a-1 混合半預算＋核心")
print("  " + HEAD)
for t in TRAILS:
    lb = labs[t]
    lbl = "關閉" if t > 0.5 else f"{t:.0%}"
    nm = f"a:trail{lbl}+核心"
    print("  " + row(f"trail {lbl}{'（現行）' if t == 0.08 else ''}",
                     add(nm, lb, lb.hybrid(5.0, 0.5), core=CORE)))
print("\n  a-2 混合半預算（無核心，隔離核心的影響）")
print("  " + HEAD)
for t in TRAILS:
    lb = labs[t]
    lbl = "關閉" if t > 0.5 else f"{t:.0%}"
    print("  " + row(f"trail {lbl}", add(f"a:trail{lbl}", lb,
                                         lb.hybrid(5.0, 0.5))))

# ---------------------------------------------------------------- (b)
print("\n" + "=" * 150)
print("【b】獲利後降槓桿 step-down（權益 +X% → 降到 Y 倍，之後不加回）\n")
print("  b-1 混合半預算＋核心（trail 8% 不變）")
print("  " + HEAD)
print("  " + row("不降槓桿（基準）", m_of("★基準")))
for X, Y in STEPS:
    nm = f"b:+{X:.0%}→{Y:g}x+核心"
    print("  " + row(f"+{X:.0%} → {Y:g}x",
                     add(nm, L, L.hybrid(5.0, 0.5), core=CORE, step=(X, Y))))
print("\n  b-2 無核心")
print("  " + HEAD)
print("  " + row("不降槓桿（基準）", m_of("混合半預算")))
for X, Y in STEPS:
    print("  " + row(f"+{X:.0%} → {Y:g}x",
                     add(f"b:+{X:.0%}→{Y:g}x", L, L.hybrid(5.0, 0.5),
                         step=(X, Y))))

# ---------------------------------------------------------------- (c)
print("\n" + "=" * 150)
print("【c】權益口徑移動停利（部位權益自進場後高點回落 Z% → 出場）\n")
print("  c-1 疊加：保留 8% 指數停利，再加權益停利（兩者先到先出）")
print("  " + HEAD)
print("  " + row("僅 8% 指數停利（基準）", m_of("★基準")))
for z in ZS:
    print("  " + row(f"8% 指數 ＋ 權益 −{z:.0%}",
                     add(f"c1:Z{z:.0%}", L, L.hybrid(5.0, 0.5), core=CORE,
                         eq_trail=z)))
print("\n  c-2 取代：關閉指數停利（trail=99%），只用權益停利")
print("  " + HEAD)
LX = labs[0.99]
print("  " + row("指數停利關閉、無權益停利", m_of("a:trail關閉+核心")))
for z in ZS:
    print("  " + row(f"僅權益 −{z:.0%}",
                     add(f"c2:Z{z:.0%}", LX, LX.hybrid(5.0, 0.5), core=CORE,
                         eq_trail=z)))
print("\n  c-3 疊加，無核心")
print("  " + HEAD)
print("  " + row("僅 8% 指數停利", m_of("混合半預算")))
for z in ZS:
    print("  " + row(f"8% 指數 ＋ 權益 −{z:.0%}",
                     add(f"c1:Z{z:.0%}(無核心)", L, L.hybrid(5.0, 0.5),
                         eq_trail=z)))

import pickle
with open(str(_LAB / 'res1.pkl'),
          'wb') as f:
    pickle.dump({k: dict(nav=v["nav"], det=v["det"], expo=v["expo"],
                         trail=v["lab"].trail, kw=v["kw"])
                 for k, v in RESULTS.items()}, f)
print(f"\n（已存 {len(RESULTS)} 個變體到 res1.pkl）")
