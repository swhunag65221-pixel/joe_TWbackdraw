"""日線資料結構與讀檔。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    d: date
    open: float
    high: float
    low: float
    close: float

    @property
    def iso(self) -> str:
        return self.d.isoformat()


def _parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"無法解析日期: {raw!r}")


def load_csv(path: str | Path) -> list[Bar]:
    """讀取日線 CSV。

    必要欄位: date, close。open/high/low 缺漏時以 close 補齊，
    這樣只有收盤價的資料集也能直接跑（策略訊號全部以收盤價判定）。
    """
    bars: list[Bar] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("date") or not row.get("close"):
                continue
            close = float(row["close"].replace(",", ""))

            def pick(key: str) -> float:
                val = row.get(key, "")
                return float(val.replace(",", "")) if val else close

            bars.append(
                Bar(
                    d=_parse_date(row["date"]),
                    open=pick("open"),
                    high=pick("high"),
                    low=pick("low"),
                    close=close,
                )
            )
    bars.sort(key=lambda b: b.d)
    if not bars:
        raise ValueError(f"{path} 沒有可用的日線資料")
    return bars
