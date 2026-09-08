import pathlib as _pl
_LAB = _pl.Path(__file__).resolve().parent
_ROOT = _LAB.parent.parent
import sys
sys.path.insert(0, str(_LAB))
from A_giveback import *
from run3 import oracle, straighten, scale
bars, fs = load(); L = Lab(bars, fs, 0.08); D = L.dates; HY = L.hybrid(5.0, 0.5)
print("\n"+"="*120)
print("【4】未卜先知（每筆在 MFE 出場）＋ 槓桿放大：Calmar 會不會過 4？\n")
print("  " + HEAD)
for k in (1.0, 1.5, 2.0, 3.0, 4.0):
    nav, det, expo = L.nav(scale(HY, k), core=CORE)
    print("  " + row(f"oracle ×{k:g}", metrics(D, oracle(D, nav, det), det, expo)))
print("\n  對照：實際（非未卜先知）")
print("  " + HEAD)
for k in (1.0, 2.0, 3.0, 4.0):
    print("  " + row(f"實際 ×{k:g}", L.measure(scale(HY, k), core=CORE)))
