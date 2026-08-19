#!/usr/bin/env python3
"""用 FinLab 抓取加權指數與 00631L 日線，輸出成本專案用的 CSV。

這是本專案取得資料的**主要**方式（`fetch_twse.py` 是不依賴 FinLab 的備援）。

需要 FinLab API token，從環境變數讀取（依序嘗試）：
    Finlab_API_token / FINLAB_API_TOKEN / FINLAB_TOKEN

    python3 scripts/fetch_finlab.py                        # 兩份都抓
    python3 scripts/fetch_finlab.py --only taiex
    python3 scripts/fetch_finlab.py --etf 00675L

資料集：
    taiex_total_index:{開盤,最高,最低,收盤}指數   加權指數 OHLC，1999-01-05 起
    etl:adj_{open,high,low,close}                 全上市櫃個股/ETF 還原股價，00631L 自 2014-10-31 起

註：`taiex_total_index` 的名稱容易誤會，實際內容是**發行量加權股價指數**（價格指數，
    非報酬指數），已與證交所 MI_5MINS_HIST 逐筆核對相符。含息的報酬指數是
    `benchmark_return:發行量加權股價報酬指數`，本策略不使用。
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TAIEX_FIELDS = {
    "open": "taiex_total_index:開盤指數",
    "high": "taiex_total_index:最高指數",
    "low": "taiex_total_index:最低指數",
    "close": "taiex_total_index:收盤指數",
}
# 一律使用還原股價。00631L 於 2026-03-31 做過約 23:1 的分割，
# 未還原的 price:收盤價 會在那天出現 -95.7% 的假單日報酬，回測必然失真。
STOCK_FIELDS = {
    "open": "etl:adj_open",
    "high": "etl:adj_high",
    "low": "etl:adj_low",
    "close": "etl:adj_close",
}
RAW_STOCK_FIELDS = {
    "open": "price:開盤價",
    "high": "price:最高價",
    "low": "price:最低價",
    "close": "price:收盤價",
}

TOKEN_ENV_VARS = ("Finlab_API_token", "FINLAB_API_TOKEN", "FINLAB_TOKEN")


def login():
    token = next((os.environ[k] for k in TOKEN_ENV_VARS if os.environ.get(k)), None)
    if not token:
        print("找不到 FinLab token，請設定 Finlab_API_token 環境變數。", file=sys.stderr)
        print("（或改用不需帳號的備援：python3 scripts/fetch_twse.py taiex ...）", file=sys.stderr)
        raise SystemExit(2)
    import finlab

    with warnings.catch_warnings():
        # 舊版 token 登入即將淘汰，但目前仍可用；新版請改跑 `python -m finlab login`
        warnings.simplefilter("ignore", DeprecationWarning)
        finlab.login(token)


def write_csv(df, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    df = df.dropna(subset=["close"]).sort_index()
    with out.open("w", encoding="utf-8") as fh:
        fh.write("date,open,high,low,close\n")
        for d, row in df.iterrows():
            def val(key):
                v = row[key]
                return row["close"] if v != v else v      # NaN → 用收盤補
            fh.write(f"{d.date()},{val('open'):.2f},{val('high'):.2f},"
                     f"{val('low'):.2f},{row['close']:.2f}\n")
    print(f"  → {out}（{len(df)} 根日線，{df.index.min().date()} ~ {df.index.max().date()}）")


def fetch_taiex(out: Path) -> None:
    import pandas as pd
    from finlab import data

    print("抓取加權指數 OHLC …")
    cols = {k: data.get(ds).iloc[:, 0] for k, ds in TAIEX_FIELDS.items()}
    write_csv(pd.DataFrame(cols), out)


def fetch_etf(symbol: str, out: Path, raw: bool = False) -> None:
    import pandas as pd
    from finlab import data

    fields = RAW_STOCK_FIELDS if raw else STOCK_FIELDS
    print(f"抓取 {symbol} OHLC（{'未還原原始價' if raw else '還原股價'}）…")
    frames = {k: data.get(ds) for k, ds in fields.items()}
    missing = [k for k, f in frames.items() if symbol not in f.columns]
    if "close" in missing:
        raise SystemExit(f"FinLab 資料中找不到 {symbol}")
    cols = {k: (f[symbol] if symbol in f.columns else None) for k, f in frames.items()}
    write_csv(pd.DataFrame({k: v for k, v in cols.items() if v is not None}), out)


def main() -> int:
    p = argparse.ArgumentParser(description="用 FinLab 抓取策略所需日線")
    p.add_argument("--only", choices=["taiex", "etf"], help="只抓其中一份")
    p.add_argument("--etf", default="00631L", help="槓桿 ETF 代號，預設 00631L")
    p.add_argument("--outdir", default=str(ROOT / "data"))
    p.add_argument("--raw-prices", action="store_true",
                   help="改用未還原的原始價（會在除權息/分割日出現假跳空，僅供比對）")
    args = p.parse_args()

    login()
    outdir = Path(args.outdir)
    if args.only != "etf":
        fetch_taiex(outdir / "taiex.csv")
    if args.only != "taiex":
        fetch_etf(args.etf, outdir / f"{args.etf}.csv", raw=args.raw_prices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
