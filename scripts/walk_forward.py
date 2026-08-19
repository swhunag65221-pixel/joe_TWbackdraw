#!/usr/bin/env python3
"""樣本外驗證：用前半段選參數，拿到後半段驗收。

    python3 scripts/walk_forward.py --split 2013-01-01

前後兩段各掃一次完整網格，然後用不同的「選擇標準」各挑前 K 組，
比較它們在測試段的表現。同時輸出訓練段與測試段各項指標的相關係數 ——
那是「參數可不可以外推」最直接的量化。

以台股個位數到二十幾筆的樣本量，這個測試的統計檢定力很低。它的用途是
把「調參到底有沒有用」變成看得到的數字，不是拿來下結論。
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
from tw_backdraw.config import DEFAULT_CONFIG, POST_CONFIG                          # noqa: E402
from grid_search import (                                              # noqa: E402
    GRID, add_market_args, base_from_args, build_config, combos,
)

_BARS = None

# 選擇標準：名稱 → (排序鍵, 說明)
CRITERIA = {
    "win": (lambda m: (m["win"], m["total"]), "勝率（次要：總報酬）"),
    "total": (lambda m: (m["total"],), "總報酬"),
    "calmar": (lambda m: (m["total"] / abs(m["mdd"]) if m["mdd"] else 0,), "總報酬 ÷ 最大回檔"),
    "avg": (lambda m: (m["avg"],), "單筆平均報酬"),
}


_BASE = None


def _init(bars, base=None):
    global _BARS, _BASE
    _BARS, _BASE = bars, base


def _key(p: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in p.items()))


def _eval(p: dict) -> tuple:
    try:
        _, st = run_backtest(_BARS, build_config(p, _BASE))
        m = dict(win=st.win_rate, total=st.total_return, avg=st.avg_return,
                 mdd=st.max_drawdown, n=st.n_trades)
    except Exception:
        m = dict(win=0.0, total=-1.0, avg=-1.0, mdd=-1.0, n=0)
    return _key(p), m


def scan(bars, todo, base=None) -> dict:
    with mp.Pool(mp.cpu_count(), initializer=_init, initargs=(bars, base)) as pool:
        return dict(pool.imap_unordered(_eval, todo, chunksize=256))


def replace_market(cfg, base):
    """把 preset 的槓桿與成本換成本次市場設定，其餘保持不變。"""
    from dataclasses import replace
    return replace(cfg, sizing=base.sizing, cost=base.cost)


def corr(xs, ys) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs) / (sx * sy)


def main() -> int:
    ap = argparse.ArgumentParser(description="樣本外驗證")
    ap.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    ap.add_argument("--split", default="2013-01-01")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--min-trades", type=int, default=5)
    add_market_args(ap)
    args = ap.parse_args()
    base = base_from_args(args)

    bars = load_csv(args.csv)
    train = [b for b in bars if b.iso < args.split]
    test = [b for b in bars if b.iso >= args.split]
    print(f"訓練段 {train[0].d} ~ {train[-1].d}（{len(train)} 根）")
    print(f"測試段 {test[0].d} ~ {test[-1].d}（{len(test)} 根）")

    todo = list(combos(GRID))
    print(f"\n掃描訓練段 {len(todo):,} 組 …")
    tr = scan(train, todo, base)
    print("掃描測試段 …")
    te = scan(test, todo, base)

    shared = [k for k in tr if k in te]
    ok = [k for k in shared if tr[k]["n"] >= args.min_trades]
    print(f"\n訓練段達 {args.min_trades} 筆門檻：{len(ok):,} / {len(shared):,} 組")

    base_win = statistics.fmean(te[k]["win"] for k in shared)
    base_tot = statistics.fmean(te[k]["total"] for k in shared)
    print(f"測試段全網格基準：平均勝率 {base_win:.1%}　平均總報酬 {base_tot:.1%}\n")

    print(f"{'訓練段選擇標準':<24}{'測試段勝率':>12}{'vs 基準':>10}{'測試段總報酬':>14}{'vs 基準':>10}")
    print("-" * 72)
    for name, (keyf, desc) in CRITERIA.items():
        picked = sorted(ok, key=lambda k: keyf(tr[k]), reverse=True)[:args.top]
        w = statistics.fmean(te[k]["win"] for k in picked)
        t = statistics.fmean(te[k]["total"] for k in picked)
        print(f"{desc:<24}{w:>12.1%}{w - base_win:>+10.1%}{t:>14.1%}{t - base_tot:>+10.1%}")

    for label, cfg, note in (
        ("專案預設 tuned", DEFAULT_CONFIG, "⚠ 樣本內"),
        ("貼文原意 post", POST_CONFIG, ""),
    ):
        _, st = run_backtest(test, replace_market(cfg, base))
        print(f"{label:<24}{st.win_rate:>12.1%}{st.win_rate - base_win:>+10.1%}"
              f"{st.total_return:>14.1%}{st.total_return - base_tot:>+10.1%}  {note}")
    print("\n  ⚠ tuned 是用「含測試段」的完整資料選出來的，它在測試段的數字不是樣本外結果，")
    print("    只能當作參考。真正的樣本外證據是上面四種選擇標準那幾列。")

    print("\n【參數可外推性】訓練段指標 vs 測試段指標的相關係數")
    for m in ("win", "total", "avg", "mdd"):
        c = corr([tr[k][m] for k in shared], [te[k][m] for k in shared])
        print(f"  {m:<8}{c:>+7.3f}")
    print("  （越接近 0，代表在訓練段調出來的優勢越無法帶到測試段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
