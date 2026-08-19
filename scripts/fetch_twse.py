#!/usr/bin/env python3
"""從證交所抓取加權指數 / 個股(ETF) 日線，輸出成本專案用的 CSV。

    python3 scripts/fetch_twse.py taiex --start 199901 --end 202608 --out data/taiex.csv
    python3 scripts/fetch_twse.py stock --stock 00631L --start 201410 --out data/00631L.csv

證交所以「月」為單位提供歷史資料，本腳本逐月抓取並快取到 data/raw/，
重跑時只補缺的月份（對站方友善，也讓離線環境能重複使用）。
加權指數最早提供到民國 88 年（1999）1 月 5 日。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

TAIEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={ym}01&response=json"
STOCK_URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
             "?date={ym}01&stockNo={stock}&response=json")
UA = "Mozilla/5.0 (compatible; tw-backdraw-research/1.0)"


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def roc_to_iso(raw: str) -> str:
    """民國日期 115/08/03 → 2026-08-03。"""
    y, m, d = raw.strip().split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def fetch_month(url: str, cache_key: str, pause: float) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    cached = RAW / f"{cache_key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            time.sleep(pause)
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            print(f"  重試 {cache_key}（{exc}）")
    return {}


def num(raw: str) -> float | None:
    raw = raw.replace(",", "").strip()
    try:
        return float(raw)
    except ValueError:
        return None      # 停牌 / 無交易的欄位是 "--"


def collect(kind: str, ym_list: list[str], stock: str | None, pause: float) -> list[tuple]:
    rows: list[tuple] = []
    for ym in ym_list:
        if kind == "taiex":
            payload = fetch_month(TAIEX_URL.format(ym=ym), f"taiex_{ym}", pause)
        else:
            payload = fetch_month(STOCK_URL.format(ym=ym, stock=stock), f"{stock}_{ym}", pause)

        if payload.get("stat") != "OK":
            print(f"  {ym}: {payload.get('stat', '無資料')}")
            continue

        for row in payload.get("data", []):
            if kind == "taiex":
                d, o, h, low, c = row[0], num(row[1]), num(row[2]), num(row[3]), num(row[4])
            else:
                # 個股欄位: 日期,成交股數,成交金額,開盤,最高,最低,收盤,漲跌價差,成交筆數
                d, o, h, low, c = row[0], num(row[3]), num(row[4]), num(row[5]), num(row[6])
            if c is None:
                continue
            rows.append((roc_to_iso(d), o or c, h or c, low or c, c))
        print(f"  {ym}: {len(payload.get('data', []))} 筆")
    rows.sort()
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="抓取證交所日線")
    p.add_argument("kind", choices=["taiex", "stock"])
    p.add_argument("--stock", help="股票/ETF 代號，kind=stock 時必填")
    p.add_argument("--start", default="199901", help="起始年月 YYYYMM")
    p.add_argument("--end", default=None, help="結束年月 YYYYMM，預設為本月")
    p.add_argument("--out", required=True, help="輸出 CSV 路徑")
    p.add_argument("--pause", type=float, default=0.6, help="每次請求間隔秒數")
    args = p.parse_args()

    if args.kind == "stock" and not args.stock:
        p.error("kind=stock 需要 --stock")

    end = args.end
    if end is None:
        from datetime import date
        end = date.today().strftime("%Y%m")

    ym_list = months(args.start, end)
    print(f"抓取 {args.kind} {args.start} ~ {end}（{len(ym_list)} 個月）")
    rows = collect(args.kind, ym_list, args.stock, args.pause)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write("date,open,high,low,close\n")
        for d, o, h, low, c in rows:
            fh.write(f"{d},{o},{h},{low},{c}\n")
    print(f"\n完成：{len(rows)} 根日線 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
