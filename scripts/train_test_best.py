#!/usr/bin/env python3
"""切一個時間點，用之前的資料選出最佳參數，再拿到之後的資料驗收。

    python3 scripts/train_test_best.py --split 2018-01-01

與 `walk_forward.py` 的差別：那支比較的是「四種選擇標準各挑前 K 組」的平均，
回答「調參這件事有沒有用」；這支直接把**單一最佳參數組**印出來，
回答「當年那個時點會選到什麼，後來過得如何」。

`--min-trades` 是必要的護欄：訓練段只有二十年不到，一組參數只成交 2 筆
也可能算出漂亮的報酬÷回檔，那不是策略好，是樣本太少。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from grid_search import (GRID, add_market_args, base_from_args,        # noqa: E402
                         build_config, combos)
from tw_backdraw import load_csv                                       # noqa: E402
from tw_backdraw.backtest import run_backtest                          # noqa: E402
from tw_backdraw.config import DEFAULT_CONFIG, POST_CONFIG             # noqa: E402

# 選擇標準：名稱 → (排序鍵, 說明)
CRITERIA = {
    "calmar": (lambda m: (m["total"] / abs(m["mdd"]) if m["mdd"] else 0.0,),
               "報酬 ÷ 回檔（專案預設目標）"),
    "total": (lambda m: (m["total"],), "總報酬"),
    "win": (lambda m: (m["win"], m["total"]), "勝率（次要：總報酬）"),
    "avg": (lambda m: (m["avg"],), "單筆平均報酬"),
}

_BARS = _BASE = None


def _init(bars, base):
    global _BARS, _BASE
    _BARS, _BASE = bars, base


def _metrics(bars, cfg) -> dict:
    _, st = run_backtest(bars, cfg)
    return dict(n=st.n_trades, win=st.win_rate, avg=st.avg_return,
                total=st.total_return, mdd=st.max_drawdown,
                best=st.best, worst=st.worst,
                calmar=st.total_return / abs(st.max_drawdown) if st.max_drawdown else 0.0)


def _eval(p: dict) -> tuple:
    try:
        m = _metrics(_BARS, build_config(p, _BASE))
    except Exception:
        m = dict(n=0, win=0.0, avg=-1.0, total=-1.0, mdd=-1.0,
                 best=0.0, worst=0.0, calmar=-1.0)
    return p, m


def describe(p: dict) -> str:
    mode, ma, trail = p["exit_style"]
    exit_txt = {"trail": f"移動停利，自高點回檔 {trail:.0%}",
                "ma_ratchet": f"創高後以 max(前高, MA{ma}) 為出場線",
                "both": f"移動停利 {trail:.0%} 與 MA{ma} 取較緊者"}[mode]
    return "\n".join([
        f"  回檔門檻 min_drawdown          {p['min_drawdown']:.0%}",
        f"  修復比例 repair_fraction       {p['repair_fraction']:.0%}",
        f"  修復天數上限 max_repair_bars   {p['max_repair_bars']} 個交易日",
        f"  警戒線位置 warn_line_ratio     {p['warn_line_ratio']:.1%} 回補位",
        f"  首筆權重 base_weight           {p['base_weight']:.0%}",
        f"  回檔梯逾時 fill_timeout_bars   {p['fill_timeout_bars']} 個交易日",
        f"  創高補足剩餘部位               {p['breakout_fills_remainder']}",
        f"  出場方式 exit_style            {exit_txt}",
        f"  跌破警戒線減碼比例             {p['warn_derisk_fraction']:.0%}",
        f"  跌破谷底硬停損                 {p['hard_stop_at_trough']}",
        f"  回到前高先獲利了結             {p['target_take_fraction']:.0%}",
    ])


def row(label: str, m: dict) -> str:
    return (f"{label:<26}{m['n']:>5}{m['win']:>9.0%}{m['total']:>12.1%}"
            f"{m['mdd']:>10.1%}{m['calmar']:>9.2f}{m['avg']:>10.1%}")


HEAD = (f"{'':<26}{'筆數':>5}{'勝率':>9}{'總報酬':>12}{'最大回檔':>10}"
        f"{'報酬/回檔':>9}{'單筆平均':>10}")


def main() -> int:
    ap = argparse.ArgumentParser(description="訓練段選參數、測試段驗收")
    ap.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    ap.add_argument("--split", default="2018-01-01")
    ap.add_argument("--min-trades", type=int, default=5, help="訓練段最少成交筆數")
    ap.add_argument("--criterion", default=None,
                    help="只跑單一標準，預設四種都跑")
    add_market_args(ap)
    args = ap.parse_args()
    base = base_from_args(args)

    bars = load_csv(args.csv)
    train = [b for b in bars if b.iso < args.split]
    test = [b for b in bars if b.iso >= args.split]
    print(f"訓練段 {train[0].d} ~ {train[-1].d}（{len(train)} 根）")
    print(f"測試段 {test[0].d} ~ {test[-1].d}（{len(test)} 根）")
    print(f"訓練段最少成交筆數門檻：{args.min_trades}")

    todo = list(combos(GRID))
    print(f"\n掃描訓練段 {len(todo):,} 組 …", flush=True)

    crits = ({args.criterion: CRITERIA[args.criterion]} if args.criterion
             else CRITERIA)
    best: dict = {k: None for k in crits}
    seen = 0
    with mp.Pool(mp.cpu_count(), initializer=_init, initargs=(train, base)) as pool:
        for p, m in pool.imap_unordered(_eval, todo, chunksize=256):
            seen += 1
            if seen % 100_000 == 0:
                print(f"  {seen:,} / {len(todo):,}", flush=True)
            if m["n"] < args.min_trades:
                continue
            for name, (keyf, _) in crits.items():
                cur = best[name]
                if cur is None or keyf(m) > keyf(cur[1]):
                    best[name] = (p, m)

    for name, (_, desc) in crits.items():
        if best[name] is None:
            print(f"\n### {desc}：沒有任何組合達到 {args.min_trades} 筆門檻")
            continue
        p, tr = best[name]
        te = _metrics(test, build_config(p, base))
        print(f"\n{'=' * 78}\n### 訓練段最佳（依{desc}）\n")
        print(describe(p))
        print(f"\n{HEAD}")
        print("-" * 78)
        print(row("訓練段（樣本內）", tr))
        print(row("測試段（樣本外）", te))

    print(f"\n{'=' * 78}\n### 對照組在測試段的表現\n")
    print(HEAD)
    print("-" * 78)
    for label, cfg in (("專案預設 tuned ⚠樣本內", DEFAULT_CONFIG),
                       ("貼文原意 post", POST_CONFIG)):
        from dataclasses import replace
        print(row(label, _metrics(test, replace(cfg, sizing=base.sizing,
                                                cost=base.cost))))
    bh = test[-1].close / test[0].close - 1.0
    peak = dd = 0.0
    for b in test:
        peak = max(peak, b.close)
        dd = min(dd, b.close / peak - 1.0)
    print(f"\n加權指數買進持有：總報酬 {bh:+.1%}　最大回檔 {dd:.1%}")
    print("\n⚠ tuned 是用含測試段的完整資料選出來的，它在測試段的數字不是樣本外結果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
