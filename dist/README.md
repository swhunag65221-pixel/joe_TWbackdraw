# 單檔可執行的回測腳本

`tx_futures_backtest.py` —— 台指期版，不需 clone repo。
Colab 上執行請改用 [`notebooks/tx_futures_finlab.ipynb`](../notebooks/tx_futures_finlab.ipynb)（同一份程式碼與資料，結果一致）。

```bash
pip install finlab pandas
export Finlab_API_token=你的token      # Windows: set Finlab_API_token=...
python tx_futures_backtest.py
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--max-leverage` | 5 | 槓桿上限 |
| `--preset` | tuned | 參數組：tuned / post / balanced / winrate |
| `--trades-since` | 無 | 印出這天之後每筆的完整明細（見下） |
| `--next-day-fill` | 關 | 改用隔一個交易日收盤成交（預設為當天期貨收盤） |
| `--html` | tx_futures_report.html | 互動報表輸出路徑 |
| `--workdir` | .tw_backdraw_pkg | 策略程式碼展開位置 |

腳本會印出逐筆交易、槓桿分布、績效對照，並產生可用瀏覽器開啟的互動報表。

加上 `--trades-since 2016-08-20` 會再印出該日之後每一筆的完整明細：買賣日期、
持有天數、當初的進場條件（前高／谷底／回檔幅度／修復天數與比例／訊號日指數與
距前高）、警戒線與失效線、距離停損 %、據此算出的槓桿，以及權益報酬、最大報酬
（MFE）、最大不利（MAE）、期間最大回撤與出場原因。

repo 內的等價指令是 `python3 scripts/futures_trades.py --since 2016-08-20`。

由 `scripts/make_futures_script.py` 產生（策略程式碼從 `tw_backdraw/` 逐字嵌入，
換倉價差嵌入 332 筆歷史值，新的換倉日會自動向期交所補抓）。改了模組請重新產生。
