#!/usr/bin/env python3
"""注碼規則層的 walk-forward：逐筆錨定，只用「這筆之前」的資料選規則。

    python3 scripts/walk_forward_sizing.py

§20 的混合注碼是在完整 21 筆上設計的（第三代後見之明）。本腳本檢驗它
過不過得了「當時的你選得到嗎」這一關：

1. **逐筆錨定選擇**：候選規則固定一張選單。第 k 筆訊號出現時，
   只用第 1..k−1 筆期間的淨值算每個規則的目標函數，選最好的用在第 k 筆。
   串起來的複合淨值是真正「當時做得到」的樣本外結果。
2. **多切點排名轉移**：在幾個切點上比較「訓練段排名」與「測試段排名」
   是否一致 —— 規則優勢能不能外推的直接量化。

⚠️ 誠實的邊界：候選**選單本身**（濾網、3x、半預算）是看完整資料才發明的。
本測試能回答「選擇是否穩定、優勢是否外推」，不能回答「當年能不能發明它」。
後者在回溯資料上無法檢驗。
"""

from __future__ import annotations

import math
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from defensive_study import MA, Book                                # noqa: E402
from hybrid_study import BELOW_LEV, hybrid, split_by_ma             # noqa: E402
from tx_data import login                                           # noqa: E402
from tw_backdraw.futures import (FuturesCost, fixed_leverage,       # noqa: E402
                                 vehicle_series)

SPLITS = ("2008-01-01", "2012-01-01", "2016-01-01", "2020-01-01")
BURN_INS = (8, 10, 12)          # 錨定選擇前至少要看過幾筆


def rules(bk: Book) -> dict[str, list]:
    """候選規則選單（固定，不因期間而變）。"""
    return {
        "現行 風險式≤5x": bk.base,
        "濾網＋固定3x": fixed_leverage(bk.filtered, BELOW_LEV),
        "混合 全預算≤5x": hybrid(bk, 5.0),
        "混合 半預算": hybrid(bk, 5.0, 0.5),
        "全部固定3x": fixed_leverage(bk.base, BELOW_LEV),
    }


def navs(bk: Book, menu: dict) -> dict[str, list[float]]:
    return {nm: vehicle_series(bk.dates, bk.fs, es, FuturesCost(), bk.ic)[0]
            for nm, es in menu.items()}


def seg_metrics(nav: list[float], a: int, b: int) -> dict:
    """nav[a..b]（含）的總報酬、MDD、Calmar、Sharpe。"""
    seg = nav[a:b + 1]
    base = seg[0]
    total = seg[-1] / base - 1.0
    peak, dd = 0.0, 0.0
    for x in seg:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1.0) if peak else dd
    r = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
    sharpe = (st.fmean(r) / st.pstdev(r) * math.sqrt(252)
              if len(r) > 1 and st.pstdev(r) else 0.0)
    return dict(total=total, mdd=dd,
                calmar=total / abs(dd) if dd else 0.0, sharpe=sharpe)


OBJECTIVES = {"calmar": lambda m: m["calmar"],       # 專案預設目標的規則層版本
              "sharpe": lambda m: m["sharpe"],
              "total": lambda m: m["total"]}


def anchored(bk: Book, menu: dict, nv: dict, obj: str, burn: int):
    """逐筆錨定：第 k 筆用 [0, entry_i−1] 的資料選規則。

    回傳 (複合淨值比較用的逐段成長, 每筆選到的規則, OOS 起點索引)。
    段 k = [第 k 筆 entry_i 前一天, 第 k+1 筆 entry_i 前一天]，
    淨值在空手期持平，所以這樣切每段恰好涵蓋第 k 筆的完整損益。
    """
    entries = sorted(bk.base, key=lambda e: e.entry_i)
    keyf = OBJECTIVES[obj]
    picks, growth = [], []
    start_i = entries[burn].entry_i - 1
    for k in range(burn, len(entries)):
        e_i = entries[k].entry_i
        nxt = (entries[k + 1].entry_i - 1 if k + 1 < len(entries)
               else len(bk.dates) - 1)
        best = max(menu, key=lambda nm: keyf(seg_metrics(nv[nm], 0, e_i - 1)))
        picks.append((bk.dates[e_i], best))
        growth.append(nv[best][nxt] / nv[best][e_i - 1])
    return growth, picks, start_i


