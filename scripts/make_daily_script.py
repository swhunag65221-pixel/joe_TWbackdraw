#!/usr/bin/env python3
"""產生可在任何 GitHub 帳號的 Action 裡單獨執行的「每日訊號」腳本。

    python3 scripts/make_daily_script.py

輸出兩個檔案，複製到你自己的 repo 根目錄即可：

    dist/tx_daily_signal.py     單一檔案，不需 clone 本 repo
    dist/daily-signal.yml       放到 .github/workflows/，設定兩個 secrets

腳本內容（全部逐字嵌入，與本 repo 同一份程式）：
  * tw_backdraw/ 全部模組（策略、注碼、step-down）
  * scripts/discord_daily.py（每日訊息）與 scripts/discord_notify.py（失敗通知）
  * 獨立版 tx_data.py：換倉價差嵌入歷史值，新的換倉日自動向期交所補抓

執行方式與 repo 內完全相同：
    python3 tx_daily_signal.py --dry-run --full
    python3 tx_daily_signal.py notify --webhook ... --title ... --text ...
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "tx_daily_signal.py"
OUT_YML = ROOT / "dist" / "daily-signal.yml"
sys.path.insert(0, str(ROOT / "scripts"))

MODULES = ["bars", "config", "levels", "setup", "leveraged",
           "engine", "backtest", "plan", "status", "futures", "__init__"]


def embed() -> str:
    parts = ["_SOURCES = {}"]
    for name in MODULES:
        src = (ROOT / "tw_backdraw" / f"{name}.py").read_text(encoding="utf-8")
        parts.append(f"_SOURCES[{'tw_backdraw/' + name + '.py'!r}] = {src!r}")
    for name in ("discord_daily.py", "discord_notify.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        parts.append(f"_SOURCES[{'scripts/' + name!r}] = {src!r}")
    return "\n".join(parts)


TX_DATA = '''#!/usr/bin/env python3
"""台指期資料載入（單檔版）：加權指數日線 + 已還原換倉價差的期貨連續序列。

與 repo 的 scripts/tx_data.py 相同，只是換倉價差改為嵌入歷史值；
嵌入資料沒涵蓋的新換倉日（產生腳本之後才發生的）會自動向期交所補抓。
"""

from __future__ import annotations

import calendar
import os
import sys
import urllib.parse
import urllib.request
import warnings
from datetime import date

from tw_backdraw.bars import Bar
from tw_backdraw.futures import build_continuous, missing_rolls

_ROLL_NEXT_CLOSE = __ROLLS__


def login() -> None:
    import finlab
    token = next((os.environ[k] for k in
                  ("Finlab_API_token", "FINLAB_API_TOKEN", "FINLAB_TOKEN")
                  if os.environ.get(k)), None)
    if not token:
        raise SystemExit("請先設定環境變數 Finlab_API_token")
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


def roll_map(dates: list[date], contract: list[str]) -> dict[date, float]:
    """{換倉日: 次月合約在該日的收盤}：嵌入值 ＋ 向期交所補抓缺的。"""
    rolls = {date.fromisoformat(k): v for k, v in _ROLL_NEXT_CLOSE.items()}
    for miss in missing_rolls(dates, contract, rolls):
        i = dates.index(miss)
        px = fetch_taifex_next_close(miss.isoformat(), contract[i + 1])
        if px:
            rolls[miss] = px
            print(f"  補抓換倉價差 {miss} → {contract[i + 1]} 收 {px:,.0f}")
    return rolls


def load_tx() -> tuple[list[Bar], list[float], list[date]]:
    """回傳 (指數日線, 對齊的期貨連續序列, 缺換倉價差的日期)。"""
    import pandas as pd
    from finlab import data

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

    rolls = roll_map(fdates, contract)
    cont = build_continuous(fdates, front, contract, rolls)
    fut = dict(zip(fdates, cont))

    bars = [Bar(d=d.date(), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]))
            for d, r in idx.iterrows() if d.date() in fut]
    return bars, [fut[b.d] for b in bars], missing_rolls(fdates, contract, rolls)
'''

BODY = '''#!/usr/bin/env python3
"""台指期「快速修復回檔」策略 —— 每日訊號（明日作戰表）→ Discord，單檔可執行。

    pip install finlab pandas tzdata
    export Finlab_API_token=...            # FinLab API token
    export DISCORD_WEBHOOK_URL=...         # Discord webhook
    python3 tx_daily_signal.py             # 送出精簡版
    python3 tx_daily_signal.py --full      # 六個區塊的完整版
    python3 tx_daily_signal.py --dry-run --as-of 2025-05-12 --full   # 回放某一天
    python3 tx_daily_signal.py notify --webhook ... --title ... --text ...   # 失敗通知（只用標準函式庫）

注碼規則（最終套件，docs/final_package.md）
    進場日收盤 < MA200 → 固定 3x；≥ MA200 → 4% ÷ 停損距離、上限 2.5x
    3x 部位權益 +150% → 減碼到 1x（step-down）；空手且收盤 > MA200 → 核心 0.5x
