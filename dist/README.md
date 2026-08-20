# 單檔可執行的回測腳本

`tx_futures_backtest.py` —— 台指期版，不需 clone repo。

```bash
pip install finlab pandas
export Finlab_API_token=你的token      # Windows: set Finlab_API_token=...
python tx_futures_backtest.py
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--max-leverage` | 5 | 槓桿上限 |
| `--preset` | tuned | 參數組：tuned / post / balanced / winrate |
| `--html` | tx_futures_report.html | 互動報表輸出路徑 |
| `--workdir` | .tw_backdraw_pkg | 策略程式碼展開位置 |

腳本會印出逐筆交易、槓桿分布、績效對照，並產生可用瀏覽器開啟的互動報表。

由 `scripts/make_futures_script.py` 產生（策略程式碼從 `tw_backdraw/` 逐字嵌入，
換倉價差嵌入 332 筆歷史值，新的換倉日會自動向期交所補抓）。改了模組請重新產生。
