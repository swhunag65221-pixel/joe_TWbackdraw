#!/usr/bin/env python3
"""台指期＋美股兩腿的組合最長水下期間（§25）。

    python3 scripts/underwater_mix.py

§24 的結論：台指期單腿的最長水下 7.5 年（2012-03 → 2019-11）是「那 8 年
只有 3 筆訊號可做」造成的，任何注碼／核心／訊號頻率的變體都無法縮短它。
唯一沒測過的方向是**報酬來源錯開**：加入低相關的美股腿。

⚠️ 決定性的限制：FinLab 的美股資料只到 2015-07（指數）／2016-01（ETF），
而問題中的水下段是 2012-03 → 2019-11 —— **無法直接檢驗分散能不能縮短它**。
本腳本能回答的是「在有資料的 2016–2026 視窗裡，分散是否縮短水下」，
以及該視窗對台指期有多有利（§22 已量化：Calmar 膨脹 ×2.7）。

腿與再平衡邏輯沿用 `scripts/lab_mdd/A2_diversify.py`（已與 docs/allocation.md 交叉驗證），
台指期腿換成最終套件（混合注碼＋核心 0.5x ±2%＋step-down）。
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
                          tx_leg, us_leg)
from A_giveback import Lab, load                                       # noqa: E402
from tw_backdraw.futures import hybrid_entries                         # noqa: E402

#: 預先宣告的固定權重（禁止最佳化，理由見 docs/allocation.md §二）
WEIGHTS = (("TX 100（對照）", (1.0, 0.0, 0.0)),
           ("TX 50 / UPRO 25 / TQQQ 25", (0.50, 0.25, 0.25)),
           ("TX 40 / UPRO 30 / TQQQ 30", (0.40, 0.30, 0.30)),
           ("等權 1/3", (1 / 3, 1 / 3, 1 / 3)),
           ("US 50/50（對照）", (0.0, 0.5, 0.5)))


def underwater(dates, rets):
    """由日報酬序列算 (最長水下年數, 起日, 收復日, 其間最深回撤, 是否仍在水下)。"""
    eq, pk, pkk, best = 1.0, 1.0, 0, (0, 0, 0, 0.0)
    cur = 0.0
    path = [1.0]
    for x in rets:
        eq *= 1 + x
        path.append(eq)
    for k, v in enumerate(path):
        if v >= pk:
            if k - pkk > best[0]:
                best = (k - pkk, pkk, k, cur)
            pk, pkk, cur = v, k, 0.0
        else:
            cur = min(cur, v / pk - 1)
    open_len = len(path) - 1 - pkk
    still = open_len > best[0]
    if still:
        best = (open_len, pkk, len(path) - 1, cur)
    d = [dates[0]] + list(dates)
    return best[0] / 252, d[best[1]], d[best[2]], best[3], still


def main() -> int:
    bars, fs = load()
    lab = Lab(bars, fs, 0.08)
    ent = hybrid_entries(lab.base, bars, 200, 3.0, 0.5)
    legs = {}
    legs["TX"] = tx_leg(lab, ent, core=0.5, step=(1.5, 1.0))
    legs["UPRO"] = us_leg("gspc.csv", "UPRO.csv", "us_tuned")
    legs["TQQQ"] = us_leg("ixic.csv", "TQQQ.csv", "nq_robust")

    names = ["TX", "UPRO", "TQQQ"]
    common = sorted(set.intersection(*(set(legs[n][0]) for n in names)))
    D = common[1:]
    R = {n: [legs[n][0][common[j]] / legs[n][0][common[j - 1]] - 1
             for j in range(1, len(common))] for n in names}
    LOCK = {n: [d in legs[n][1] for d in D] for n in names}
    print(f"重疊視窗 {D[0]} ~ {D[-1]}（{len(D)} 個交易日，"
          f"{(D[-1] - D[0]).days / 365.25:.1f} 年）")
    print("⚠️ §24 的水下段是 2012-03 → 2019-11；美股資料 2016 才開始，"
          "本視窗只涵蓋它的尾段。\n")

    print("【1】各腿單獨（同視窗）\n")
    hdr = f'{"腿／配置":<28}{"最長水下":>8}  {"起→收復":<24}{"其間DD":>8}{"CAGR":>7}{"MDD":>8}{"Sharpe":>7}{"Calmar":>7}'
    print(hdr)
    for n in names:
        m = metrics(R[n], D)
        L, a, b, dd, still = underwater(D, R[n])
        tag = "（仍在水下）" if still else ""
        print(f'{n:<28}{L:>7.1f}年  {a}→{b}{dd:>8.1%}{m["cagr"]:>7.1%}'
              f'{m["mdd"]:>8.1%}{m["sharpe"]:>7.2f}{m["calmar"]:>7.2f}  {tag}')

    for label, eng in (("【2】每日再平衡（上界）", daily_rebalance),
                       ("【3】訊號再平衡 SIG（實務可達成）",
                        lambda R_, w, N: signal_rebalance(R_, LOCK, w, N))):
        print(f"\n{label}\n")
        print(hdr)
        for wlab, w in WEIGHTS:
            r = eng(R, list(w), names)
            m = metrics(r, D)
            L, a, b, dd, still = underwater(D, r)
            tag = "（仍在水下）" if still else ""
            print(f'{wlab:<28}{L:>7.1f}年  {a}→{b}{dd:>8.1%}{m["cagr"]:>7.1%}'
                  f'{m["mdd"]:>8.1%}{m["sharpe"]:>7.2f}{m["calmar"]:>7.2f}  {tag}')

    # 水下期間的重疊：三腿同時在水下的天數
    print("\n【4】水下的重疊程度（各腿逐日是否低於自己的歷史高點）\n")
    uw = {}
    for n in names:
        eq, pk, flag = 1.0, 1.0, []
        for x in R[n]:
            eq *= 1 + x
            pk = max(pk, eq)
            flag.append(eq < pk * (1 - 1e-12))
        uw[n] = flag
    tot = len(D)
    for n in names:
        print(f"  {n:<6}水下 {sum(uw[n]) / tot:.0%} 的交易日")
    all3 = sum(1 for j in range(tot) if all(uw[n][j] for n in names))
    tx_only = sum(1 for j in range(tot) if uw["TX"][j] and not (uw["UPRO"][j] and uw["TQQQ"][j]))
    print(f"  三腿同時水下 {all3 / tot:.0%}　"
          f"台指期水下但至少一條美股腿在創高 {tx_only / tot:.0%}")
    sig = signal_rebalance(R, LOCK, [0.5, 0.25, 0.25], names)
    eq, pk, cnt = 1.0, 1.0, 0
    for x in sig:
        eq *= 1 + x
        pk = max(pk, eq)
        cnt += eq < pk * (1 - 1e-12)
    print(f"  TX 50/25/25 組合水下 {cnt / tot:.0%} 的交易日")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
