# 每日訊號 → Discord

```bash
export Finlab_API_token=你的token
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

python3 scripts/discord_daily.py              # 送出
python3 scripts/discord_daily.py --dry-run    # 只印出
python3 scripts/discord_daily.py --only-if-action   # 沒事就不吵
python3 scripts/discord_daily.py --dry-run --as-of 2026-08-10   # 回放某一天
```

## 訊息長什麼樣

四個區塊，**只有第一塊是要執行的動作**：

```
🟢 進場　2026-08-10
加權指數收盤 44,928.76

── ① 今天要做什麼 ──
今日收盤買進台指期，槓桿 2.74x（每 819,089 元 1 口小台）
訊號：2026-06-22 高點 47,742 → 2026-07-30 谷底 39,933（回檔 16.4%），7 日補回 64%
停損線 43,618（-2.92%）　失效線 39,933

── ② 部位現況 ──   進場日、槓桿、目前損益、MFE/MAE、距各條線多遠
── ③ 劇本追蹤 ──   空手時追到哪一段回檔、收盤要站上多少才觸發、視窗剩幾天
── ④ 核心部位 ──   MA200 濾網的狀態（策略有部位時不啟用）
```

狀態與顏色：

| | 意義 |
|---|---|
| 🟢 進場 | 今日收盤買進 |
| 🔴 出場 | 今日收盤全數平倉（跌破停損線或移動停利線） |
| 🟠 貼近停損 | 續抱，但距停損線不到 1% |
| 🟡 續抱 | 沒有動作 |
| ⚪ 空手觀望 | 沒有動作，附「收盤站上多少才觸發」 |

## ⚠️ 時間窗：13:30 – 13:45

加權指數 13:30 收盤，台指期 13:45 收盤。整套回測都建立在
「**指數收盤判定訊號 → 當天期貨收盤成交**」這個假設上
（`docs/futures_trades_2014_2026.md`）。所以腳本要在 13:30 之後跑，
而且下單要在 13:45 之前完成 —— **只有 15 分鐘**。

這個窗口太窄，不適合交給雲端排程。實測上 GitHub Actions 的 cron 常有
數分鐘到數十分鐘的延遲，趕不上 13:45。因此：

- **要照訊號下單** → 用自己的機器排 cron（下方），或人工在 13:31 手動跑一次。
- **只要紀錄與備援** → 用 `.github/workflows/daily-signal.yml`。

本機 cron（台北時區的機器）：

```cron
31 13 * * 1-5  cd /path/to/joe_TWbackdraw && \
  Finlab_API_token=xxx DISCORD_WEBHOOK_URL=yyy \
  .venv/bin/python scripts/discord_daily.py >> /tmp/tw-daily.log 2>&1
```

若改用 `--next-day-fill` 的隔日成交假設，就沒有這個時間壓力
（但回測顯示隔日成交的績效較差 —— 跳空正是把「假設停損 1.5%」
放大成「實際虧損 7.8%」的主因）。

## GitHub Actions 設定

到 repo 的 Settings → Secrets and variables → Actions 建立兩個 secret：

| Secret | 內容 |
|---|---|
| `FINLAB_API_TOKEN` | FinLab 的 API token |
| `DISCORD_WEBHOOK_URL` | Discord 頻道的 webhook URL |

Discord webhook 的取得：頻道設定 → 整合 → Webhook → 新增 Webhook → 複製網址。

workflow 排了兩次（13:31 與 14:05 台北），第二次是在期貨收盤後定稿；
也可以在 Actions 頁面手動觸發，並指定 `as_of` 回放任一天。

## 為什麼「訊號當天」要另外處理

回測引擎的買單掛在訊號日收盤、**隔一根 K 才成交**，所以訊號當天那一根
還沒有成交紀錄。期貨版是當日期貨收盤成交，訊號日就是下單日 ——
若只看引擎的成交紀錄，最該行動的那一天反而會顯示「沒有動作」。

腳本因此另外用 `detect_setups()` 檢查「最後一筆訊號的觸發日是不是今天」，
並用 `build_levels()` + `futures_leverage()` 當場算出槓桿與停損線。
`--as-of` 就是為了驗證這條路徑而存在的：

```bash
python3 scripts/discord_daily.py --dry-run --as-of 2022-11-15   # 應顯示 🟢 進場 2.11x
python3 scripts/discord_daily.py --dry-run --as-of 2026-07-17   # 應顯示 🔴 出場（移動停利）
python3 scripts/discord_daily.py --dry-run --as-of 2021-08-19   # 應顯示 🔴 出場（停損）
python3 scripts/discord_daily.py --dry-run --as-of 2026-04-07   # 應顯示 ⚪ 空手，差 2.13%
```

## 核心部位

第 ④ 塊回報 `docs/coverage.md` 驗收後保留的唯一改良：**空手時持有 0.5x 核心部位
（指數 > MA200）**。它不影響訊號，只在策略空手時填補曝險，
所以有策略部位時會顯示「不啟用」。若不打算執行這一段，忽略該欄即可。