`--sizing risk --no-step-down` 可切回舊版注碼。

程式碼（策略、注碼、訊息）從 repo 逐字嵌入，換倉價差嵌入 __NROLLS__ 筆歷史值，
新換倉日自動向期交所補抓。由 scripts/make_daily_script.py 產生；改了程式請重新產生。
"""

from __future__ import annotations

import os
import pathlib
import sys

__EMBED__


def write_package(dest: pathlib.Path) -> None:
    for rel, src in _SOURCES.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    (dest / "scripts" / "tx_data.py").write_text(_TX_DATA, encoding="utf-8")
    for d in (str(dest / "scripts"), str(dest)):
        if d not in sys.path:
            sys.path.insert(0, d)


def main(argv: list[str]) -> int:
    workdir = pathlib.Path(os.environ.get("TW_BACKDRAW_WORKDIR", ".tw_backdraw_pkg"))
    if argv[:1] == ["--workdir"]:
        workdir, argv = pathlib.Path(argv[1]), argv[2:]
    write_package(workdir)
    if argv[:1] == ["notify"]:
        import discord_notify
        sys.argv = ["discord_notify.py"] + argv[1:]
        return discord_notify.main()
    import discord_daily
    return discord_daily.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

YML = '''name: 明日作戰表 → Discord

# 前一晚執行（12:00 UTC = 20:00 台北）。若 log 顯示「資料只到前一個交易日」，
# 代表 FinLab 尚未更新當日收盤，把 cron 往後調（例如 "0 13 * * 0-4" = 21:00 台北）。
# 所有觸發點位由當天已收盤的資料決定，明天不會變動，排程延遲幾分鐘也無所謂。
#
# 設定：Settings → Secrets and variables → Actions 新增
#   FINLAB_API_TOKEN     FinLab API token
#   DISCORD_WEBHOOK_URL  Discord webhook
# 把 tx_daily_signal.py 放在 repo 根目錄，本檔放到 .github/workflows/。
on:
  schedule:
    - cron: "0 12 * * 0-4"      # 週日至週四晚間 → 產出隔一個交易日的作戰表
  workflow_dispatch:
    inputs:
      as_of:
        description: "依據某一天的收盤回放（YYYY-MM-DD），留空則用最新資料"
        required: false
      full:
        description: "完整版（六個區塊）"
        type: boolean
        default: false
      dry_run:
        description: "只印在 log，不送到 Discord"
        type: boolean
        default: false

concurrency:
  group: daily-signal
  cancel-in-progress: false

jobs:
  plan:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: 檢查 secrets
        env:
          TOKEN: ${{ secrets.FINLAB_API_TOKEN }}
          HOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          missing=""
          [ -z "$TOKEN" ] && missing="$missing FINLAB_API_TOKEN"
          [ -z "$HOOK" ]  && missing="$missing DISCORD_WEBHOOK_URL"
          if [ -n "$missing" ]; then
            echo "::error::缺少 repository secret:$missing"
            exit 1
          fi
          echo "secrets 齊全"

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - run: pip install --quiet finlab pandas tzdata

      - name: 產出明日作戰表
        env:
          Finlab_API_token: ${{ secrets.FINLAB_API_TOKEN }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          python3 tx_daily_signal.py \\
            ${{ inputs.as_of && format('--as-of {0}', inputs.as_of) || '' }} \\
            ${{ inputs.full && '--full' || '' }} \\
            ${{ inputs.dry_run && '--dry-run' || '' }}

      - name: 失敗時通知 Discord
        if: failure()
        env:
          HOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          if [ -z "$HOOK" ]; then
            echo "沒有 webhook，略過通知"
            exit 0
          fi
          python3 tx_daily_signal.py notify \\
            --webhook "$HOOK" --color red \\
            --title "⚠️ 明日作戰表產生失敗" \\
            --text "今晚沒有作戰表。[查看 log]($GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID)
          明天 13:30 請自行比對上一則訊息的點位 —— 停損線與觸發價不會因為這次失敗而改變。"
'''


def main() -> int:
    from make_futures_notebook import roll_map
    rolls = roll_map()
    tx_data_src = TX_DATA.replace("__ROLLS__", json.dumps(rolls, indent=0))
    embed_src = embed() + f"\n\n_TX_DATA = {tx_data_src!r}\n"
    text = (BODY.replace("__NROLLS__", str(len(rolls)))
                .replace("__EMBED__", embed_src))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    OUT_YML.write_text(YML, encoding="utf-8")
    compile(text, str(OUT), "exec")
    compile(tx_data_src, "tx_data.py", "exec")
    print(f"→ {OUT}（{OUT.stat().st_size / 1024:.0f} KB，換倉價差 {len(rolls)} 筆）")
    print(f"→ {OUT_YML}")
    print("  ✓ 語法檢查通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
