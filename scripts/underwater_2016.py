#!/usr/bin/env python3
"""2016 年後（三腿都有資料）的最長水下：台指期變體與跨市場組合同基準比較（§26）。

    python3 scripts/underwater_2016.py

§24 在全期（1999–2026）測了四類槓桿，§25 只能在 2016 後的重疊視窗測組合。
本腳本把兩者放到**同一個視窗**（2016-01 ~ 2026-08，三腿資料都存在）：

  1. 台指期單腿的基準與 §24 的各類變體，在 2016 後的水下表現
  2. 跨市場固定權重組合（權重預先宣告，禁止最佳化）
  3. **兩者疊加**：核心 1.0x 的台指期腿 ＋ 美股兩腿
  4. 視窗內前後段（2016–2020 / 2021–2026）一致性檢驗

⚠️ 2016–2026 幾乎全是多頭，台指期腿在此視窗的 Calmar 膨脹約 ×2.7（§22），
且它排除了 2012–2015 的空轉年 —— 視窗內的「最長水下」本身就遠短於全期。
所有數字只能同視窗互比，不可與 §24 的全期數字對照。

⚠️⚠️ **截斷假高點**（§27 的更正）：在截斷視窗裡量水下，等於把視窗第一天
當成新高。以 2016-01 起算時，2016-03-18 會被當成高點，但它其實比全期高點
（2012-03-02）低 4.7% —— 由此量出的「2.2 年水下」是假的。
第【5】節的視窗起點敏感度就是用來擋這個錯：**任何結論都必須在多個起點下成立**。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lab_mdd"))

from A2_diversify import (daily_rebalance, metrics, signal_rebalance,  # noqa: E402
                          us_leg)
from A_giveback import Lab, load                                       # noqa: E402
from underwater_mix import underwater                                  # noqa: E402
from underwater_study import Study                                     # noqa: E402

W0, SPLIT = date(2016, 1, 1), date(2021, 1, 1)

#: 台指期腿的變體（名稱, Study.run 的參數）—— 取 §24 測過的各類槓桿
TX_VARIANTS = (
    ("最終套件（基準）", {}),
    ("核心 0.75x", dict(core=0.75)),
    ("核心 1.0x", dict(core=1.0)),
    ("補回 ≥55%／30 日", dict(repair=0.55)),
    ("減碼 +100%→1.5x", dict(step=(1.0, 1.5))),
    ("移動停利 6%", dict(trail=0.06)),
)

#: 預先宣告的固定權重（TX, UPRO, TQQQ）
WEIGHTS = (("TX 100（對照）", (1.0, 0.0, 0.0)),
           ("TX 50 / UPRO 25 / TQQQ 25", (0.50, 0.25, 0.25)),
           ("TX 40 / UPRO 30 / TQQQ 30", (0.40, 0.30, 0.30)),
           ("等權 1/3", (1 / 3, 1 / 3, 1 / 3)))

HEAD = (f'{"方案":<30}{"最長水下":>8}  {"起→收復":<24}{"其間DD":>8}{"CAGR":>7}'
        f'{"MDD":>8}{"Sharpe":>7}{"Calmar":>7}')


def show(name, dates, rets, note=""):
    m = metrics(rets, dates)
    L, a, b, dd, still = underwater(dates, rets)
    print(f'{name:<30}{L:>7.1f}年  {a}→{b}{dd:>8.1%}{m["cagr"]:>7.1%}'
          f'{m["mdd"]:>8.1%}{m["sharpe"]:>7.2f}{m["calmar"]:>7.2f}'
          f'  {"（仍在水下）" if still else ""}{note}')
    return m


def sub(dates, rets, lo, hi):
    idx = [j for j, d in enumerate(dates) if lo <= d < hi]
    a, b = idx[0], idx[-1]
    ds, rs = dates[a:b + 1], rets[a:b + 1]
    m = metrics(rs, ds)
    L = underwater(ds, rs)[0]
    return m, L


def main() -> int:
    bars, fs = load()
    S = Study()
    lab = Lab(bars, fs, 0.08)

    # 美股兩腿（只有 2016 後）
    us = {"UPRO": us_leg("gspc.csv", "UPRO.csv", "us_tuned"),
          "TQQQ": us_leg("ixic.csv", "TQQQ.csv", "nq_robust")}

    # 台指期各變體的 nav dict
    tx_nav, tx_lock = {}, {}
    for name, kw in TX_VARIANTS:
        nav, _, ent = S.run(**kw)
        tx_nav[name] = dict(zip(S.dates, nav))
        locked = set()
        for e in ent:
            z = e.exit_i if e.exit_i is not None else S.n - 1
            locked.update(S.dates[e.entry_i:z + 1])
        tx_lock[name] = locked

    # 共同交易日（以基準台指期 ∩ 兩條美股腿）
    common = sorted(set(tx_nav["最終套件（基準）"]) & set(us["UPRO"][0])
                    & set(us["TQQQ"][0]))
    common = [d for d in common if d >= W0]
    D = common[1:]
    yrs = (D[-1] - D[0]).days / 365.25
    print(f"視窗 {D[0]} ~ {D[-1]}（{len(D)} 個交易日，{yrs:.1f} 年）")
    print("⚠️ 此視窗幾乎全是多頭，且排除台指期 2012–2015 的空轉年；"
          "數字只能同視窗互比。\n")

    def rets_of(curve):
        return [curve[common[j]] / curve[common[j - 1]] - 1
                for j in range(1, len(common))]

    R_us = {n: rets_of(us[n][0]) for n in us}
    LOCK = {n: [d in us[n][1] for d in D] for n in us}

    print("【1】台指期單腿：§24 的各類變體在 2016 後的水下\n")
    print(HEAD)
    base_m = None
    tx_r = {}
    for name, _ in TX_VARIANTS:
        tx_r[name] = rets_of(tx_nav[name])
        m = show(name, D, tx_r[name])
        if base_m is None:
            base_m = m

    print("\n【2】跨市場組合（台指期腿＝最終套件；SIG 訊號再平衡）\n")
    print(HEAD)
    names = ["TX", "UPRO", "TQQQ"]
    R = {"TX": tx_r["最終套件（基準）"], **R_us}
    L_all = {"TX": [d in tx_lock["最終套件（基準）"] for d in D], **LOCK}
    for wlab, w in WEIGHTS:
        show(wlab, D, signal_rebalance(R, L_all, list(w), names))
    print("  （對照）每日再平衡：")
    for wlab, w in WEIGHTS[1:]:
        show("  " + wlab, D, daily_rebalance(R, list(w), names))

    print("\n【3】疊加：核心 1.0x 的台指期腿 ＋ 美股兩腿\n")
    print(HEAD)
    R1 = {"TX": tx_r["核心 1.0x"], **R_us}
    L1 = {"TX": [d in tx_lock["核心 1.0x"] for d in D], **LOCK}
    show("TX（核心 1.0x）100%", D, tx_r["核心 1.0x"])
    for wlab, w in WEIGHTS[1:]:
        show(wlab.replace("TX", "TX¹")
             .replace("等權 1/3", "等權 1/3（TX¹）"), D,
             signal_rebalance(R1, L1, list(w), names))
    print("  TX¹ = 核心 1.0x 的台指期腿")

    print("\n【4】視窗內前後段（2016–2020 / 2021–2026）\n")
    print(f'{"方案":<30}{"前段 水下/CAGR/Sh/Ca":<30}{"後段 水下/CAGR/Sh/Ca":<30}判定')
    cands = [(n, tx_r[n]) for n, _ in TX_VARIANTS]
    cands += [(w[0], signal_rebalance(R, L_all, list(w[1]), names)) for w in WEIGHTS[1:]]
    cands += [("TX¹ 50/25/25", signal_rebalance(R1, L1, [0.5, 0.25, 0.25], names))]
    bf = bg = None
    for n, r in cands:
        (mf, Lf), (mg, Lg) = sub(D, r, W0, SPLIT), sub(D, r, SPLIT, date(2027, 1, 1))
        if bf is None:
            bf, bg = mf, mg
            verdict = "—"
        else:
            up_f = mf["calmar"] - bf["calmar"] > 0.01
            up_g = mg["calmar"] - bg["calmar"] > 0.01
            dn_f = mf["calmar"] - bf["calmar"] < -0.01
            dn_g = mg["calmar"] - bg["calmar"] < -0.01
            verdict = ("✗ 前後段不一致" if (up_f and dn_g) or (dn_f and up_g)
                       else "✓ 兩段皆優" if up_f and up_g
                       else "✗ 兩段皆劣或一段劣" if dn_f or dn_g else "＝ 無差異")
        cf = f'{Lf:.1f}年 {mf["cagr"]:>6.1%} {mf["sharpe"]:.2f}/{mf["calmar"]:.2f}'
        cg = f'{Lg:.1f}年 {mg["cagr"]:>6.1%} {mg["sharpe"]:.2f}/{mg["calmar"]:.2f}'
        print(f'{n:<30}{cf:<30}{cg:<30}{verdict}')

    # ---- 截斷假高點的防呆：同一組方案換幾個視窗起點再量一次 ----
    print("\n【5】視窗起點敏感度（擋截斷假高點；任何結論都要在多個起點下成立）\n")
    starts = (date(2016, 1, 1), date(2016, 7, 1), date(2017, 1, 1), date(2018, 1, 1))
    print(f'{"方案":<30}' + "".join(f'{str(s):>20}' for s in starts))
    for n, r in cands:
        cells = []
        for s in starts:
            idx = [j for j, d in enumerate(D) if d >= s]
            ds, rs = D[idx[0]:], r[idx[0]:]
            m = metrics(rs, ds)
            cells.append(f'{underwater(ds, rs)[0]:.1f}年 / {m["calmar"]:.2f}')
        print(f'{n:<30}' + "".join(f'{c:>20}' for c in cells))
    print("\n  2016-01 起算會把 2016-03-18 當成高點，但它比全期高點（2012-03-02）"
          "低 4.7% —— 該欄的水下長度不可信，以其餘欄位為準。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
