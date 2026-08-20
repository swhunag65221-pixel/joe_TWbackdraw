#!/usr/bin/env python3
"""產生可在 Colab 直接執行的 .ipynb。

    python3 scripts/make_notebook.py

策略程式碼直接從 `tw_backdraw/` 的原始碼嵌入，不另外手寫簡化版 ——
notebook 跑的與 repo 回測的是同一份程式。改了模組就重跑這支腳本重新產生。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "notebooks" / "tw50_2x_finlab.ipynb"

# 嵌入順序需符合 import 相依
MODULES = ["bars", "config", "levels", "setup", "leveraged",
           "engine", "backtest", "plan", "status", "__init__"]


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


def build() -> dict:
    cells = [
        md("# 台灣50正2（00631L）快速修復回檔入場策略 — FinLab 回測",
           "",
           "大跌之後如果**修復得夠快**，行情大概率會回到舊高，而途中的回檔**淺到不值得等**。",
           "本 notebook 把這個觀察寫成規則，訊號來自**加權指數**，部位開在 **00631L**，",
           "用 `finlab.backtest.sim()` 回測並產生 `report.display()` 的互動報表。",
           "",
           "**執行方式**：由上而下依序執行即可。需要 FinLab API token。",
           "",
           "---",
           "### 30 秒版本",
           "```",
           "訊號   自高點回檔 ≥10% 後，從谷底起 ≤30 個交易日內收盤補回 ≥60% 的跌幅",
           "進場   訊號確認後次一交易日一次買足目標水位，不等回檔",
           "防守   收盤跌破「補回一半」的線（含 0.5% 緩衝）→ 全部出場",
           "       收盤跌破谷底 → 全部出場",
           "出場   前高不賣；創高後啟動移動停利，自波段最高收盤回檔 8% → 出清",
           "```",
           "",
           "> ⚠️ 這是研究專案，**不是投資建議**。00631L 是 2 倍槓桿工具，",
           "> 指數 −1% ≈ ETF −2%，另有波動耗損、內扣與折溢價風險。",
           "> 回測期間 2014-10 起僅約 7 次訊號，樣本數不足以支撐統計結論。"),

        md("## 1. 安裝套件"),
        code("!pip install -q finlab"),

        md("## 2. 登入 FinLab",
           "",
           "執行後貼上你的 API token（不會顯示在畫面上）。",
           "token 可在 [FinLab 會員頁面](https://ai.finlab.tw/) 取得。"),
        code("import getpass, os, warnings",
             "import finlab",
             "",
             "token = os.environ.get('FINLAB_API_TOKEN') or getpass.getpass('FinLab API token: ')",
             "with warnings.catch_warnings():",
             "    warnings.simplefilter('ignore', DeprecationWarning)",
             "    finlab.login(token)"),

        md("## 3. 寫入策略程式碼",
           "",
           "以下是 `tw_backdraw` 套件的完整原始碼，直接寫成檔案後 import ——",
           "與專案回測使用的是同一份程式，不是簡化版。"),
        code(embed_package()),

        md("## 4. 取得資料",
           "",
           "| 資料 | 來源 | 說明 |",
           "|---|---|---|",
           "| 加權指數 OHLC | `taiex_total_index:*` | 名稱易誤會，實際是**價格指數**，已與證交所核對相符 |",
           "| 00631L | `etl:adj_*` | **還原股價**。00631L 於 2026-03-31 做過約 23:1 分割，用未還原的 `price:收盤價` 會出現 −95.7% 的假單日報酬 |"),
        code("import pandas as pd",
             "from finlab import data",
             "from tw_backdraw.bars import Bar",
             "from tw_backdraw.backtest import align_etf",
             "",
             "SYMBOL = '00631L'",
             "",
             "idx = pd.DataFrame({",
             "    'open':  data.get('taiex_total_index:開盤指數').iloc[:, 0],",
             "    'high':  data.get('taiex_total_index:最高指數').iloc[:, 0],",
             "    'low':   data.get('taiex_total_index:最低指數').iloc[:, 0],",
             "    'close': data.get('taiex_total_index:收盤指數').iloc[:, 0],",
             "}).dropna(subset=['close'])",
             "",
             "etf = pd.DataFrame({",
             "    'close': data.get('etl:adj_close')[SYMBOL],",
             "}).dropna(subset=['close'])",
             "",
             "def to_bars(df):",
             "    return [Bar(d=d.date(), open=float(r.get('open', r['close'])),",
             "                high=float(r.get('high', r['close'])),",
             "                low=float(r.get('low', r['close'])), close=float(r['close']))",
             "            for d, r in df.iterrows()]",
             "",
             "index_bars, etf_prices = align_etf(to_bars(idx), to_bars(etf))",
             "print(f'加權指數 {idx.index.min().date()} ~ {idx.index.max().date()}（{len(idx)} 根）')",
             "print(f'{SYMBOL}   {etf.index.min().date()} ~ {etf.index.max().date()}（{len(etf)} 根）')",
             "print(f'對齊後共 {len(index_bars)} 根，{index_bars[0].d} ~ {index_bars[-1].d}')"),

        md("## 5. 選擇參數組",
           "",
           "| preset | 訊號 | 出場 | 說明 |",
           "|---|---|---|---|",
           "| `tuned` | 30 日 / 補回 60% | 移動停利 8% | **預設**，以「總報酬 ÷ 最大回檔」從 648,000 組中選出 |",
           "| `post` | 15 日 / 補回 75% | 移動停利 8% | 忠於原始貼文：分批加碼、38.2% 減碼一半 |",
           "| `balanced` | 30 日 / 補回 60% | MA40 棘輪 | 出場線 = max(前高, MA40)，勝率較高、回檔較小 |",
           "| `winrate` | 30 日 / 補回 60% | MA40，**無停損** | 全網格勝率最高（靠關掉風控換來的），列出僅供對照 |"),
        code("from tw_backdraw.config import PRESETS",
             "",
             "PRESET = 'tuned'      # ← 想換參數組改這裡",
             "cfg = PRESETS[PRESET]",
             "",
             "print(f'訊號   回檔 ≥{cfg.setup.min_drawdown:.0%}，谷底起 ≤{cfg.setup.max_repair_bars} 日'",
             "      f'內補回 ≥{cfg.setup.repair_fraction:.0%}')",
             "print(f'進場   底倉 {cfg.entry.base_weight:.0%} 的目標水位')",
             "print(f'防守   跌破警戒線（{cfg.levels.warn_line_ratio:.1%} 回補位）→ '",
             "      f'賣出 {cfg.exit.warn_derisk_fraction:.0%}')",
             "print(f'出場   {cfg.exit.exit_mode}，移動停利 {cfg.exit.trail_drawdown:.0%} / '",
             "      f'MA{cfg.exit.ma_period}')"),

        md("## 6. 跑策略，產生 FinLab 要的 position",
           "",
           "**時點對齊是這裡最容易錯的地方**：FinLab 的 `position` 日期是**訊號日**，",
           "實際成交落在**次一交易日**（trades 表裡 `entry_sig_date` 與 `entry_date` 差一天），",
           "而引擎的 `Fill.d` 記的是**成交日** —— 權重必須往前挪一根 K 才對得上，",
           "否則整套策略會慢一天進出場。"),
        code("from tw_backdraw.engine import Engine",
             "from tw_backdraw.backtest import summarize",
             "",
             "result = Engine(cfg).run(index_bars, etf_prices)",
             "own = summarize(result)",
             "print(own.render())",
             "",
             "# 成交紀錄 → 每日目標權重（往前挪一根 K，對齊 FinLab 的訊號日語意）",
             "index_of = {b.d: i for i, b in enumerate(index_bars)}",
             "changes = {}",
             "for t in result.trades:",
             "    for f in t.fills:",
             "        changes.setdefault(max(index_of[f.d] - 1, 0), []).append(f)",
             "",
             "weights, w = [], 0.0",
             "for i, bar in enumerate(index_bars):",
             "    for f in changes.get(i, []):",
             "        w = w + f.weight if f.side == 'buy' else w * (1.0 - f.weight)",
             "    weights.append(0.0 if w < 1e-9 else w)",
             "",
             "dates = pd.to_datetime([b.d for b in index_bars])",
             "position = pd.DataFrame({SYMBOL: weights}, index=dates)",
             "price = pd.DataFrame({SYMBOL: etf_prices}, index=dates)",
             "print(f'\\n在市天數 {(position[SYMBOL] > 0).sum()} / {len(position)}'",
             "      f'（{(position[SYMBOL] > 0).mean():.0%}）')"),

        md("## 7. FinLab 回測 → `report.display()`",
           "",
           "成交價直接餵我們清理過的還原股價，避免 FinLab 內建價格表裡休市日的 NaN 列",
           "讓成交日落空。成本用 ETF 稅率：手續費 0.1425%×0.6 折、證交稅 0.1%（非股票的 0.3%）。",
           "",
           "> 傳 DataFrame 給 `trade_at_price` 之後必須加一行 `report.trade_at = 'close'`，",
           "> 否則 `report.display()` 會丟 `Object of type 'DataFrame' is not JSON serializable`。",
           "> 那只是顯示層的標籤，成交與績效在 `sim()` 當下就算完了，數值不受影響。"),
        code("from finlab.backtest import sim",
             "",
             "report = sim(",
             "    position,",
             "    trade_at_price=price,",
             "    position_limit=1,",
             "    fee_ratio=cfg.cost.buy_cost,",
             "    tax_ratio=cfg.cost.tax_rate,",
             "    name=f'加權指數快速修復 {PRESET}（{SYMBOL}）',",
             "    upload=False,",
             ")",
             "",
             "# trade_at_price 傳 DataFrame 時，FinLab 會把它原封不動存進 report.trade_at，",
             "# 而 report.display() 要序列化 positionConfig 成 JSON 就會丟",
             "# \"Object of type 'DataFrame' is not JSON serializable\"。",
             "# 成交與績效在 sim() 當下已算完，這裡只換掉顯示用的標籤，數值不受影響。",
             "report.trade_at = 'close'",
             "",
             "report.display()"),

        md("## 8. 驗證：FinLab 與內建引擎逐筆對齊",
           "",
           "兩邊的進出場日期應該完全一致。",
           "報酬欄位定義不同：FinLab 的 `return` 是**個股報酬**，引擎的 `Trade.ret` 是",
           "**權益報酬**，單一標的下 `權益 ≈ 個股 × 目標水位`。"),
        code("trades = report.trades.reset_index()",
             "print(f\"{'進場':<12}{'出場':<12}{'引擎(權益)':>12}{'FinLab(個股)':>14}   對齊\")",
             "mismatch = 0",
             "for i, t in enumerate(result.trades):",
             "    if i >= len(trades):",
             "        break",
             "    f = trades.iloc[i]",
             "    f_in = f['entry_date'].date()",
             "    f_out = f['exit_date'].date() if f['exit_date'] == f['exit_date'] else None",
             "    own_in = t.fills[0].d if t.fills else None",
             "    own_out = t.fills[-1].d if len(t.fills) > 1 else None",
             "    ok = (f_in == own_in) and (f_out == own_out)",
             "    mismatch += not ok",
             "    print(f\"{own_in!s:<12}{str(own_out or '持有中'):<12}{t.ret:>12.1%}\"",
             "          f\"{f['return']:>14.1%}   {'✓' if ok else '✗'}\")",
             "print(f\"\\n{'✓ 全部一致' if not mismatch else f'⚠ 有 {mismatch} 筆不一致'}\")"),

        md("## 9. 與買進持有對照",
           "",
           "**這張表是判斷策略有沒有價值的關鍵。** 策略與對照組使用同一套指標公式",
           "（FinLab 的 `daily_sharpe` 定義與此不同，混用會變成蘋果比橘子）。"),
        code("import numpy as np",
             "",
             "def series_metrics(s):",
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
             "lo, hi = position.index[0], position.index[-1]",
             "rows = {f'策略 {PRESET}': series_metrics(report.creturn)}",
             "for b, src in ((SYMBOL, 'etl:adj_close'), ('0050', 'etl:adj_close')):",
             "    s = data.get(src)[b].dropna()",
             "    s = s[(s.index >= lo) & (s.index <= hi)]",
             "    rows[f'買進持有 {b}'] = series_metrics(s)",
             "",
             "table = pd.DataFrame(rows).T",
             "for c in ('CAGR', '總報酬', '最大回檔'):",
             "    table[c] = table[c].map('{:.1%}'.format)",
             "for c in ('Sharpe', 'Sortino', 'Calmar'):",
             "    table[c] = table[c].map('{:.2f}'.format)",
             "table"),

        md("## 10. 目前部位該做什麼",
           "",
           "印出進行中部位的已建立水位、目前分區，以及每一個尚未觸發的加碼／減碼價位。",
           "`--capital` 換算金額，改下面的 `CAPITAL` 即可。"),
        code("from tw_backdraw.status import render_status",
             "",
             "CAPITAL = 1_000_000",
             "print(render_status(result, index_bars, cfg, capital=CAPITAL))"),

        md("## 11. 換參數做敏感度測試",
           "",
           "所有參數都在 `tw_backdraw/config.py`，可以用 `dataclasses.replace` 局部覆寫。"),
        code("from dataclasses import replace",
             "from tw_backdraw.backtest import run_backtest",
             "",
             "print(f\"{'移動停利':>8}{'筆數':>6}{'勝率':>7}{'總報酬':>11}{'最大回檔':>10}{'報酬/回檔':>11}\")",
             "for trail in (0.06, 0.08, 0.10, 0.12, 0.15):",
             "    c = replace(cfg, exit=replace(cfg.exit, trail_drawdown=trail))",
             "    _, st = run_backtest(index_bars, c, etf_prices)",
             "    ratio = st.total_return / abs(st.max_drawdown) if st.max_drawdown else 0",
             "    print(f'{trail:>8.0%}{st.n_trades:>6}{st.win_rate:>7.0%}'",
             "          f'{st.total_return:>11.1%}{st.max_drawdown:>10.1%}{ratio:>11.1f}')"),

        md("---",
           "## 已知限制",
           "",
           "1. **樣本數極少。** 00631L 2014-10 才掛牌，回測期間僅約 7 次訊號。",
           "   預設參數是從這批資料選出來的，屬樣本內結果。",
           "2. **獲利集中。** 以加權指數全期（1999 起）拆解，2010–2019 十年累積僅 0.84x，",
           "   獲利幾乎全部集中在 2020 之後。這組參數本質上是在押注暴力單邊行情。",
           "3. **CAGR 輸給買進持有。** 見第 9 節 —— 策略買的是**風險調整後報酬**",
           "   （回檔砍半、Sharpe 與 Calmar 較佳），不是絕對報酬。",
           "4. **跳空風險。** 「跌破警戒線全部出場」在跳空時無法保證執行。",
           "",
           "完整討論見 repo 的 `docs/strategy.md`。"),
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    nb = build()
    OUT.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    size = OUT.stat().st_size / 1024
    print(f"→ {OUT}（{len(nb['cells'])} cells，{size:.0f} KB）")

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
