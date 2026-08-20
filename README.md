# 台灣指數快速修復回檔入場策略

大跌之後如果**修復得夠快**，行情大概率會回到舊高，而途中的回檔**淺到不值得等**。
本專案把這個觀察寫成一套可回測、可即時執行的規則，操作標的為
**元大台灣50正2（00631L）**，訊號來源為**發行量加權股價指數（TAIEX）**。

完整規則與推導見 **[docs/strategy.md](docs/strategy.md)**。

---

## 30 秒版本

```
訊號   自高點回檔 ≥10% 後，從谷底起 ≤30 個交易日內收盤補回 ≥60% 的跌幅
進場   訊號確認後次一交易日一次買足目標水位，不等回檔
防守   收盤跌破「補回一半」的線（含 0.5% 緩衝）→ 全部出場
       收盤跌破谷底 → 全部出場
出場   前高不賣（「離前高很近，本身不是賣出的理由」）
       創高後啟動移動停利：自波段最高收盤回檔 8% → 出清
       （或改用 MA 棘輪：出場線 = max(前高, MA)，見 --preset balanced）
```

預設參數以「總報酬 ÷ 最大回檔」從 648,000 組中選出。忠於貼文的原始設定
（15 日 / 補回 75% / 分批加碼 / 38.2% 減碼一半）保留在 `--preset post`。

台股當前實例（`P=47742`、`T=39933`）：

| 價位 | 指數 | 動作 |
|---|---|---|
| 前波高點 | 47,742 | 不賣，啟動 8% 移動停利 |
| 主防線 = 警戒線（50%） | 43,837 | 跌破（緩衝 43,618）→ **全部出場** |
| 失效線 | 39,933 | 跌破 → 全部出場 |

---

## 使用方式

策略本體（`tw_backdraw/`、CLI、回測、測試）**只用 Python 標準函式庫**，Python 3.10+ 即可跑。
只有 `scripts/fetch_finlab.py` 需要額外安裝 `finlab`（見下方「抓資料」）。
`data/` 內已附上抓好的日線，所以 clone 下來就能直接回測。

### 產生操作計畫

```bash
python3 -m tw_backdraw plan --peak 47742 --trough 39933 --now 45309 --capital 1000000
```

```
關鍵價位
  61.8% 回補          44,759
  主防線 = 警戒線     43,838   (50.0% 回補位) 收盤跌破 → 全部出場
  容忍緩衝 (-0.5%)  43,618   假跌破區，不動作
  失效線              39,933   收盤跌破 → 清倉

現價（參考）          45,309   補回 69%，距前高 -5.1%，分區：healthy

目標總持股水位    80% 的權益（約 800,000 元）

進場（權重為佔總權益比例）
  底倉          80.0%  ≈ 800,000 元      訊號確認後次一交易日市價買進，不等回檔

出場
  觸及 47,742    → 不賣（前高不是賣出的理由），啟動停利
  移動停利          自波段最高收盤回檔 8%（指數）→ 出清
  收盤 < 43,838   → 全部出場
  收盤 < 39,933   → 全部出場，不留倉
```

改用貼文原意的分批加碼版：加上 `--preset post`。

### 抓資料（FinLab）

主要資料來源是 **FinLab**，需要 API token（從環境變數 `Finlab_API_token` 讀取）：

```bash
python3 -m venv .venv && .venv/bin/pip install finlab
.venv/bin/python scripts/fetch_finlab.py          # 加權指數 + 00631L 一次抓完
```

抓下來的是：

| 檔案 | 來源資料集 | 範圍 |
|---|---|---|
| `data/taiex.csv` | `taiex_total_index:{開盤,最高,最低,收盤}指數` | 1999-01-05 起，6,821 根 |
| `data/00631L.csv` | `etl:adj_{open,high,low,close}`（**還原股價**） | 2014-10-31 起，2,874 根 |

> `taiex_total_index` 的命名容易誤會，實際內容是**發行量加權股價指數**（價格指數，
> 非含息報酬指數），已逐筆與證交所 MI_5MINS_HIST 核對相符。
>
> ETF 一律使用**還原股價**：00631L 於 2026-03-31 做過約 23:1 的分割，
> 未還原的 `price:收盤價` 會在那天出現 −95.7% 的假單日報酬。

沒有 FinLab 帳號時，`scripts/fetch_twse.py` 是不需帳號的備援（直接爬證交所，逐月抓、較慢）：

```bash
python3 scripts/fetch_twse.py taiex --start 199901 --out data/taiex.csv
python3 scripts/fetch_twse.py stock --stock 00631L --start 201410 --out data/00631L.csv
```

### 掃描歷史訊號 → 回測

```bash
python3 -m tw_backdraw scan     --csv data/taiex.csv
python3 -m tw_backdraw episodes --csv data/taiex.csv --since 2005-01-01   # 為什麼沒訊號
python3 -m tw_backdraw backtest --csv data/taiex.csv -v
python3 -m tw_backdraw backtest --csv data/taiex.csv --etf-csv data/00631L.csv -v
```

