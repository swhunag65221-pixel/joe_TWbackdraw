#!/usr/bin/env python3
"""抓期交所的台指期分月合約收盤價，用來算換倉價差。

    python3 scripts/fetch_taifex_rolls.py --start 199901 --out data/tx_rolls.csv

為什麼需要這支腳本
------------------
FinLab 的 `futures_price` 每個商品只有**一條近月連續序列**（`TX一般`），
換倉時整條序列直接跳到次月，跳動的中位數是 −0.27%。若把那個跳動當成損益，
337 次換倉會憑空製造每年約 −3% 的假虧損。

要正確換倉，需要「同一天的近月收盤」與「次月收盤」——FinLab 沒有，
期交所的每日行情有。本腳本只抓換倉日所在的月份，取出那兩個價格。

期交所的查詢區間上限約一個月，所以每個月發一次請求，並快取於 data/raw_taifex/。
"""

from __future__ import annotations

import argparse
import calendar
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw_taifex"
URL = "https://www.taifex.com.tw/cht/3/futDataDown"
UA = "Mozilla/5.0 (compatible; tw-backdraw-research/1.0)"


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch_month(ym: str, pause: float) -> str:
    """抓一個月的台指期每日行情（Big5 CSV 原文）。"""
    RAW.mkdir(parents=True, exist_ok=True)
    cached = RAW / f"TX_{ym}.csv"
    if cached.exists():
        return cached.read_text(encoding="utf-8")

    y, m = int(ym[:4]), int(ym[4:6])
    last = calendar.monthrange(y, m)[1]
    body = urllib.parse.urlencode({
        "down_type": "1", "commodity_id": "TX",
        "queryStartDate": f"{y:04d}/{m:02d}/01",
        "queryEndDate": f"{y:04d}/{m:02d}/{last:02d}",
    }).encode()

    for attempt in range(4):
        try:
            req = urllib.request.Request(URL, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode("big5", errors="replace")
            if not text.lstrip().startswith("交易日期"):
                raise ValueError("回應不是預期的 CSV（可能被拒絕）")
            cached.write_text(text, encoding="utf-8")
            time.sleep(pause)
            return text
        except Exception as exc:
            if attempt == 3:
                print(f"  {ym} 失敗：{exc}", file=sys.stderr)
                return ""
            time.sleep(2 ** attempt)
    return ""


def parse(text: str) -> list[tuple]:
    """回傳 [(日期, 合約月, 收盤, 成交量)]，只取一般交易時段的單式合約。"""
    rows = []
    for line in text.strip().splitlines()[1:]:
        f = [x.strip() for x in line.split(",")]
        if len(f) < 18 or f[1] != "TX":
            continue
        contract, close, vol, session = f[2], f[6], f[9], f[17]
        if session != "一般" or "/" in contract:      # 排除價差對單式
            continue
        if close in ("-", ""):
            continue
        try:
            rows.append((f[0].replace("/", "-"), contract,
                         float(close.replace(",", "")), int(vol.replace(",", "") or 0)))
        except ValueError:
            continue
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="抓期交所台指期分月合約收盤價")
    p.add_argument("--start", default="199901", help="起始年月 YYYYMM")
    p.add_argument("--end", default=None, help="結束年月 YYYYMM，預設為本月")
    p.add_argument("--out", default=str(ROOT / "data" / "tx_contracts.csv"))
    p.add_argument("--pause", type=float, default=0.4)
    args = p.parse_args()

    end = args.end
    if end is None:
        from datetime import date
        end = date.today().strftime("%Y%m")

    yms = months(args.start, end)
    print(f"抓取台指期 {args.start} ~ {end}（{len(yms)} 個月）")
    all_rows, done = [], 0
    for ym in yms:
        rows = parse(fetch_month(ym, args.pause))
        all_rows.extend(rows)
        done += 1
        if done % 20 == 0 or done == len(yms):
            print(f"  {done}/{len(yms)}　累計 {len(all_rows):,} 筆")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    all_rows.sort()
    with out.open("w", encoding="utf-8") as fh:
        fh.write("date,contract,close,volume\n")
        for d, c, cl, v in all_rows:
            fh.write(f"{d},{c},{cl},{v}\n")
    print(f"\n完成：{len(all_rows):,} 筆 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
