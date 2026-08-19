#!/usr/bin/env python3
"""一次印出「這一輪的完整計畫」＋「目前部位該做什麼」。

    python3 scripts/current_plan.py --capital 1000000

需要 data/taiex.csv（用 scripts/fetch_finlab.py 取得）。
有 data/00631L.csv 時會改用真實 ETF 價格。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import build_plan, load_csv                         # noqa: E402
from tw_backdraw.backtest import align_etf, run_backtest             # noqa: E402
from tw_backdraw.config import DEFAULT_CONFIG                        # noqa: E402
from tw_backdraw.status import render_status                         # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="目前這一輪的操作計畫與部位現況")
    p.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    p.add_argument("--etf-csv", default=str(ROOT / "data" / "00631L.csv"))
    p.add_argument("--capital", type=float, help="投入本金，用來換算金額")
    args = p.parse_args()

    cfg = DEFAULT_CONFIG
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"找不到 {csv_path}，請先執行：python3 scripts/fetch_finlab.py", file=sys.stderr)
        return 1

    bars = load_csv(csv_path)
    etf = None
    etf_path = Path(args.etf_csv)
    if etf_path.exists():
        bars, etf = align_etf(bars, load_csv(etf_path))

    result, _ = run_backtest(bars, cfg, etf)
    if not result.setups:
        print("資料期間內沒有觸發過快速修復訊號。")
        return 1

    s = result.setups[-1]
    print(build_plan(s.peak, s.trough, bars[-1].close, cfg, args.capital).render())
    print()
    print(render_status(result, bars, cfg, args.capital))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
