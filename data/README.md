# 資料

| 檔案 | 內容 | 範圍 |
|---|---|---|
| `taiex.csv` | 發行量加權股價指數（TAIEX）日 OHLC | 1999-01-05 ~ 2026-08-18，6,821 根 |
| `00631L.csv` | 元大台灣50正2 日 OHLC | 2014-10-31 ~ 2026-08-18，2,873 根 |

兩份都是**公開的證交所交易資料**，此處以 FinLab 取得（`scripts/fetch_finlab.py`），
並已與證交所 `MI_5MINS_HIST` 逐筆核對相符。附在 repo 內是為了讓回測可以直接重現。

重新產生：

```bash
.venv/bin/python scripts/fetch_finlab.py                    # 需要 Finlab_API_token
python3 scripts/fetch_twse.py taiex --start 199901 --out data/taiex.csv   # 不需帳號的備援
```

`raw/` 是 `fetch_twse.py` 的逐月 JSON 快取，已列入 `.gitignore`。

## 欄位格式

```
date,open,high,low,close
2026-08-18,45922.40,46064.09,45225.42,45308.68
```

`tw_backdraw.bars.load_csv()` 只要求 `date` 與 `close`，其餘欄位缺漏時以收盤價補齊。
