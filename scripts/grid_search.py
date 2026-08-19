#!/usr/bin/env python3
"""全參數 grid search。

    python3 scripts/grid_search.py --out /tmp/grid.json
    python3 scripts/grid_search.py --quick          # 小網格，先驗證流程

⚠️ 台股全期只有個位數的訊號次數。勝率在 8 筆交易上只有 9 種可能取值
（0/8…8/8），單一筆交易翻面就會讓勝率跳動 12.5 個百分點。
因此本工具的輸出**不應該**被當成「最佳參數」，而是用來看：
  1. 勝率對每個參數的敏感度（邊際分析）——哪些參數其實沒差
  2. 高勝率的組合是不是靠「把交易次數壓到剩兩三筆」換來的
  3. 預設值在整個分布裡的位置
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import load_csv                                      # noqa: E402
from tw_backdraw.backtest import align_etf, run_backtest              # noqa: E402
from tw_backdraw.config import (                                      # noqa: E402
    DEFAULT_CONFIG, EntryConfig, ExitConfig, LevelConfig, SetupConfig,
)

# ---- 搜尋空間 ---------------------------------------------------------------
# (exit_mode, ma_period, trail_drawdown) 綁在一起，避免產生無意義的組合
EXIT_STYLES = (
    [("trail", 20, t) for t in (0.06, 0.08, 0.10, 0.12)]
    + [("ma_ratchet", p, 0.08) for p in (20, 40, 60)]
    + [("both", p, 0.08) for p in (20, 40, 60)]
)

GRID = {
    "min_drawdown": (0.07, 0.10, 0.13, 0.16),
    "repair_fraction": (0.60, 0.70, 0.75, 0.80, 0.85),
    "max_repair_bars": (10, 15, 20, 25, 30),
    "warn_line_ratio": (0.236, 0.382, 0.500),
    "base_weight": (0.4, 0.7, 1.0),
    "fill_timeout_bars": (10, 20, 40),
    "breakout_fills_remainder": (True, False),
    "exit_style": tuple(EXIT_STYLES),
    "warn_derisk_fraction": (0.0, 0.5, 1.0),
    "hard_stop_at_trough": (True, False),
    "target_take_fraction": (0.0, 1 / 3),
}

QUICK = {
    "min_drawdown": (0.10,),
    "repair_fraction": (0.70, 0.75),
    "max_repair_bars": (15, 25),
    "warn_line_ratio": (0.382,),
    "base_weight": (0.4, 1.0),
    "fill_timeout_bars": (20,),
    "breakout_fills_remainder": (True,),
    "exit_style": (("trail", 20, 0.08), ("ma_ratchet", 40, 0.08)),
    "warn_derisk_fraction": (0.5,),
    "hard_stop_at_trough": (True,),
    "target_take_fraction": (0.0,),
}

_BARS = None
_ETF = None


def _init(bars, etf):
    global _BARS, _ETF
    _BARS, _ETF = bars, etf


def build_config(p: dict):
    d = DEFAULT_CONFIG
    mode, ma, trail = p["exit_style"]
    return replace(
        d,
        setup=SetupConfig(min_drawdown=p["min_drawdown"],
                          repair_fraction=p["repair_fraction"],
                          max_repair_bars=p["max_repair_bars"],
                          setup_expiry_bars=d.setup.setup_expiry_bars,
                          reanchor_on_expiry=True),
        levels=LevelConfig(half_line_ratio=d.levels.half_line_ratio,
                           warn_line_ratio=p["warn_line_ratio"],
                           half_line_buffer=d.levels.half_line_buffer),
        entry=EntryConfig(base_weight=p["base_weight"],
                          pullback_ladder=d.entry.pullback_ladder,
                          fill_timeout_bars=p["fill_timeout_bars"],
                          breakout_fills_remainder=p["breakout_fills_remainder"]),
        exit=ExitConfig(warn_derisk_fraction=p["warn_derisk_fraction"],
                        hard_stop_at_trough=p["hard_stop_at_trough"],
                        target_take_fraction=p["target_take_fraction"],
                        exit_mode=mode, ma_period=ma, trail_drawdown=trail),
    )


def evaluate(p: dict) -> dict:
    try:
        _, st = run_backtest(_BARS, build_config(p), _ETF)
    except Exception:
        return {}
    rec = dict(p)
    rec["exit_style"] = "/".join(str(x) for x in p["exit_style"])
    rec.update(n=st.n_trades, win=st.win_rate, hit=st.hit_rate, avg=st.avg_return,
               total=st.total_return, mdd=st.max_drawdown)
    return rec


def combos(grid: dict):
    keys = list(grid)
    for vals in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, vals))


def marginal(rows: list[dict], key: str) -> list[tuple]:
    """把某個參數的每個取值，對應到它在所有其他組合下的平均表現。"""
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(r[key], []).append(r)
    out = []
    for val, rs in buckets.items():
        out.append((val, len(rs),
                    statistics.fmean(r["win"] for r in rs),
                    statistics.fmean(r["total"] for r in rs),
                    statistics.fmean(r["n"] for r in rs)))
    return sorted(out, key=lambda x: -x[2])


def main() -> int:
    ap = argparse.ArgumentParser(description="策略參數 grid search")
    ap.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    ap.add_argument("--etf-csv", help="用真實 ETF 價格（回測期間會縮到重疊區間）")
    ap.add_argument("--quick", action="store_true", help="小網格，驗證流程用")
    ap.add_argument("--min-trades", type=int, default=8,
                    help="排行榜的最低交易次數門檻，避免用少樣本換高勝率")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out", help="把完整結果寫成 JSON")
    args = ap.parse_args()

    bars = load_csv(args.csv)
    etf = None
    if args.etf_csv:
        bars, etf = align_etf(bars, load_csv(args.etf_csv))

    grid = QUICK if args.quick else GRID
    todo = list(combos(grid))
    print(f"資料 {bars[0].d} ~ {bars[-1].d}（{len(bars)} 根）")
    print(f"參數組合 {len(todo):,} 組，{mp.cpu_count()} 核心\n")

    t0 = time.time()
    with mp.Pool(mp.cpu_count(), initializer=_init, initargs=(bars, etf)) as pool:
        rows = [r for r in pool.imap_unordered(evaluate, todo, chunksize=256) if r]
    print(f"完成，耗時 {time.time() - t0:.0f} 秒\n")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"完整結果 → {args.out}\n")

    # ---- 排行榜 ----
    for label, pool_rows in (("全部組合（含低樣本）", rows),
                             (f"交易次數 ≥ {args.min_trades}", [r for r in rows if r["n"] >= args.min_trades])):
        if not pool_rows:
            print(f"【{label}】無符合條件的組合\n")
            continue
        best = sorted(pool_rows, key=lambda r: (-r["win"], -r["total"]))[:args.top]
        print(f"【{label}】共 {len(pool_rows):,} 組，依勝率排序")
        print(f"{'勝率':>6}{'筆數':>5}{'總報酬':>9}{'單筆':>8}{'回檔':>8}  "
              f"{'跌幅':>5}{'補回':>5}{'天數':>5}{'警戒':>6}{'底倉':>5}{'補齊':>5}"
              f"{'突破':>5}  {'出場':<16}{'減碼':>5}{'谷停':>5}{'前高賣':>7}")
        for r in best:
            print(f"{r['win']:>6.0%}{r['n']:>5}{r['total']:>9.1%}{r['avg']:>8.1%}{r['mdd']:>8.1%}  "
                  f"{r['min_drawdown']:>5.0%}{r['repair_fraction']:>5.0%}{r['max_repair_bars']:>5}"
                  f"{r['warn_line_ratio']:>6.3f}{r['base_weight']:>5.1f}{r['fill_timeout_bars']:>5}"
                  f"{'Y' if r['breakout_fills_remainder'] else 'N':>5}  {r['exit_style']:<16}"
                  f"{r['warn_derisk_fraction']:>5.1f}{'Y' if r['hard_stop_at_trough'] else 'N':>5}"
                  f"{r['target_take_fraction']:>7.2f}")
        print()

    # ---- 邊際分析：每個參數自己的貢獻 ----
    print("【邊際分析】固定某參數取值，對所有其他組合取平均")
    for key in grid:
        if len(grid[key]) < 2:
            continue
        print(f"\n  {key}")
        print(f"    {'取值':<18}{'組合數':>8}{'平均勝率':>10}{'平均總報酬':>12}{'平均交易數':>11}")
        for val, cnt, win, total, n in marginal(rows, key if key != "exit_style" else "exit_style"):
            print(f"    {str(val):<18}{cnt:>8,}{win:>10.1%}{total:>12.1%}{n:>11.1f}")

    # ---- 預設值在分布中的位置 ----
    wins = sorted(r["win"] for r in rows)
    totals = sorted(r["total"] for r in rows)
    _, st = run_backtest(bars, DEFAULT_CONFIG, etf)
    def pct(sorted_vals, v):
        return sum(1 for x in sorted_vals if x <= v) / len(sorted_vals)
    print(f"\n【預設參數的位置】勝率 {st.win_rate:.0%}（贏過 {pct(wins, st.win_rate):.0%} 的組合）、"
          f"總報酬 {st.total_return:.1%}（贏過 {pct(totals, st.total_return):.0%} 的組合）")
    print(f"       全網格勝率中位數 {statistics.median(wins):.0%}、"
          f"總報酬中位數 {statistics.median(totals):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
