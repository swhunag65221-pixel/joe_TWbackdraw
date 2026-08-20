#!/usr/bin/env python3
"""台指期資料載入：加權指數日線 + 已還原換倉價差的期貨連續序列。

近月連續序列與到期月份來自 FinLab，換倉當日的次月收盤來自期交所
（`data/tx_contracts.csv`，用 `scripts/fetch_taifex_rolls.py` 抓）。
"""

from __future__ import annotations

import csv
import os
import sys
import warnings
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tw_backdraw.bars import Bar                                # noqa: E402
from tw_backdraw.futures import build_continuous, missing_rolls  # noqa: E402


def login() -> None:
    import finlab
    token = os.environ.get("Finlab_API_token")
    if not token:
        raise SystemExit("請先設定環境變數 Finlab_API_token")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        finlab.login(token)


def roll_map(dates: list[date], contract: list[str],
             path: Path | None = None) -> dict[date, float]:
    """{換倉日: 次月合約在該日的收盤}，由期交所分月行情推出。"""
    path = path or ROOT / "data" / "tx_contracts.csv"
    by: dict[str, dict[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            by.setdefault(r["date"], {})[r["contract"]] = float(r["close"])

    out: dict[date, float] = {}
    for i in range(1, len(dates)):
        if contract[i] != contract[i - 1]:
            px = by.get(dates[i - 1].isoformat(), {}).get(contract[i])
            if px:
                out[dates[i - 1]] = px
    return out


def load_tx() -> tuple[list[Bar], list[float], list[date]]:
    """回傳 (指數日線, 對齊的期貨連續序列, 缺換倉價差的日期)。"""
    import pandas as pd
    from finlab import data

    idx = pd.DataFrame({k: data.get(f"taiex_total_index:{n}指數").iloc[:, 0]
                        for k, n in (("open", "開盤"), ("high", "最高"),
                                     ("low", "最低"), ("close", "收盤"))}
                       ).dropna(subset=["close"])
    fc = data.get("futures_price:收盤價")["TX一般"].dropna()
    fm = data.get("futures_price:到期月份(週別)")["TX一般"].dropna()
    fdf = pd.DataFrame({"close": fc, "exp": fm}).dropna()
    fdf = fdf[fdf.index >= "1999-01-01"]

    fdates = [d.date() for d in fdf.index]
    front = [float(x) for x in fdf["close"]]
    contract = [str(x) for x in fdf["exp"]]

    rolls = roll_map(fdates, contract)
    cont = build_continuous(fdates, front, contract, rolls)
    fut = dict(zip(fdates, cont))

    bars = [Bar(d=d.date(), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]))
            for d, r in idx.iterrows() if d.date() in fut]
    return bars, [fut[b.d] for b in bars], missing_rolls(fdates, contract, rolls)
