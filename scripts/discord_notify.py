#!/usr/bin/env python3
"""送一則簡單訊息到 Discord webhook。**不依賴任何第三方套件。**

    python3 scripts/discord_notify.py --title "標題" --text "內文" --color red

給 CI 的失敗通知用 —— 那個時間點 finlab / pandas 可能還沒裝好，
所以這支刻意只用標準函式庫，也刻意與 discord_daily.py 分開。

⚠️ 一定要帶 User-Agent。Discord 會對 urllib 的預設 UA（`Python-urllib/x.y`）
回 403 Forbidden，症狀是本機測試正常、放到 CI 就失敗。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

UA = "tw-backdraw-daily/1.0 (+https://github.com/swhunag65221-pixel/joe_TWbackdraw)"
COLORS = {"red": 0xE74C3C, "green": 0x2ECC71, "yellow": 0xF1C40F,
          "blue": 0x3498DB, "grey": 0x95A5A6}


def send(webhook: str, title: str, text: str, colour: int) -> None:
    body = {"embeds": [{"title": title, "description": text, "color": colour}]}
    req = urllib.request.Request(
        webhook, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord 回應 {r.status}")


def main() -> int:
    ap = argparse.ArgumentParser(description="送一則訊息到 Discord")
    ap.add_argument("--webhook", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--text", default="")
    ap.add_argument("--color", default="grey", choices=sorted(COLORS))
    args = ap.parse_args()

    if not args.webhook:
        print("沒有 webhook，略過通知")
        return 0
    try:
        send(args.webhook, args.title, args.text, COLORS[args.color])
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"通知送出失敗：{exc}", file=sys.stderr)
        return 1
    print("通知已送出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