def composite_metrics(bk: Book, nv_ref: dict, growth: list[float],
                      picks, start_i: int, entries) -> dict:
    """把逐段成長攤回日線（段內沿用被選規則的日淨值），算 OOS 指標。"""
    daily = [1.0]
    idx = start_i
    acc = 1.0
    for k, ((_, nm), g) in enumerate(zip(picks, growth)):
        e_i = entries[k].entry_i
        nxt = (entries[k + 1].entry_i - 1 if k + 1 < len(entries)
               else len(bk.dates) - 1)
        base = nv_ref[nm][e_i - 1]
        for i in range(e_i, nxt + 1):
            daily.append(acc * nv_ref[nm][i] / base)
        acc *= g
        idx = nxt
    return seg_metrics(daily, 0, len(daily) - 1)


def main() -> int:
    login()
    bk = Book()
    menu = rules(bk)
    nv = navs(bk, menu)
    entries = sorted(bk.base, key=lambda e: e.entry_i)
    print(f"期間 {bk.dates[0]} ~ {bk.dates[-1]}　訊號 {len(entries)} 筆　"
          f"候選規則 {len(menu)} 個\n")

    # ---- 1. 多切點：規則排名能不能外推 ----
    print("【1】多切點排名轉移（訓練段選第一名 → 它在測試段排第幾）\n")
    names = list(menu)
    for obj in ("calmar", "sharpe"):
        print(f"  目標函數：{obj}")
        hdr = f'  {"切點":<12}{"訓練段第一名":<16}{"測試段排名":>10}   測試段第一名'
        print(hdr)
        for s in SPLITS:
            cut = date.fromisoformat(s)
            ci = next(i for i, d in enumerate(bk.dates) if d >= cut)
            trm = {nm: seg_metrics(nv[nm], 0, ci - 1) for nm in names}
            tem = {nm: seg_metrics(nv[nm], ci, len(bk.dates) - 1) for nm in names}
            keyf = OBJECTIVES[obj]
            tr_rank = sorted(names, key=lambda nm: keyf(trm[nm]), reverse=True)
            te_rank = sorted(names, key=lambda nm: keyf(tem[nm]), reverse=True)
            pos = te_rank.index(tr_rank[0]) + 1
            print(f'  {s:<12}{tr_rank[0]:<16}{pos:>7}/{len(names)}   {te_rank[0]}')
        print()

    # ---- 2. 逐筆錨定複合 ----
    print("【2】逐筆錨定：每筆只用之前的資料選規則，串成樣本外複合淨值\n")
    for obj in ("calmar", "sharpe"):
        for burn in BURN_INS:
            growth, picks, start_i = anchored(bk, menu, nv, obj, burn)
            comp = composite_metrics(bk, nv, growth, picks, start_i, entries[burn:])
            oos_a = entries[burn].entry_i - 1
            oos = {nm: seg_metrics(nv[nm], oos_a, len(bk.dates) - 1)
                   for nm in menu}
            print(f"  目標 {obj}／燒入 {burn} 筆　OOS {bk.dates[oos_a]} 起"
                  f"（{len(growth)} 筆）")
            switch = sum(1 for i in range(1, len(picks))
                         if picks[i][1] != picks[i - 1][1])
            print(f"    選擇路徑：{picks[0][1]} 起，切換 {switch} 次　"
                  f"最後選 {picks[-1][1]}")
            uniq = []
            for _, nm in picks:
                if not uniq or uniq[-1] != nm:
                    uniq.append(nm)
            print("    路徑：" + " → ".join(uniq))
            fmt = lambda m: (f"總報酬 {m['total']:+8.1%}  MDD {m['mdd']:6.1%}  "
                             f"Sharpe {m['sharpe']:4.2f}  Calmar {m['calmar']:4.2f}")
            print(f"    複合（真樣本外）  {fmt(comp)}")
            for nm in menu:
                print(f"    {nm:<14}  {fmt(oos[nm])}")
            print()
    print("  讀法：複合 ≈ 事後最佳規則 → 選擇可以外推；複合明顯落後 → 規則優勢")
    print("  是後見之明。切換次數多代表訓練段的排名不穩定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
