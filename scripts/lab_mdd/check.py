import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0,str(_LAB))
from A_giveback import *
from tw_backdraw.futures import core_overlay, vehicle_series

bars, fs = load()
L = Lab(bars, fs, 0.08)
print("訊號", len(L.base), "低於MA200", len(L.filtered))
print(HEAD)
print(row("現行 風險式≤5x", L.measure(L.base)))
print(row("混合半預算", L.measure(L.hybrid(5.0,0.5))))
print(row("混合半預算＋核心", L.measure(L.hybrid(5.0,0.5), core=CORE)))

# 交叉驗證：與 repo 的 vehicle_series / core_overlay 對照
nav1,_ = vehicle_series(L.dates, L.fs, L.hybrid(5.0,0.5), COST, L.ic)
nav2,_,ex2 = core_overlay(L.dates, L.fs, L.hybrid(5.0,0.5), COST, L.ic, CORE, L.ma)
navA,_,exA = L.nav(L.hybrid(5.0,0.5))
navB,_,exB = L.nav(L.hybrid(5.0,0.5), core=CORE)
print("max abs diff vs vehicle_series:", max(abs(a-b) for a,b in zip(nav1,navA)))
print("max abs diff vs core_overlay :", max(abs(a-b) for a,b in zip(nav2,navB)), "expo", ex2, exB)
