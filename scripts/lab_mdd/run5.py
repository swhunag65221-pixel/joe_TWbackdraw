import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
from A_giveback import *
bars, fs = load()
print("完整 trail 前後段表（混合半預算＋核心）")
for lab_p, lo, hi in PERIODS:
    print(f"\n  {lab_p}")
    print("  " + HEAD)
    for t in (0.04,0.05,0.06,0.07,0.08,0.09,0.10):
        lb = Lab(bars, fs, t)
        print("  " + row(f"trail {t:.0%}", lb.measure(lb.hybrid(5.0,0.5), lo, hi, core=CORE)))
