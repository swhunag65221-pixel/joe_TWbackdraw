#!/usr/bin/env python3
"""印出目前這一輪劇本的操作計畫。

有 data/taiex.csv 就用真實資料自動抓最新一次訊號與最新收盤；
沒有的話退回貼文中的實例（P=47742、T=39933）。

    python3 scripts/current_plan.py --capital 1000000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw import build_plan, detect_setups, load_csv          # noqa: E402
from tw_backdraw.config import DEFAULT_CONFIG                        # noqa: E402

# 貼文中的台股實例，作為沒有資料時的後備
FALLBACK_PEAK, FALLBACK_TROUGH, FALLBACK_NOW = 47742.0, 39933.0, 46200.0


def main() -> int:
    p = argparse.ArgumentParser(description="產生目前的操作計畫")
    p.add_argument("--csv", default=str(ROOT / "data" / "taiex.csv"))
    p.add_argument("--capital", type=float, help="投入本金，用來換算金額")
    p.add_argument("--now", type=float, help="覆寫參考指數")
    args = p.parse_args()

    cfg = DEFAULT_CONFIG
    csv_path = Path(args.csv)

    if csv_path.exists():
        bars = load_csv(csv_path)
        setups = detect_setups(bars, cfg.setup)
        if not setups:
            print(f"{csv_path} 內沒有偵測到快速修復訊號。")
            return 1
        s = setups[-1]
        peak, trough = s.peak, s.trough
        now = args.now or bars[-1].close
        print(f"資料：{csv_path}（{bars[0].d} ~ {bars[-1].d}，{len(bars)} 根日線）")
        print(f"最新訊號：{s.describe()}")
        print(f"訊號至今經過 {len(bars) - 1 - s.trigger_index} 個交易日\n")
    else:
        peak, trough = FALLBACK_PEAK, FALLBACK_TROUGH
        now = args.now or FALLBACK_NOW
        print(f"找不到 {csv_path}，改用貼文中的實例（先跑 scripts/fetch_twse.py 取得真實資料）\n")

    print(build_plan(peak, trough, now, cfg, args.capital).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
