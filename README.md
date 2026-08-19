# 台灣指數快速修復回檔入場策略

大跌之後如果**修復得夠快**，行情大概率會回到舊高，而途中的回檔**淺到不值得等**。
本專案把這個觀察寫成一套可回測、可即時執行的規則，操作標的為
**元大台灣50正2（00631L）**，訊號來源為**發行量加權股價指數（TAIEX）**。

完整規則與推導見 **[docs/strategy.md](docs/strategy.md)**。

---

## 30 秒版本

```
訊號   自高點回檔 ≥10% 後，從谷底起 ≤15 個交易日內收盤補回 ≥75% 的跌幅
進場   底倉 40% 立刻買（不等回檔）；−3% 加 30%；−5% 加 30%；20 日沒回檔就補齊
防守   跌破 50% 回補位 → 停止加碼
       跌破 38.2% 回補位 → 減碼一半
       跌破谷底 → 清倉
目標   回到前波高點 → 賣 1/3，其餘用「自高點回檔 8%」移動停利
```

台股當前實例（`P=47742`、`T=39933`）：

| 價位 | 指數 | 動作 |
|---|---|---|
| 前波高點 | 47,742 | 賣 1/3，其餘轉移動停利 |
| 主防線（50%） | 43,838 | 跌破 → 停止加碼 |
| 警戒線（38.2%） | 42,916 | 跌破 → 減碼一半 |
| 失效線 | 39,933 | 跌破 → 清倉 |

---

## 使用方式

只用 Python 標準函式庫，不需要安裝任何套件（Python 3.10+）。

### 產生操作計畫

```bash
python3 -m tw_backdraw plan --peak 47742 --trough 39933 --now 46200 --capital 1000000
```

```
關鍵價位
  主防線 (50%)        43,838   歷史回檔多數止步於此
  警戒線 (38.2%)      42,916   收盤跌破 → 減碼一半
  失效線              39,933   收盤跌破 → 清倉

目標總持股水位    39% 的權益（約 386,977 元）

進場梯（權重為佔總權益比例）
  底倉          15.5%  ≈ 154,791 元      訊號確認後次一交易日市價買進，不等回檔
  回檔 -3%      11.6%  ≈ 116,093 元      自訊號後波段最高收盤回檔 3%...
  回檔 -5%      11.6%  ≈ 116,093 元      自訊號後波段最高收盤回檔 5%...
  時間補齊        剩餘                    訊號後 20 個交易日仍未觸發回檔梯 → 市價補齊
```

### 抓真實資料 → 掃描歷史訊號 → 回測

```bash
# 證交所加權指數日線，最早到 1999-01-05（逐月抓取並快取於 data/raw/）
python3 scripts/fetch_twse.py taiex --start 199901 --out data/taiex.csv

# 00631L 實際日線（2014-10 掛牌），可選
python3 scripts/fetch_twse.py stock --stock 00631L --start 201410 --out data/00631L.csv

python3 -m tw_backdraw scan --csv data/taiex.csv
python3 -m tw_backdraw backtest --csv data/taiex.csv -v
python3 -m tw_backdraw backtest --csv data/taiex.csv --etf-csv data/00631L.csv
```

沒指定 `--etf-csv` 時，會用指數日報酬合成一條 2 倍槓桿淨值（含內扣與波動耗損）來回測。

### 目前這一輪

```bash
python3 scripts/current_plan.py --capital 1000000
```

有 `data/taiex.csv` 就自動抓最新一次訊號與最新收盤，否則退回貼文中的實例。

### 調參數做敏感度測試

```bash
python3 -m tw_backdraw backtest --csv data/taiex.csv --max-bars 25 --repair 0.60 --risk 0.05
```

所有參數集中在 [`tw_backdraw/config.py`](tw_backdraw/config.py)。

---

## 專案結構

```
tw_backdraw/
  config.py      所有可調參數
  bars.py        日線資料結構與 CSV 讀取
  setup.py       「快速修復」訊號辨識
  levels.py      主防線 / 警戒線 / 失效線
  engine.py      進出場狀態機（含部位大小計算）
  leveraged.py   00631L 的 2 倍槓桿淨值模型（含內扣與波動耗損）
  plan.py        訊號 → 可下單的操作計畫
  backtest.py    回測與績效統計
  cli.py         plan / scan / backtest 三個指令
scripts/
  fetch_twse.py    證交所日線抓取
  current_plan.py  印出目前這一輪的計畫
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
