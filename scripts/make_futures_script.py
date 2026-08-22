#!/usr/bin/env python3
"""產生可在本機單獨執行的台指期回測腳本。

    python3 scripts/make_futures_script.py

輸出 dist/tx_futures_backtest.py —— 單一檔案、不需 clone repo，
只要 `pip install finlab pandas` 與一組 FinLab API token。

策略程式碼從 `tw_backdraw/` 逐字嵌入（與專案回測同一份），換倉價差則嵌入
已抓好的歷史值；遇到嵌入資料沒涵蓋的新換倉日，腳本會自動向期交所補抓。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "tx_futures_backtest.py"

MODULES = ["bars", "config", "levels", "setup", "leveraged",
           "engine", "backtest", "plan", "status", "futures", "__init__"]


def embed_sources() -> str:
    parts = ["_SOURCES = {}"]
    for name in MODULES:
        src = (ROOT / "tw_backdraw" / f"{name}.py").read_text(encoding="utf-8")
        parts.append(f"_SOURCES[{name!r}] = {src!r}")
    return "\n".join(parts)


def build_roll_map() -> dict:
    """由 data/tx_contracts.csv 與 FinLab 的近月序列推出 {換倉日: 次月收盤}。

    這裡在產生腳本時就算好，結果嵌入輸出檔。
    """
    sys.path.insert(0, str(ROOT))
    import os
    import warnings

    import finlab

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        finlab.login(os.environ["Finlab_API_token"])
    import pandas as pd
    from finlab import data

    fc = data.get("futures_price:收盤價")["TX一般"].dropna()
    fm = data.get("futures_price:到期月份(週別)")["TX一般"].dropna()
    df = pd.DataFrame({"close": fc, "exp": fm}).dropna()
    df = df[df.index >= "1999-01-01"]
    dates = [d.date() for d in df.index]
    contract = [str(x) for x in df["exp"]]

    by: dict = {}
    with open(ROOT / "data" / "tx_contracts.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            by.setdefault(r["date"], {})[r["contract"]] = float(r["close"])

    out = {}
    for i in range(1, len(dates)):
        if contract[i] != contract[i - 1]:
            d0 = dates[i - 1].isoformat()
            px = by.get(d0, {}).get(contract[i])
            if px:
                out[d0] = px
    return out


BODY = '''#!/usr/bin/env python3
"""台指期（TX）快速修復回檔入場策略 —— FinLab sim 回測，單檔可執行。

    pip install finlab pandas
    export Finlab_API_token=你的token          # Windows: set Finlab_API_token=...
    python tx_futures_backtest.py              # 預設槓桿上限 5 倍
    python tx_futures_backtest.py --max-leverage 3
    python tx_futures_backtest.py --preset post --html out.html

做什麼
------
訊號來自**加權指數**，部位開在**台指期連續合約**。與 ETF 版的三個差異：

1. **換倉**：近月到期時，以「換倉日的近月收盤」平倉、「次月同日收盤」建倉，
   價差不計入損益。台指期長期逆價差，不還原的話 332 次換倉會憑空造成
   每年約 −3.8% 的假虧損（實測未還原 7.28x vs 已還原 20.44x）。

2. **槓桿由停損距離決定**：L = 風險預算 ÷ 停損距離，上限由 --max-leverage 指定。
   刻意不套 ETF 版的 5% 停損下限 —— 那個下限會讓 L 恆等於 1.6 倍，上限永遠碰不到。

3. **進場後不調整口數**：持有期間權益為 1 + L×(F/F0 − 1)，線性、不複利、不再平衡。

4. **當天成交**：加權指數 13:30 收盤、台指期 13:45 收盤，中間 15 分鐘足夠下單。
   訊號以指數收盤判定後，當天的期貨收盤就能成交，不必等隔天。
   ETF 版沒有這個空間（同時收盤），只能次日成交。
   實測差距很大 —— 隔夜跳空正是把「假設停損 1.5%」放大成「實際虧損 7.8%」的主因：

       隔日成交   CAGR 14.9%  最大回檔 -71.5%  單筆最差 -48.1%
       當天成交   CAGR 17.1%  最大回檔 -45.7%  單筆最差 -22.0%

   以 --next-day-fill 可切回隔日成交做對照。