沒指定 `--etf-csv` 時，會用指數日報酬合成一條 2 倍槓桿淨值（含內扣與波動耗損）來回測。

**1999–2026 全期觸發 21 次訊號**，命中率 12/21、勝率 57%、權益總報酬 +2,232%、
最大回檔 −35.8%。以真實 00631L 還原股價回測（2014 起 7 次訊號）為 +1,028%、回檔 −27.3%。
**但 2010–2019 整整十年是虧的**，獲利集中在 2020 之後——完整檢討見
[docs/strategy.md §9、§11](docs/strategy.md)。

`episodes` 會列出每一段 ≥10% 的回檔以及它為什麼沒觸發。例如 2008 金融海嘯的
−44.5% 那一段，30 日內只補回 19%；2011 歐債的 −24.8% 只補回 33%——都是真的修復太慢。

> 21 筆樣本仍然推不出統計結論，而且預設參數正是從這 21 筆挑出來的。
> 樣本外驗證（1999–2012 選參數、2013–2026 驗收）證實「報酬÷回檔」這個
> 目標函數可以外推，但**個別參數值不該當成真理**——詳見 docs/strategy.md §11。

### 目前部位該做什麼

```bash
python3 -m tw_backdraw status --csv data/taiex.csv --etf-csv data/00631L.csv --capital 1000000
```

```
訊號        2026-07-30 谷底 39,933（自 2026-06-22 高點 47,742 回檔 16.4%），7 個交易日補回 64%，2026-08-10 觸發訊號 @ 44,929
已持有      6 個交易日（劇本有效期 120 日）

目標水位    80.0%（約 800,000 元）
已建立      80.0%（約 800,000 元）　＝ 目標的 100%

已成交
  2026-08-11 買進  80.0% @ ETF 767.14 (指數 45,121)  底倉：訊號確認，不等回檔

目前分區    healthy　劇本正常 —— 回檔就是加碼機會
距前高      -5.1%　距主防線 +3.4%　距警戒線 +3.4%　距失效線 +13.5%

下一步（收盤價判定，次一交易日執行）
  加碼梯已全部處理，不再加碼
  賣     全部　創高後啟動移動停利（自最高收盤回檔 8%）
  賣   100%　收盤 < 43,837（警戒線）
  賣     全部　收盤 < 39,933（失效線）

預期最大損失  跌到出清線 43,837（-3.2%），00631L 約 -6%；依已建立的 80.0% 部位，權益衝擊約 -5.2%
              （跳空直接摜破失效線 39,933 的極端情形：-11.9%，權益衝擊約 -19.0%）
```

`scripts/current_plan.py --capital 1000000` 會一次印出完整計畫加上這份現況。

### Colab notebook（不用 clone repo）

[`notebooks/tw50_2x_finlab.ipynb`](notebooks/tw50_2x_finlab.ipynb) 是可直接上傳到
Colab 執行的完整回測，由上而下跑完即可，只需要 FinLab API token。
策略程式碼是從 `tw_backdraw/` 的原始碼**嵌入**的（不是另寫的簡化版），
內容涵蓋：取資料 → 跑策略 → `sim()` → `report.display()` → 逐筆對齊驗證 →
與買進持有對照 → 目前部位現況 → 參數敏感度。

改了 `tw_backdraw/` 之後用 `python3 scripts/make_notebook.py` 重新產生。

### 用 FinLab `sim()` 產生互動報表

```python
import sys; sys.path.insert(0, "scripts")
from finlab_report import build_report

report = build_report()                  # 預設參數（報酬÷回檔最佳）
report.display()

report = build_report(preset="post")     # 貼文原意
report = build_report(preset="balanced") # MA40 棘輪出場
```

命令列驗證（不開互動介面）：

```bash
.venv/bin/python scripts/finlab_report.py --preset tuned
```

```
交易筆數  自建   7   FinLab   7
總報酬    自建   1028.2%   FinLab   1004.5%
最大回檔  自建    -27.3%   FinLab    -27.3%
  ✓ 7 筆進出場日期全部一致
```

**時點對齊是這裡最容易錯的地方**：FinLab 的 `position` 日期是**訊號日**，
實際成交落在**次一交易日**（trades 表的 `entry_sig_date` 與 `entry_date` 差一天），
而 `tw_backdraw` 的 `Fill.d` 記的是成交日 —— 權重必須往前挪一根 K 才對得上，
否則整套策略會慢一天進出場。`verify_against_engine()` 就是用來擋這個錯的。

兩邊數字的讀法：FinLab trades 表的 `return` 是**個股報酬**，`Trade.ret` 是
**權益報酬**，單一標的下 `權益 ≈ 個股 × 目標水位`（預設 0.8）。總報酬會差 1~2%，
主因是 FinLab 內建價格資料通常比 repo 內的 CSV 多一兩個交易日，未平倉部位的
評價日不同。

