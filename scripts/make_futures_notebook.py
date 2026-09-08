#!/usr/bin/env python3
"""產生可在 Colab 直接執行的台指期回測 .ipynb。

    python3 scripts/make_futures_notebook.py

輸出 notebooks/tx_futures_finlab.ipynb —— 單一檔案、不需 clone repo，
在 Colab 由上而下執行即可，只需一組 FinLab API token。

策略程式碼直接從 `tw_backdraw/` 的原始碼嵌入，不另外手寫簡化版 ——
notebook 跑的與 repo 回測（scripts/futures_trades.py、dist/tx_futures_backtest.py）
是同一份程式；換倉價差嵌入已抓好的歷史值，新的換倉日會自動向期交所補抓，
因此結果與 repo 版完全一致。改了模組就重跑這支腳本重新產生。

產生時若環境有 Finlab_API_token，換倉價差會重新計算（與
make_futures_script.py 同一套邏輯）；沒有 token 則沿用
dist/tx_futures_backtest.py 已嵌入的那份。
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "notebooks" / "tx_futures_finlab.ipynb"
DIST = ROOT / "dist" / "tx_futures_backtest.py"

# 嵌入順序需符合 import 相依（與 make_futures_script.py 一致）
MODULES = ["bars", "config", "levels", "setup", "leveraged",
           "engine", "backtest", "plan", "status", "futures", "__init__"]


def md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {},
            "source": [l + "\n" for l in "\n".join(lines).split("\n")]}


def code(*lines: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": [l + "\n" for l in "\n".join(lines).split("\n")]}


def embed_package() -> str:
    """把 tw_backdraw 各模組的原始碼包成一段可執行的寫檔程式。"""
    parts = ["import pathlib",
             "",
             "PKG = pathlib.Path('tw_backdraw')",
             "PKG.mkdir(exist_ok=True)",
             "SOURCES = {}",
             ""]
    for name in MODULES:
        src = (ROOT / "tw_backdraw" / f"{name}.py").read_text(encoding="utf-8")
        # 用 repr 保證任何內容都能安全嵌入（含中文、引號、反斜線）
        parts.append(f"SOURCES[{name!r}] = {src!r}")
    parts += [
        "",
        "for _name, _src in SOURCES.items():",
        "    (PKG / f'{_name}.py').write_text(_src, encoding='utf-8')",
        "",
        "print(f'已寫入 {len(SOURCES)} 個模組到 tw_backdraw/')",
    ]
    return "\n".join(parts)


def roll_map() -> dict[str, float]:
    """{換倉日: 次月收盤}。有 token 就重新計算，否則沿用 dist 腳本嵌入的那份。"""
    if os.environ.get("Finlab_API_token"):
        sys.path.insert(0, str(ROOT / "scripts"))
        from make_futures_script import build_roll_map
        return build_roll_map()
    m = re.search(r"_ROLL_NEXT_CLOSE = (\{.*?\})\n\n",
                  DIST.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit("沒有 Finlab_API_token，也找不到 dist 腳本裡的換倉價差")
    print("（無 Finlab_API_token，沿用 dist/tx_futures_backtest.py 的換倉價差）")
    return ast.literal_eval(m.group(1))


def build(rolls: dict[str, float]) -> dict:
    cells = [
        md("# 台指期（TX）快速修復回檔入場策略 — FinLab 回測（Colab 版）",
           "",
           "大跌之後如果**修復得夠快**，行情大概率會回到舊高。本 notebook 把這個觀察寫成規則，",
           "訊號來自**加權指數**，部位開在**台指期連續合約**，用 `finlab.backtest.sim()` 回測",
           "並產生 `report.display()` 的互動報表。",
           "",
           "程式碼與換倉價差都已嵌入，**不需 clone repo**，結果與 repo 的",
           "`scripts/futures_trades.py`／`dist/tx_futures_backtest.py` 完全一致。",
           "",
           "**執行方式**：由上而下依序執行即可。需要 FinLab API token。",
           "",
           "---",
           "### 與 ETF 版（00631L）的四個差異",
           "",
           "1. **換倉**：近月到期時，以「換倉日的近月收盤」平倉、「次月同日收盤」建倉，",
           "   價差不計入損益。台指期長期逆價差，不還原的話 300 多次換倉會憑空造成",
           "   每年約 −3.8% 的假虧損（實測未還原 7.28x vs 已還原 20.44x）。",
           "2. **槓桿由停損距離決定**：L = 風險預算 ÷ 停損距離，上限 `MAX_LEVERAGE`。",
           "   刻意不套 ETF 版的 5% 停損下限 —— 那個下限會讓 L 恆等於 1.6 倍，上限永遠碰不到。",
           "3. **進場後不調整口數**：持有期間權益為 1 + L×(F/F0 − 1)，線性、不複利、不再平衡。",
           "4. **當天成交**：加權指數 13:30 收盤、台指期 13:45 收盤，中間 15 分鐘足夠下單。",
           "   訊號以指數收盤判定後，當天的期貨收盤就能成交，不必等隔天（ETF 版同時收盤，只能次日）。",
           "",
           "> ⚠️ 這是研究專案，**不是投資建議**。即使當天成交，5 倍上限仍有 −45.7% 的",
           "> 最大回檔與單筆 −22% 的虧損，原因見末尾的「停損假設 vs 實際」一節。"),

        md("## 1. 安裝套件"),
        code("!pip install -q finlab"),

        md("## 2. 登入 FinLab",
           "",
           "執行後貼上你的 API token（不會顯示在畫面上）。",
           "token 可在 [FinLab 會員頁面](https://ai.finlab.tw/) 取得。"),
        code("import getpass, os, warnings",
             "import finlab",
             "",
             "token = (os.environ.get('FINLAB_API_TOKEN') or os.environ.get('Finlab_API_token')",
             "         or getpass.getpass('FinLab API token: '))",
             "with warnings.catch_warnings():",
             "    warnings.simplefilter('ignore', DeprecationWarning)",
             "    finlab.login(token)"),

        md("## 3. 寫入策略程式碼",
           "",
           "以下是 `tw_backdraw` 套件的完整原始碼，直接寫成檔案後 import ——",
           "與專案回測使用的是同一份程式，不是簡化版。"),
        code(embed_package()),

        md("## 4. 換倉價差資料",
           "",
           f"嵌入 {len(rolls)} 筆歷史換倉日的「次月合約收盤價」（來源：期交所分月行情）。",
           "遇到嵌入資料沒涵蓋的新換倉日（產生 notebook 之後才發生的），",
           "下一節會自動向期交所補抓。"),
        code("import calendar, sys, urllib.parse, urllib.request",
             "",
             f"_ROLL_NEXT_CLOSE = {json.dumps(rolls, indent=0)}",
             "",
             "def fetch_taifex_next_close(day, contract):",
             "    \"\"\"向期交所補抓某日某合約的收盤價（嵌入資料沒涵蓋的新換倉日才會用到）。\"\"\"",
             "    y, m = int(day[:4]), int(day[5:7])",
             "    last = calendar.monthrange(y, m)[1]",
             "    body = urllib.parse.urlencode({",
             "        'down_type': '1', 'commodity_id': 'TX',",
             "        'queryStartDate': f'{y:04d}/{m:02d}/01',",
             "        'queryEndDate': f'{y:04d}/{m:02d}/{last:02d}'}).encode()",
             "    try:",
             "        req = urllib.request.Request(",
             "            'https://www.taifex.com.tw/cht/3/futDataDown', data=body,",
             "            headers={'User-Agent': 'Mozilla/5.0 (tw-backdraw)'})",
             "        with urllib.request.urlopen(req, timeout=60) as resp:",
             "            text = resp.read().decode('big5', errors='replace')",
             "    except Exception as exc:",
             "        print(f'  期交所補抓 {day} 失敗：{exc}', file=sys.stderr)",
             "        return None",
             "    for line in text.splitlines()[1:]:",
             "        f = [x.strip() for x in line.split(',')]",
             "        if (len(f) > 17 and f[1] == 'TX' and f[0].replace('/', '-') == day",
             "                and f[2] == contract and f[17] == '一般' and f[6] not in ('-', '')):",
             "            return float(f[6].replace(',', ''))",
             "    return None",
             "",
             "print(f'換倉價差：已嵌入 {len(_ROLL_NEXT_CLOSE)} 筆'",
             "      f'（{min(_ROLL_NEXT_CLOSE)} ~ {max(_ROLL_NEXT_CLOSE)}）')"),

        md("## 5. 取得資料",
           "",
           "| 資料 | 來源 | 說明 |",
           "|---|---|---|",
           "| 加權指數 OHLC | `taiex_total_index:*` | 名稱易誤會，實際是**價格指數**，已與證交所核對相符 |",
           "| 台指期近月收盤 | `futures_price:收盤價` 的 `TX一般` | 近月連續序列，1999 年起 |",
           "| 到期月份 | `futures_price:到期月份(週別)` 的 `TX一般` | 用來偵測換倉日 |",
           "",
           "換倉日以「近月收盤 ÷ 次月同日收盤」還原價差，接成可交易的連續序列。"),
        code("import datetime",
             "import pandas as pd",
             "from finlab import data",
             "from tw_backdraw.bars import Bar",
             "from tw_backdraw.futures import build_continuous, missing_rolls",
             "",
             "idx = pd.DataFrame({k: data.get(f'taiex_total_index:{n}指數').iloc[:, 0]",
             "                    for k, n in (('open', '開盤'), ('high', '最高'),",
             "                                 ('low', '最低'), ('close', '收盤'))}",
             "                   ).dropna(subset=['close'])",
             "fc = data.get('futures_price:收盤價')['TX一般'].dropna()",
             "fm = data.get('futures_price:到期月份(週別)')['TX一般'].dropna()",
             "fdf = pd.DataFrame({'close': fc, 'exp': fm}).dropna()",
             "fdf = fdf[fdf.index >= '1999-01-01']",
             "",
             "fdates = [d.date() for d in fdf.index]",
             "front = [float(x) for x in fdf['close']]",
             "contract = [str(x) for x in fdf['exp']]",
             "",
             "rolls = {datetime.date(*(int(x) for x in k.split('-'))): v",
             "         for k, v in _ROLL_NEXT_CLOSE.items()}",
             "",
             "# 嵌入資料沒涵蓋的換倉日（通常是產生 notebook 之後才發生的），向期交所補抓",
             "for miss in missing_rolls(fdates, contract, rolls):",
             "    i = fdates.index(miss)",
             "    px = fetch_taifex_next_close(miss.isoformat(), contract[i + 1])",
             "    if px:",
             "        rolls[miss] = px",
             "        print(f'  補抓換倉價差 {miss} → {contract[i + 1]} 收 {px:,.0f}')",
             "",
             "still_missing = missing_rolls(fdates, contract, rolls)",
             "cont = build_continuous(fdates, front, contract, rolls)",
             "fut = dict(zip(fdates, cont))",
             "",
             "bars = [Bar(d=d.date(), open=float(r['open']), high=float(r['high']),",
             "            low=float(r['low']), close=float(r['close']))",
             "        for d, r in idx.iterrows() if d.date() in fut]",
             "fseries = [fut[b.d] for b in bars]",
             "",
             "print(f'期間 {bars[0].d} ~ {bars[-1].d}（{len(bars)} 個交易日）')",
             "if still_missing:",
             "    print(f'⚠ 有 {len(still_missing)} 個換倉日缺次月報價，該處沿用原始跳動：'",
             "          f'{still_missing[:5]}')",
             "else:",
             "    print('換倉價差：全數取得，無缺漏')"),

        md("## 6. 選擇參數",
           "",
           "| 變數 | 預設 | 說明 |",
           "|---|---|---|",
           "| `PRESET` | `tuned` | 參數組：tuned / post / balanced / winrate |",
           "| `MAX_LEVERAGE` | 5.0 | 槓桿上限 |",
           "| `SAME_DAY` | True | 當天期貨收盤成交；False 改隔一個交易日收盤（對照用） |",
           "",
           "對應 repo 指令 `python3 scripts/futures_trades.py`（預設值完全相同）。"),
        code("from tw_backdraw.config import PRESETS",
             "",
             "PRESET = 'tuned'       # ← 想換參數組改這裡",
             "MAX_LEVERAGE = 5.0",
             "SAME_DAY = True",
             "",
             "cfg = PRESETS[PRESET]",
             "print(f'訊號   回檔 ≥{cfg.setup.min_drawdown:.0%}，谷底起 ≤{cfg.setup.max_repair_bars} 日'",
             "      f'內補回 ≥{cfg.setup.repair_fraction:.0%}')",
             "print(f'槓桿   風險預算 {cfg.sizing.risk_per_trade:.0%} ÷ 停損距離，上限 {MAX_LEVERAGE:g}x')",
             "print('成交   ' + ('當天期貨收盤（指數 13:30 收、期貨 13:45 收）' if SAME_DAY",
             "               else '隔一個交易日收盤'))"),

        md("## 7. 跑策略",
           "",
           "訊號引擎跑在**加權指數**上，交易轉成**期貨部位**：",
           "槓桿由停損距離決定、進出場各扣一次成本（期交稅十萬分之二＋每口 50 元），",
           "持有期間權益線性於期貨報酬（固定口數，不複利）。"),
        code("from tw_backdraw.engine import Engine",
             "from tw_backdraw.futures import (FuturesCost, entries_from_trades,",
             "                                 trade_details, vehicle_series)",
             "",
             "result = Engine(cfg).run(bars, fseries)",
             "entries = entries_from_trades(result.trades, bars, cfg, MAX_LEVERAGE, SAME_DAY)",
             "dates = [b.d for b in bars]",
             "nav, detail = vehicle_series(dates, fseries, entries, FuturesCost(),",
             "                             [b.close for b in bars])",
             "details = trade_details(dates, nav, entries, detail)",
             "",
             "print(f\"{'進場':<12}{'出場':<12}{'停損距離':>9}{'槓桿':>7}\"",
             "      f\"{'期貨報酬':>10}{'權益報酬':>10}\")",
             "print('-' * 62)",
             "for t in detail:",
             "    print(f\"{t.entry_date!s:<12}{str(t.exit_date or '持有中'):<12}\"",
             "          f\"{t.stop_distance:>9.2%}{t.leverage:>7.2f}\"",
             "          f\"{t.futures_return:>10.1%}{t.ret:>10.1%}\")",
             "",
             "levs = [t.leverage for t in detail]",
             "capped = sum(1 for x in levs if x >= MAX_LEVERAGE - 1e-9)",
             "print(f'\\n槓桿：中位數 {sorted(levs)[len(levs) // 2]:.2f}　'",
             "      f'範圍 {min(levs):.2f}~{max(levs):.2f}　封頂 {capped}/{len(levs)} 筆')"),

        md("## 8. 逐筆明細（含進場條件）",
           "",
           "每筆列出：買賣日期、持有天數、進場當初的實際條件（前高／谷底／回檔幅度／",
           "修復天數與比例／訊號日指數與距前高）、警戒線與失效線、距離停損 %、",
           "據此算出的槓桿，以及權益報酬、最大報酬（MFE）、最大不利（MAE）、",
           "期間最大回撤與出場原因。",
           "",
           "`TRADES_SINCE = '2016-08-20'` 可只看這天之後的交易；`None` 列出全部。",
           "對應 repo 指令 `python3 scripts/futures_trades.py --since 2016-08-20`。"),
        code("from tw_backdraw.futures import format_trade_details",
             "",
             "TRADES_SINCE = None    # 例：'2016-08-20'",
             "",
             "picked = details",
             "if TRADES_SINCE:",
             "    cut = datetime.datetime.strptime(TRADES_SINCE, '%Y-%m-%d').date()",
             "    picked = [d for d in details if d.trade.entry_date >= cut]",
             "    print(f'（只列出 {cut} 之後進場的交易）\\n')",
             "print(format_trade_details(picked))",
             "",
             "rets = [d.trade.ret for d in picked]",
             "wins = sum(1 for r in rets if r > 0)",
             "dds = [d.max_drawdown for d in picked]",
             "print(f'共 {len(rets)} 筆　勝 {wins} 敗 {len(rets) - wins}'",
             "      f'（勝率 {wins / len(rets):.0%}）')",
             "print(f'權益報酬：中位數 {sorted(rets)[len(rets) // 2]:+.1%}　'",
             "      f'最佳 {max(rets):+.1%}　最差 {min(rets):+.1%}')",
             "print(f'單筆期間最大回撤：中位數 {sorted(dds)[len(dds) // 2]:.1%}　'",
             "      f'最深 {min(dds):.1%}')"),

        md("## 9. FinLab 回測 → `report.display()`",
           "",
           "把算好的期貨部位淨值交給 `sim()` 產生互動報表。兩個必要的繞法：",
           "",
           "1. FinLab 內建的台股市場找不到 TXF，`report.display()` 組 positionConfig 會失敗",
           "   —— 自訂一個市場類別，價格一律回傳我們的淨值序列。",
           "2. FinLab 的 `position` 日期是**訊號日**、次一交易日成交，權重要往前挪一天。",
           "",
           "> 成本已含在淨值裡（第 7 節），所以這裡 `fee_ratio=0`；",
           "> `report.trade_at = 'close'` 是讓 `display()` 能序列化的顯示層標籤，不影響數值。"),
        code("import numpy as np",
             "from finlab.backtest import sim",
             "from finlab.markets.tw import TWMarket",
             "",
             "SYMBOL = 'TXF'",
             "di = pd.to_datetime([b.d for b in bars])",
             "price = pd.DataFrame({SYMBOL: nav}, index=di)",
             "",
             "class TWFuturesMarket(TWMarket):",
             "    \"\"\"自訂市場：價格一律回傳我們算好的期貨部位淨值。\"\"\"",
             "",
             "    @staticmethod",
             "    def get_name():",
             "        return 'tw_futures'",
             "",
             "    @staticmethod",
             "    def get_asset_id_to_name():",
             "        return {SYMBOL: '台指期連續合約'}",
             "",
             "    def get_price(self, trade_at_price, adj=True):",
             "        if isinstance(trade_at_price, (pd.DataFrame, pd.Series)):",
             "            return pd.DataFrame(trade_at_price)",
             "        return price",
             "",
             "# FinLab 的 position 日期是「訊號日」、次一交易日成交，所以權重往前挪一天",
             "pos = np.zeros(len(bars))",
             "for e in entries:",
             "    end = (e.exit_i - 1) if e.exit_i is not None else len(bars)",
             "    pos[max(e.entry_i - 1, 0):max(end, 0)] = 1.0",
             "",
             "report = sim(pd.DataFrame({SYMBOL: pos}, index=di), trade_at_price=price,",
             "             position_limit=1, fee_ratio=0.0, tax_ratio=0.0,",
             "             market=TWFuturesMarket(),",
             "             name=f'台指期快速修復 {PRESET}（槓桿≤{MAX_LEVERAGE:g}x）',",
             "             upload=False)",
             "report.trade_at = 'close'          # 讓 display() 能序列化",
             "report.display()"),

        md("## 10. 與買進持有對照",
           "",
           "**這張表是判斷策略有沒有價值的關鍵。** 策略與對照組使用同一套指標公式。",
           "淨值用自己算的 `nav`，不用 `report.creturn` —— 後者從**第一筆交易**起算，",
           "期初空手的策略 CAGR 會被灌水。"),
        code("def series_metrics(s):",
             "    ret = s.pct_change().dropna()",
             "    years = (s.index[-1] - s.index[0]).days / 365.25",
             "    total = float(s.iloc[-1] / s.iloc[0] - 1)",
             "    mdd = float((s / s.cummax() - 1).min())",
             "    cagr = (1 + total) ** (1 / years) - 1",
             "    down = ret[ret < 0].std()",
             "    return dict(CAGR=cagr, 總報酬=total, 最大回檔=mdd,",
             "                Sharpe=float(ret.mean() / ret.std() * np.sqrt(252)),",
             "                Sortino=float(ret.mean() / down * np.sqrt(252)) if down else np.nan,",
             "                Calmar=cagr / abs(mdd) if mdd else np.nan)",
             "",
             "idx_series = pd.Series([b.close for b in bars], index=di)",
             "fut_series = pd.Series(fseries, index=di)",
             "eq = pd.Series(nav, index=di)",
             "rows = {f'策略（槓桿≤{MAX_LEVERAGE:g}x）': series_metrics(eq),",
             "        '買進持有 台指期（已還原換倉）': series_metrics(fut_series),",
             "        '加權指數（價格指數）': series_metrics(idx_series)}",
             "",
             "table = pd.DataFrame(rows).T",
             "for c in ('CAGR', '總報酬', '最大回檔'):",
             "    table[c] = table[c].map('{:.1%}'.format)",
             "for c in ('Sharpe', 'Sortino', 'Calmar'):",
             "    table[c] = table[c].map('{:.2f}'.format)",
             "table"),

        md("## 11. 停損假設 vs 實際",
           "",
           "槓桿是按「距離停損 X% × L = 風險預算」定的，但實際虧損常大於假設：",
           "訊號在收盤才判定（收盤已跌過線），且期貨與指數的 15 分鐘差內價格會續走。",
           "這就是 5 倍上限仍出現單筆 −22% 的原因。"),
        code("worst = min(detail, key=lambda t: t.ret)",
             "print(f'單筆最差：{worst.entry_date} → {worst.exit_date}　'",
             "      f'槓桿 {worst.leverage:.2f}x　權益 {worst.ret:.1%}\\n')",
             "i_of = {b.d: i for i, b in enumerate(bars)}",
             "for t in sorted(detail, key=lambda t: t.ret)[:5]:",
             "    a = i_of[t.entry_date]",
             "    b = i_of[t.exit_date] if t.exit_date else len(bars) - 1",
             "    real = (bars[b].close - bars[a].close) / bars[a].close",
             "    ratio = abs(real) / t.stop_distance if t.stop_distance else 0",
             "    print(f'  {t.entry_date}  假設 -{t.stop_distance:.2%} → 實際 {real:+.2%}'",
             "          f'（{ratio:.1f} 倍）　槓桿 {t.leverage:.1f}x → 權益 {t.ret:.1%}')"),

        md("## 12.（選配）逆勢濾網與固定槓桿",
           "",
           "對應 dist 腳本的 `--defensive` 與 `--fixed-leverage`：",
           "只做進場日收盤**低於** MA200 的訊號（逆勢策略，深回檔才是報酬最好的場景），",
           "並把所有部位改成固定 3 倍。這是 docs/strategy.md §18 討論的變體，",
           "不影響上面主結果。"),
        code("from tw_backdraw.futures import fixed_leverage, trend_filter",
             "",
             "ent_d = trend_filter(entries, bars, 200, below=True)",
             "print(f'逆勢濾網：只做收盤低於 MA200 的訊號　{len(entries)} → {len(ent_d)} 筆')",
             "ent_d = fixed_leverage(ent_d, 3.0)",
             "nav_d, detail_d = vehicle_series(dates, fseries, ent_d, FuturesCost(),",
             "                                 [b.close for b in bars])",
             "rows_d = {'現行（停損距離定槓桿≤5x）': series_metrics(eq),",
             "          '逆勢 MA200 ＋固定 3x': series_metrics(pd.Series(nav_d, index=di))}",
             "table_d = pd.DataFrame(rows_d).T",
             "for c in ('CAGR', '總報酬', '最大回檔'):",
             "    table_d[c] = table_d[c].map('{:.1%}'.format)",
             "for c in ('Sharpe', 'Sortino', 'Calmar'):",
             "    table_d[c] = table_d[c].map('{:.2f}'.format)",
             "table_d"),

        md("---",
           "## 已知限制",
           "",
           "1. **樣本數少。** 1999 年起約 30 筆訊號，預設參數屬樣本內結果。",
           "2. **停損假設會被跳空與收盤判定放大**，見第 11 節 —— 5 倍上限",
           "   對應的實際單筆最差是 −22%，不是風險預算的 8%。",
           "3. **換倉價差還原依賴期交所次月報價**；缺資料的換倉日退回原始跳動",
           "   （第 5 節會列出是哪幾天，目前無缺漏）。",
           "4. **保證金與強制平倉未建模。** 權益按線性攤算，實務上深度虧損時",
           "   會先收到追繳通知。",
           "",
           "完整討論見 repo 的 `docs/strategy.md` 與 `docs/tx_evaluation.md`。"),
    ]

    return {
        "cells": cells,
        "metadata": {
            "colab": {"provenance": [], "toc_visible": True},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }


def main() -> int:
    rolls = roll_map()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    nb = build(rolls)
    OUT.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    size = OUT.stat().st_size / 1024
    print(f"→ {OUT}（{len(nb['cells'])} cells，{size:.0f} KB，"
          f"換倉價差 {len(rolls)} 筆）")

    # 基本檢查：JSON 可解析、每個 code cell 語法正確
    parsed = json.loads(OUT.read_text(encoding="utf-8"))
    bad = 0
    for i, c in enumerate(parsed["cells"]):
        if c["cell_type"] != "code":
            continue
        # IPython magic（! 與 %）不是合法 Python，檢查前先濾掉
        src = "".join(line for line in c["source"]
                      if not line.lstrip().startswith(("!", "%")))
        try:
            compile(src, f"<cell {i}>", "exec")
        except SyntaxError as exc:
            bad += 1
            print(f"  ✗ cell {i} 語法錯誤：{exc}", file=sys.stderr)
    print("  ✓ 所有 code cell 語法檢查通過" if not bad else f"  ✗ {bad} 個 cell 有問題")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