⚠️ 這是研究專案，不是投資建議。即使改成當天成交，5 倍上限仍有 −45.7% 的
最大回檔與單筆 −22% 的虧損，原因見輸出末尾的「停損假設 vs 實際」一節。
"""

from __future__ import annotations

import argparse
import calendar
import os
import pathlib
import sys
import urllib.parse
import urllib.request
import warnings

SYMBOL = "TXF"


# ---------------------------------------------------------------- 策略程式碼
def write_package(dest: pathlib.Path) -> None:
    pkg = dest / "tw_backdraw"
    pkg.mkdir(parents=True, exist_ok=True)
    for name, src in _SOURCES.items():
        (pkg / f"{name}.py").write_text(src, encoding="utf-8")
    if str(dest) not in sys.path:
        sys.path.insert(0, str(dest))


# ---------------------------------------------------------------- 資料
def login() -> None:
    token = next((os.environ[k] for k in
                  ("Finlab_API_token", "FINLAB_API_TOKEN", "FINLAB_TOKEN")
                  if os.environ.get(k)), None)
    if not token:
        raise SystemExit("請先設定環境變數 Finlab_API_token")
    import finlab
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        finlab.login(token)


def fetch_taifex_next_close(day: str, contract: str) -> float | None:
    """向期交所補抓某日某合約的收盤價（嵌入資料沒涵蓋的新換倉日才會用到）。"""
    y, m = int(day[:4]), int(day[5:7])
    last = calendar.monthrange(y, m)[1]
    body = urllib.parse.urlencode({
        "down_type": "1", "commodity_id": "TX",
        "queryStartDate": f"{y:04d}/{m:02d}/01",
        "queryEndDate": f"{y:04d}/{m:02d}/{last:02d}"}).encode()
    try:
        req = urllib.request.Request(
            "https://www.taifex.com.tw/cht/3/futDataDown", data=body,
            headers={"User-Agent": "Mozilla/5.0 (tw-backdraw)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("big5", errors="replace")
    except Exception as exc:
        print(f"  期交所補抓 {day} 失敗：{exc}", file=sys.stderr)
        return None
    for line in text.splitlines()[1:]:
        f = [x.strip() for x in line.split(",")]
        if (len(f) > 17 and f[1] == "TX" and f[0].replace("/", "-") == day
                and f[2] == contract and f[17] == "一般" and f[6] not in ("-", "")):
            return float(f[6].replace(",", ""))
    return None


def load_data():
    """回傳 (指數日線, 期貨連續序列, 缺換倉價差的日期)。"""
    import pandas as pd
    from finlab import data
    from tw_backdraw.bars import Bar
    from tw_backdraw.futures import build_continuous, missing_rolls

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

    rolls = {}
    for k, v in _ROLL_NEXT_CLOSE.items():
        y, m, d = (int(x) for x in k.split("-"))
        rolls[__import__("datetime").date(y, m, d)] = v

    # 嵌入資料沒涵蓋的換倉日（通常是產生腳本之後才發生的），向期交所補抓
    for miss in missing_rolls(fdates, contract, rolls):
        i = fdates.index(miss)
        px = fetch_taifex_next_close(miss.isoformat(), contract[i + 1])
        if px:
            rolls[miss] = px
            print(f"  補抓換倉價差 {miss} → {contract[i + 1]} 收 {px:,.0f}")

    still_missing = missing_rolls(fdates, contract, rolls)
    cont = build_continuous(fdates, front, contract, rolls)
    fut = dict(zip(fdates, cont))

    bars = [Bar(d=d.date(), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]))
            for d, r in idx.iterrows() if d.date() in fut]
    return bars, [fut[b.d] for b in bars], still_missing


# ---------------------------------------------------------------- 回測
def run(preset: str, max_leverage: float, same_day: bool = True,
        defensive: bool = False, fixed_lev: float = 0.0, ma_period: int = 200):
    from tw_backdraw.config import PRESETS
    from tw_backdraw.engine import Engine
    from tw_backdraw.futures import (
        FuturesCost, entries_from_trades, fixed_leverage, trade_details,
        trend_filter, vehicle_series)

    cfg = PRESETS[preset]
    bars, fseries, missing = load_data()
    print(f"期間 {bars[0].d} ~ {bars[-1].d}（{len(bars)} 個交易日）")
    if missing:
        print(f"⚠ 有 {len(missing)} 個換倉日缺次月報價，該處沿用原始跳動：{missing[:5]}")
    else:
        print("換倉價差：全數取得，無缺漏")

    print("成交時點：" + ("當天期貨收盤（指數 13:30 收、期貨 13:45 收）"
                       if same_day else "隔一個交易日收盤"))
    result = Engine(cfg).run(bars, fseries)
    entries = entries_from_trades(result.trades, bars, cfg, max_leverage, same_day)
    if defensive:
        before = len(entries)
        entries = trend_filter(entries, bars, ma_period, below=True)
        print(f"逆勢濾網：只做收盤低於 MA{ma_period} 的訊號　"
              f"{before} → {len(entries)} 筆")
    if fixed_lev:
        entries = fixed_leverage(entries, fixed_lev)
        print(f"固定槓桿：所有部位一律 {fixed_lev:g}x（不再依停損距離決定）")

    dates = [b.d for b in bars]
    nav, detail = vehicle_series(dates, fseries, entries,
                                 FuturesCost(), [b.close for b in bars])
    return (bars, fseries, entries, nav, detail,
            trade_details(dates, nav, entries, detail))


def build_report(bars, nav, entries, name: str):
    """把淨值序列交給 FinLab sim，產生可 display() 的 Report。"""
    import numpy as np
    import pandas as pd
    from finlab.backtest import sim
    from finlab.markets.tw import TWMarket

    di = pd.to_datetime([b.d for b in bars])
    price = pd.DataFrame({SYMBOL: nav}, index=di)

    class TWFuturesMarket(TWMarket):
        """自訂市場：價格一律回傳我們算好的期貨部位淨值。

        FinLab 內建的台股市場找不到 TXF，會讓 report.display() 在組
        positionConfig 時失敗；由市場自己供應價格即可避開。
        """

        @staticmethod
        def get_name():
            return "tw_futures"

        @staticmethod
        def get_asset_id_to_name():
            return {SYMBOL: "台指期連續合約"}

        def get_price(self, trade_at_price, adj=True):
            if isinstance(trade_at_price, (pd.DataFrame, pd.Series)):
                return pd.DataFrame(trade_at_price)
            return price

    # FinLab 的 position 日期是「訊號日」、次一交易日成交，所以權重往前挪一天
    pos = np.zeros(len(bars))
    for e in entries:
        end = (e.exit_i - 1) if e.exit_i is not None else len(bars)
        pos[max(e.entry_i - 1, 0):max(end, 0)] = 1.0

    report = sim(pd.DataFrame({SYMBOL: pos}, index=di), trade_at_price=price,
                 position_limit=1, fee_ratio=0.0, tax_ratio=0.0,
                 market=TWFuturesMarket(), name=name, upload=False)
    report.trade_at = "close"          # 讓 display() 能序列化
    return report


# ---------------------------------------------------------------- 輸出
def metrics(series):
    import numpy as np
    ret = series.pct_change().dropna()
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = float(series.iloc[-1] / series.iloc[0] - 1)
    mdd = float((series / series.cummax() - 1).min())
    cagr = (1 + total) ** (1 / years) - 1
    down = ret[ret < 0].std()
    return dict(CAGR=cagr, 總報酬=total, 最大回檔=mdd,
                Sharpe=float(ret.mean() / ret.std() * np.sqrt(252)),
                Sortino=float(ret.mean() / down * np.sqrt(252)) if down else float("nan"),
                Calmar=cagr / abs(mdd) if mdd else float("nan"))


def main() -> int:
    ap = argparse.ArgumentParser(description="台指期快速修復策略回測")
    ap.add_argument("--preset", default="tuned",
                    help="參數組：tuned（預設）/ post / balanced / winrate")
    ap.add_argument("--max-leverage", type=float, default=5.0, help="槓桿上限，預設 5")
    ap.add_argument("--next-day-fill", action="store_true",
                    help="改用隔一個交易日收盤成交（預設為當天期貨收盤）")
    ap.add_argument("--defensive", action="store_true",
                    help="逆勢濾網：只做進場日收盤低於 MA200 的訊號")
    ap.add_argument("--ma-period", type=int, default=200, help="濾網用的均線天期")
    ap.add_argument("--fixed-leverage", type=float, default=0.0,
                    help="所有部位改用同一個槓桿（建議搭配 --defensive，3 倍）")
    ap.add_argument("--trades-since", default=None, metavar="YYYY-MM-DD",
                    help="額外印出這天之後每一筆的完整明細（進場條件、"
                         "距離停損、槓桿、最大報酬／最大不利／期間最大回撤）")
    ap.add_argument("--html", default="tx_futures_report.html",
                    help="把互動報表寫成 HTML 檔，設空字串則不輸出")
    ap.add_argument("--workdir", default=".tw_backdraw_pkg",
                    help="策略程式碼展開的位置")
    args = ap.parse_args()

    write_package(pathlib.Path(args.workdir))
    login()

    import pandas as pd
    bars, fseries, entries, nav, detail, details = run(
        args.preset, args.max_leverage, same_day=not args.next_day_fill,
        defensive=args.defensive, fixed_lev=args.fixed_leverage,
        ma_period=args.ma_period)

    print(f"\\n{'進場':<12}{'出場':<12}{'停損距離':>9}{'槓桿':>7}"
          f"{'期貨報酬':>10}{'權益報酬':>10}")
    print("-" * 62)
    for t in detail:
        print(f"{t.entry_date!s:<12}{str(t.exit_date or '持有中'):<12}"
              f"{t.stop_distance:>9.2%}{t.leverage:>7.2f}"
              f"{t.futures_return:>10.1%}{t.ret:>10.1%}")

    if args.trades_since:
        from datetime import datetime as _dt
        from tw_backdraw.futures import format_trade_details
        cut = _dt.strptime(args.trades_since, "%Y-%m-%d").date()
        picked = [d for d in details if d.trade.entry_date >= cut]
        print(f"\\n—— {cut} 之後的逐筆明細（{len(picked)} 筆）——\\n")
        print(format_trade_details(picked))

    levs = [t.leverage for t in detail]
    if args.fixed_leverage:
        print(f"\\n槓桿：固定 {args.fixed_leverage:g}x，共 {len(levs)} 筆")
    else:
        capped = sum(1 for x in levs if x >= args.max_leverage - 1e-9)
        print(f"\\n槓桿：中位數 {sorted(levs)[len(levs) // 2]:.2f}　"
              f"範圍 {min(levs):.2f}~{max(levs):.2f}　封頂 {capped}/{len(levs)} 筆")

    fill = "隔日" if args.next_day_fill else "當日"
    lev = (f"固定 {args.fixed_leverage:g}x" if args.fixed_leverage
           else f"槓桿≤{args.max_leverage:g}x")
    tag = f"，逆勢 MA{args.ma_period}" if args.defensive else ""
    report = build_report(
        bars, nav, entries,
        f"台指期快速修復 {args.preset}（{lev}{tag}，{fill}成交）")

    idx_series = pd.Series([b.close for b in bars],
                           index=pd.to_datetime([b.d for b in bars]))
    fut_series = pd.Series(fseries, index=idx_series.index)
    # 用自己算的 nav，不要用 report.creturn —— 後者從**第一筆交易**起算，
    # 期初空手的策略 CAGR 會被灌水（防守版空手三年：19.2% vs 實際 17.0%）。
    eq = pd.Series(nav, index=idx_series.index)
    rows = {f"策略（{lev}{tag}）": metrics(eq),
            "買進持有 台指期（已還原換倉）": metrics(fut_series),
            "加權指數（價格指數）": metrics(idx_series)}
    table = pd.DataFrame(rows).T
    for c in ("CAGR", "總報酬", "最大回檔"):
        table[c] = table[c].map("{:.1%}".format)
    for c in ("Sharpe", "Sortino", "Calmar"):
        table[c] = table[c].map("{:.2f}".format)
    print("\\n" + table.to_string())

    worst = min(detail, key=lambda t: t.ret)
    print(f"\\n單筆最差：{worst.entry_date} → {worst.exit_date}　"
          f"槓桿 {worst.leverage:.2f}x　權益 {worst.ret:.1%}")
    print("停損假設 vs 實際（實際跌幅仍大於假設：訊號在收盤才判定，"
          "且期貨與指數的 15 分鐘差內價格會續走）：")
    i_of = {b.d: i for i, b in enumerate(bars)}
    for t in sorted(detail, key=lambda t: t.ret)[:5]:
        a = i_of[t.entry_date]
        b = i_of[t.exit_date] if t.exit_date else len(bars) - 1
        real = (bars[b].close - bars[a].close) / bars[a].close
        ratio = abs(real) / t.stop_distance if t.stop_distance else 0
        print(f"  {t.entry_date}  假設 -{t.stop_distance:.2%} → 實際 {real:+.2%}"
              f"（{ratio:.1f} 倍）　槓桿 {t.leverage:.1f}x → 權益 {t.ret:.1%}")

    if args.html:
        try:
            from finlab.core.dashboard import generate_html
            pathlib.Path(args.html).write_text(generate_html(report), encoding="utf-8")
            print(f"\\n互動報表 → {pathlib.Path(args.html).resolve()}（用瀏覽器開啟）")
        except Exception as exc:
            print(f"\\n產生 HTML 失敗：{exc}", file=sys.stderr)
    try:                      # 在 Jupyter 裡才有意義
        report.display()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> int:
    rolls = build_roll_map()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    text = (BODY.split("SYMBOL = \"TXF\"")[0]
            + f'SYMBOL = "TXF"\n\n_ROLL_NEXT_CLOSE = {json.dumps(rolls, indent=0)}\n\n'
            + embed_sources() + "\n"
            + BODY.split("SYMBOL = \"TXF\"", 1)[1])
    OUT.write_text(text, encoding="utf-8")

    try:
        compile(OUT.read_text(encoding="utf-8"), str(OUT), "exec")
    except SyntaxError as exc:
        print(f"✗ 語法錯誤：{exc}", file=sys.stderr)
        return 1
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB，"
          f"換倉價差 {len(rolls)} 筆）")
    print("  ✓ 語法檢查通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