### 參數預設組與敏感度測試

```bash
python3 -m tw_backdraw backtest --csv data/taiex.csv --preset tuned
python3 -m tw_backdraw backtest --csv data/taiex.csv --max-bars 25 --repair 0.60 --risk 0.05
```

| `--preset` | 訊號 | 出場 | 筆數 | 勝率 | 總報酬 | 最大回檔 |
|---|---|---|---|---|---|---|
| **`tuned`（預設）** | 30 日 / 60% | trail 8% | 21 | 57% | +2,232% | −35.8% |
| `post` | 15 日 / 75% | trail 8% | 8 | 50% | +31.4% | −20.4% |
| `balanced` | 30 日 / 60% | MA40 棘輪 | 21 | 67% | +316.4% | −26.5% |
| `winrate` | 30 日 / 60% | MA40，**無停損** | 12 | 92% | +26.3% | −22.9% |

`winrate` 是 648,000 組裡勝率最高的一組 —— 它是**靠關掉兩道停損**換來的，
總報酬遠低於預設組。列出來是為了讓「最大化勝率」的後果可以被重現，不是建議值。
完整的 grid search、邊際分析與樣本外驗證見 [docs/strategy.md §11](docs/strategy.md)。

所有參數集中在 [`tw_backdraw/config.py`](tw_backdraw/config.py)。

### 美股版：S&P 500 → UPRO（3x）

```bash
.venv/bin/python scripts/fetch_finlab.py --us                       # ^GSPC + UPRO
.venv/bin/python scripts/finlab_report.py --market us --preset us_tuned --stats
```

| | CAGR | 總報酬 | 最大回檔 | Sharpe | Calmar |
|---|---|---|---|---|---|
| **策略 `us_tuned`（UPRO）** | **20.8%** | 627.2% | **−37.5%** | **0.88** | **0.55** |
| 買進持有 UPRO | 29.2% | 1413.4% | −76.8% | 0.75 | 0.38 |
| 買進持有 SPY | 13.5% | 282.8% | −34.1% | 0.80 | 0.40 |

兩個市場的形狀一致：**CAGR 輸給買進持有槓桿 ETF，但最大回檔砍半、Sharpe 與
Calmar 較佳**。但美股資料只有 10.6 年、8 筆交易，且樣本外驗證顯示台股採用的
「報酬÷回檔」選擇標準**在美股沒有複製成功**。完整討論見
[docs/strategy.md §12](docs/strategy.md)。

---

## 專案結構

```
tw_backdraw/
  config.py      所有可調參數
  bars.py        日線資料結構與 CSV 讀取
  setup.py       「快速修復」訊號辨識與回檔段落診斷
  levels.py      主防線 / 警戒線 / 失效線
  engine.py      進出場狀態機（含部位大小計算、往上加碼的風險縮放）
  leveraged.py   00631L 的 2 倍槓桿淨值模型（含內扣與波動耗損）
  plan.py        訊號 → 可下單的操作計畫
  status.py      進行中部位的現況與下一個觸發點
  futures.py     台指期執行版（連續合約換倉、依停損距離定槓桿、逐筆明細）
  backtest.py    回測與績效統計
  cli.py         plan / scan / episodes / status / backtest 五個指令
scripts/
  fetch_finlab.py  FinLab 日線抓取（主要）
  fetch_twse.py    證交所日線抓取（備援，不需帳號）
  current_plan.py  印出目前這一輪的計畫與現況
  make_notebook.py Colab notebook 產生器（嵌入 tw_backdraw 原始碼）
  grid_search.py   648,000 組參數搜尋 + 邊際分析
  walk_forward.py  前半段選參數、後半段驗收的樣本外測試
  finlab_report.py FinLab sim() 回測，供 report.display() 使用
  tx_data.py       台指期資料載入（指數日線 + 已還原換倉價差的連續序列）
  futures_trades.py 台指期逐筆交易明細（進場條件、槓桿、MFE/MAE/期間回撤）
  fetch_taifex_rolls.py 期交所分月合約收盤價（算換倉價差用）
  make_futures_script.py 單檔可執行的台指期回測腳本產生器
notebooks/         Colab 用的 .ipynb
dist/              單檔可執行的腳本（tx_futures_backtest.py）
docs/strategy.md   完整策略說明
tests/             單元測試
```

## 測試

```bash
python3 -m unittest discover -s tests -v
```

---

## 免責

這是把一則統計貼文翻譯成可執行規則的研究專案，**不是投資建議**。
00631L 是 2 倍槓桿工具，指數 −1% ≈ ETF −2%，另有波動耗損、內扣與折溢價風險。
貼文中 89% 的另一面是 11%，而那 11% 全是大熊市的開場 —— **失效線必須無條件執行**。
