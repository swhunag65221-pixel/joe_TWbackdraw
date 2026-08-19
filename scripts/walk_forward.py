#!/usr/bin/env python3
"""樣本外驗證：把 grid search 選出來的參數拿到另一段期間去跑。

    python3 scripts/walk_forward.py --split 2013-01-01

做法很簡單，也很殘忍：
    1. 只用前半段資料跑 grid search，挑出勝率最高的 K 組參數
    2. 把這 K 組拿到後半段（完全沒看過的資料）跑一次
    3. 跟預設參數在後半段的表現比較

如果「前半段最佳」在後半段沒有優勢，就代表 grid search 挑到的是雜訊。
以台股這種個位數訊號的樣本量，這個結果幾乎是可以預期的 —— 本工具的用途
是把「可以預期」變成「看得到的數字」。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import load_csv                                       # noqa: E402
from tw_backdraw.backtest import run_backtest                          # noqa: E402
from tw_backdraw.config import DEFAULT_CONFIG                          # noqa: E402
from grid_search import GRID, build_config, combos                     # noqa: E402

_BARS = None


def _init(bars):
    global _BARS
    _BARS = bars


def _eval(p: dict) -> tuple:
    try:
        _, st = run_backtest(_BARS, build_config(p))
    except Exception:
        return (-1.0, -1.0, 0, p)
    return (st.win_rate, st.total_return, st.n_trades, p)


def run_all(bars, todo):
    with mp.Pool(mp.cpu_count(), initializer=_init, initargs=(bars,)) as pool:
        return list(pool.imap_unordered(_eval, todo, chunksize=256))


def summarize(label, rows):
    wins = [r[0] for r in rows]
    tots = [r[1] for r in rows]
    print(f"  {label:<28}勝率 中位數 {statistics.median(wins):>5.0%}　"
          f"平均 {statistics.fmean(wins):>5.0%}　｜　總報酬 中位數 {statistics.median(tots):>7.1%}　"
          f"平均 {statistics.fmean(tots):>7.1%}")


def main() -> int:
    ap = argparse.ArgumentParser(description="樣本外驗證")
    ap.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    ap.add_argument("--split", default="2013-01-01", help="切分日期 YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=50, help="從訓練段挑幾組進入樣本外測試")
    ap.add_argument("--min-trades", type=int, default=5)
    args = ap.parse_args()

    bars = load_csv(args.csv)
    train = [b for b in bars if b.iso < args.split]
    test = [b for b in bars if b.iso >= args.split]
    print(f"訓練段 {train[0].d} ~ {train[-1].d}（{len(train)} 根）")
    print(f"測試段 {test[0].d} ~ {test[-1].d}（{len(test)} 根）\n")

    todo = list(combos(GRID))
    print(f"訓練段掃描 {len(todo):,} 組 …")
    tr = run_all(train, todo)
    eligible = [r for r in tr if r[2] >= args.min_trades]
    if not eligible:
        print("訓練段沒有任何組合達到最低交易次數門檻")
        return 1
    picked = sorted(eligible, key=lambda r: (-r[0], -r[1]))[:args.top]
    print(f"  達門檻 {len(eligible):,} 組，取前 {len(picked)} 組")
    print(f"  訓練段前 {len(picked)} 組：勝率 {picked[0][0]:.0%}~{picked[-1][0]:.0%}，"
          f"平均總報酬 {statistics.fmean(r[1] for r in picked):.1%}\n")

    print(f"測試段驗證 …")
    te_picked = run_all(test, [r[3] for r in picked])
    te_all = run_all(test, todo)

    print("\n【測試段表現】")
    summarize("訓練段挑出的前段班", te_picked)
    summarize("測試段全網格", te_all)
    _, st = run_backtest(test, DEFAULT_CONFIG)
    print(f"  {'預設參數':<28}勝率 {st.win_rate:>10.0%}　　　　｜　總報酬 {st.total_return:>16.1%}"
          f"（{st.n_trades} 筆）")

    win_lift = statistics.fmean(r[0] for r in te_picked) - statistics.fmean(r[0] for r in te_all)
    ret_lift = statistics.fmean(r[1] for r in te_picked) - statistics.fmean(r[1] for r in te_all)
    print(f"\n  前段班相對全網格的樣本外優勢：勝率 {win_lift:+.1%}　總報酬 {ret_lift:+.1%}")
    print("  （接近 0 或為負，代表 grid search 選到的是雜訊而非訊號）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
