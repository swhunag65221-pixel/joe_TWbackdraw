#!/usr/bin/env python3
"""混合版：依進場日與 MA200 的相對位置，切換槓桿規則。

    python3 scripts/hybrid_study.py

§18 的結論是「逆勢 MA200 濾網＋固定 3x」全面優於現行，但它把 21 筆訊號
砍到 9 筆 —— MA200 之上的 12 筆整批丟掉，其中有 2005、2009-07、2026 的贏家。
§14/§18 同時顯示：均線之上藏著三大虧損（−18.9%／−18.7%／−17.8%），
沒有濾網的固定槓桿是災難，而風險式槓桿正是讓那些交易活下來的機制。

整合構想因此不是「要不要濾網」，而是**按環境切換注碼法**：

    進場日收盤 < MA200 → 固定 3x（§18：深回檔是最好的機會，別壓小注）
    進場日收盤 ≥ MA200 → 風險式 8% ÷ 停損距離，上限另設（≤現行的 5x）

MA200 之上的上限只會**往下**收（cap 不會把槓桿加上去），所以混合版對
均線之上任何一筆的單筆風險，都不高於現行版本。上限取 5/4/3/2 的小網格，
全部列出，不挑格子。

⚠️ 與 §18 相同的保留：切換規則是看著同一批 21 筆的結果設計的（後見之明），
前後段方向一致是必要條件，不是樣本外驗證。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from defensive_study import CORE, HEAD, MA, Book, row               # noqa: E402
from tx_data import login                                           # noqa: E402
from tw_backdraw.futures import (FuturesCost, FuturesEntry,         # noqa: E402
                                 fixed_leverage, vehicle_series)

BELOW_LEV = 3.0                 # §18 選定：唯一零筆超出風險預算、爆倉緩衝三倍
ABOVE_CAPS = (5.0, 4.0, 3.0, 2.0)
PERIODS = (("全期 1999–2026", date(1999, 1, 1), date(2027, 1, 1)),
           ("前段 1999–2012", date(1999, 1, 1), date(2013, 1, 1)),
           ("後段 2013–2026", date(2013, 1, 1), date(2027, 1, 1)))


def split_by_ma(bk: Book) -> tuple[list, list]:
    """把訊號分成（低於均線, 其餘）。均線資料不足的一律歸「其餘」——
    沒有濾網資訊時維持現行做法，是保守的選擇。"""
    below, above = [], []
    for e in bk.base:
        m = bk.ma[e.entry_i]
        (below if m is not None and bk.ic[e.entry_i] < m else above).append(e)
    return below, above


def hybrid(bk: Book, above_cap: float,
           above_scale: float = 1.0) -> list[FuturesEntry]:
    """低於均線 → 固定 3x；其餘 → 風險式槓桿 × above_scale，再以 above_cap 封頂。

    above_scale=0.5 等於把均線之上的風險預算從 8% 砍半到 4% ——
    「反環境的訊號只給一半預算」，是規則不是挑出來的數字。
    """
    below, above = split_by_ma(bk)
    out = list(fixed_leverage(below, BELOW_LEV))
    out += [FuturesEntry(e.entry_i, e.exit_i,
                         min(e.leverage * above_scale, above_cap),
                         e.entry_index, e.stop_distance, e.trade)
            for e in above]
    return sorted(out, key=lambda e: e.entry_i)


def main() -> int:
    login()
    bk = Book()
    below, above = split_by_ma(bk)
    print(f"期間 {bk.dates[0]} ~ {bk.dates[-1]}　訊號 {len(bk.base)} 筆"
          f"　= 低於 MA{MA} {len(below)} 筆 ＋ 其餘 {len(above)} 筆\n")

    print("【1】全期：現行、§18 濾網版、混合版\n")
    print(HEAD)
    print(row("現行（風險式 ≤5x）", bk.measure(bk.base)))
    print(row(f"濾網 ＋ 固定 {BELOW_LEV:g}x（§18）",
              bk.measure(fixed_leverage(bk.filtered, BELOW_LEV))))
    for cap in ABOVE_CAPS:
        print(row(f"混合：下3x／上風險式≤{cap:g}x", bk.measure(hybrid(bk, cap))))
    print(row("混合：下3x／上半預算(4%)", bk.measure(hybrid(bk, 5.0, 0.5))))
    print("  對照（上檔也用固定槓桿 —— §18 已證明的災難路線）：")
    print("  " + row(f"全部固定 {BELOW_LEV:g}x", bk.measure(
        fixed_leverage(bk.base, BELOW_LEV))))

    print(f"\n【2】再加空手期核心 {CORE:g}x（收盤 > MA{MA} 才持有）\n")
    print(HEAD)
    print(row("§19 濾網+3x+核心", bk.measure(
        fixed_leverage(bk.filtered, BELOW_LEV), core=CORE)))
    for cap in ABOVE_CAPS:
        print(row(f"混合≤{cap:g}x ＋ 核心", bk.measure(hybrid(bk, cap), core=CORE)))
    print(row("混合半預算 ＋ 核心", bk.measure(hybrid(bk, 5.0, 0.5), core=CORE)))

    print("\n【3】前後段檢驗\n")
    for lab, lo, hi in PERIODS:
        print(f"  {lab}")
        print("  " + HEAD)
        print("  " + row("現行", bk.measure(bk.base, lo, hi)))
        print("  " + row("濾網＋3x", bk.measure(
            fixed_leverage(bk.filtered, BELOW_LEV), lo, hi)))
        for cap in ABOVE_CAPS:
            print("  " + row(f"混合≤{cap:g}x", bk.measure(hybrid(bk, cap), lo, hi)))
        print("  " + row("混合半預算", bk.measure(hybrid(bk, 5.0, 0.5), lo, hi)))
        print("  " + row("混合≤3x＋核心", bk.measure(hybrid(bk, 3.0), lo, hi,
                                                    core=CORE)))
        print("  " + row("混合半預算＋核心", bk.measure(hybrid(bk, 5.0, 0.5), lo, hi,
                                                      core=CORE)))
        print()

    print(f"【4】MA{MA} 之上的 12 筆在各上限下的權益報酬（下檔 9 筆一律 3x，略）\n")
    per = {}
    for cap in ABOVE_CAPS:
        _, det = vehicle_series(bk.dates, bk.fs, hybrid(bk, cap),
                                FuturesCost(), bk.ic)
        per[cap] = {t.entry_date: t for t in det}
    dates_above = sorted(bk.dates[e.entry_i] for e in above)
    print(f'{"進場":<12}{"停損距離":>9}{"風險式槓桿":>11}'
          + "".join(f"{f'≤{c:g}x':>9}" for c in ABOVE_CAPS))
    for d in dates_above:
        e = next(x for x in above if bk.dates[x.entry_i] == d)
        print(f"{d!s:<12}{e.stop_distance:>9.2%}{e.leverage:>11.2f}"
              + "".join(f"{per[c][d].ret:>9.1%}" for c in ABOVE_CAPS))

    worst_above = min(t.futures_return for t in per[ABOVE_CAPS[0]].values()
                      if t.entry_date in set(dates_above))
    print(f"\n上檔 12 筆最差期貨報酬 {worst_above:.1%}"
          f"（上限 cap 只會降槓桿，單筆風險恆 ≤ 現行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
