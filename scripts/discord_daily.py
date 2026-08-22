#!/usr/bin/env python3
"""每日訊號 → Discord。

    export Finlab_API_token=...
    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    python3 scripts/discord_daily.py                 # 送出
    python3 scripts/discord_daily.py --dry-run       # 只印出，不送
    python3 scripts/discord_daily.py --only-if-action  # 沒事就不吵

每天收盤後跑一次。訊息內容分三塊：

1. **今天要做什麼** —— 只有這一段是動作，放最前面。
2. **部位現況** —— 進場條件、槓桿、停損線、目前損益、距離各條線多遠。
3. **劇本追蹤** —— 空手時追到哪一段回檔、還差多少收盤價才觸發。

一切判定沿用回測那一套：訊號以**加權指數收盤**判定，成交以**當日台指期收盤**
（指數 13:30 收、期貨 13:45 收）。所以這支腳本該在 13:30 之後、13:45 之前跑完。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tx_data import load_tx, login                                  # noqa: E402
from tw_backdraw.config import PRESETS, StrategyConfig              # noqa: E402
from tw_backdraw.engine import Engine, moving_average               # noqa: E402
from tw_backdraw.futures import (FuturesCost, entries_from_trades,  # noqa: E402
                                 futures_leverage, trade_details,
                                 vehicle_series)
from tw_backdraw.levels import build_levels                         # noqa: E402
from tw_backdraw.setup import detect_setups, scan_episodes          # noqa: E402

#: 依 docs/coverage.md 的驗收結論：空手時持有 0.5x 核心，濾網為指數 > MA200
CORE_LEVERAGE = 0.5
CORE_MA = 200
MTX_POINT = 50.0        # 小台每點新台幣

GREEN, BLUE, YELLOW, RED, GREY = 0x2ECC71, 0x3498DB, 0xF1C40F, 0xE74C3C, 0x95A5A6


def pct(x: float) -> str:
    return f"{x:+.2%}"


class Daily:
    def __init__(self, cfg: StrategyConfig, max_leverage: float,
                 as_of: date | None = None):
        self.cfg = cfg
        self.max_leverage = max_leverage
        self.bars, self.fs, self.missing = load_tx()
        if as_of is not None:            # 回放歷史某一天，用來驗證訊息內容
            keep = [i for i, b in enumerate(self.bars) if b.d <= as_of]
            if not keep:
                raise SystemExit(f"{as_of} 之前沒有資料")
            self.bars = self.bars[:keep[-1] + 1]
            self.fs = self.fs[:keep[-1] + 1]
        self.dates = [b.d for b in self.bars]
        self.ic = [b.close for b in self.bars]
        res = Engine(cfg).run(self.bars, self.fs)
        self.ent = entries_from_trades(res.trades, self.bars, cfg, max_leverage,
                                       same_day=True)
        self.nav, det = vehicle_series(self.dates, self.fs, self.ent,
                                       FuturesCost(), self.ic)
        self.details = trade_details(self.dates, self.nav, self.ent, det)
        live: list = []
        scan_episodes(self.bars, cfg.setup, live)
        self.live = live[0] if live else None
        self.setups = detect_setups(self.bars, cfg.setup)
        self.ma = moving_average(self.bars, CORE_MA)[-1]

    # ---- 狀態判斷 ----
    @property
    def last(self):
        return self.bars[-1]

    @property
    def open_trade(self):
        """目前未平倉的那一筆（沒有就是 None）。"""
        if not self.details:
            return None
        d = self.details[-1]
        return d if d.trade.exit_date is None else None

    @property
    def closed_today(self):
        d = self.details[-1] if self.details else None
        return d if d and d.trade.exit_date == self.last.d else None

    @property
    def fired_today(self):
        """今天收盤剛觸發的訊號。

        不能只看引擎的成交紀錄 —— 引擎的買單掛在訊號日收盤、隔一根 K 才成交，
        所以「訊號當天」那一根還沒有 fill，`open_trade` 會是 None。
        但期貨版是**當日期貨收盤**成交，訊號日就是下單日，非抓出來不可。
        """
        if not self.setups or self.setups[-1].trigger_date != self.last.d:
            return None
        return self.setups[-1]

    @property
    def opened_today(self) -> bool:
        t = self.open_trade
        return bool(t and t.trade.entry_date == self.last.d)

    @property
    def core_on(self) -> bool:
        return self.ma is not None and self.last.close > self.ma

    # ---- 三個區塊 ----
    def action(self) -> tuple[str, str, int]:
        """回傳 (標題, 內容, 顏色)。只有這一段是要執行的動作。"""
        c = self.last.close
        fired = self.fired_today
        if fired is not None:
            lv = build_levels(fired.peak, fired.trough, self.cfg.levels)
            lev = futures_leverage(c, lv, self.cfg, self.max_leverage)
            return ("🟢 進場",
                    f"**今日收盤買進台指期，槓桿 {lev:.2f}x**"
                    f"（每 {self.lot_capital(lev):,.0f} 元 1 口小台）\n"
                    f"訊號：{fired.peak_date} 高點 {fired.peak:,.0f} → "
                    f"{fired.trough_date} 谷底 {fired.trough:,.0f}"
                    f"（回檔 {fired.drop_pct:.1%}），"
                    f"{fired.bars_to_repair} 日補回 {fired.repair_fraction:.0%}\n"
                    f"停損線 **{lv.stop_line:,.0f}**（{pct(lv.stop_line / c - 1)}）"
                    f"　失效線 {lv.invalidation:,.0f}",
                    GREEN)
        if self.opened_today:
            t = self.open_trade
            lots = f"（每 {self.lot_capital(t.trade.leverage):,.0f} 元 1 口小台）"
            return ("🟢 進場", 
                    f"**今日收盤買進台指期，槓桿 {t.trade.leverage:.2f}x**{lots}\n"
                    f"停損線 {t.levels.stop_line:,.0f}"
                    f"（{pct(t.levels.stop_line / c - 1)}），收盤跌破就全數出場。",
                    GREEN)
        if self.closed_today:
            t = self.closed_today
            return ("🔴 出場",
                    f"**今日收盤平倉全部台指期部位**\n"
                    f"原因：{t.exit_reason}\n"
                    f"本筆權益報酬 **{t.trade.ret:+.1%}**"
                    f"（持有 {t.bars_held} 個交易日）",
                    RED)

        t = self.open_trade
        if t is None:
            trig = self.live.trigger_close(self.cfg.setup) if self.live else None
            if trig is not None:
                return ("⚪ 空手觀望",
                        f"沒有動作。收盤站上 **{trig:,.0f}**"
                        f"（{pct(trig / c - 1)}）才觸發進場。",
                        GREY)
            return ("⚪ 空手觀望", "沒有動作，目前沒有在追蹤的回檔劇本。", GREY)

        lv = t.levels
        gap = lv.stop_line / c - 1
        if c < lv.stop_line:
            return ("🔴 出場", f"**收盤已跌破停損線 {lv.stop_line:,.0f}，今日收盤全數出場。**", RED)
        stop, why = self.trail_stop(t)
        if stop is not None and c < stop:
            return ("🔴 出場",
                    f"**收盤已跌破移動停利線 {stop:,.0f}，今日收盤全數出場。**\n{why}",
                    RED)
        if gap > -0.01:
            return ("🟠 貼近停損",
                    f"續抱，但收盤距停損線 {lv.stop_line:,.0f} 只剩 **{pct(gap)}**，"
                    "隨時可能出場。", YELLOW)
        if stop is not None:
            near = " ⚠️ 已很接近" if stop / c - 1 > -0.02 else ""
            return ("🟡 續抱（已創高）",
                    f"沒有動作。{why}\n出場線 **{stop:,.0f}**"
                    f"（{pct(stop / c - 1)}）{near}", GREEN)
        return ("🟡 續抱", f"沒有動作。停損線 {lv.stop_line:,.0f}（{pct(gap)}）", GREEN)

    def trail_stop(self, t):
        """已創高時的移動停利價位。"""
        s = t.setup
        i0 = self.dates.index(t.trade.entry_date)
        hi = max(self.ic[i0:])
        if hi < s.peak:
            return None, ""
        d = self.cfg.exit.trail_drawdown
        return hi * (1 - d), f"波段最高收盤 {hi:,.0f}，移動停利 {d:.0%}。"

    def lot_capital(self, leverage: float) -> float:
        """在這個槓桿下，一口小台需要多少權益。"""
        return self.last.close * MTX_POINT / leverage

    def position(self) -> str:
        t = self.open_trade
        if t is None:
            return "目前空手。"
        s, lv, tr = t.setup, t.levels, t.trade
        c = self.last.close
        stop, why = self.trail_stop(t)
        lines = [
            f"進場 **{tr.entry_date}** @ 指數 {tr.entry_index:,.0f}"
            f"　槓桿 **{tr.leverage:.2f}x**　持有 {t.bars_held} 日",
            f"訊號 {s.peak_date} 高點 {s.peak:,.0f} → {s.trough_date} 谷底 {s.trough:,.0f}"
            f"（回檔 {s.drop_pct:.1%}），{s.bars_to_repair} 日補回 {s.repair_fraction:.0%}",
            f"目前指數 {c:,.0f}　期貨報酬 {tr.futures_return:+.1%}"
            f"　**權益報酬 {tr.ret:+.1%}**",
            f"最大報酬 {t.mfe:+.1%}　最大不利 {t.mae:+.1%}　期間最大回撤 {t.max_drawdown:.1%}",
            f"停損線 **{lv.stop_line:,.0f}**（{pct(lv.stop_line / c - 1)}）"
            f"　失效線 {lv.invalidation:,.0f}（{pct(lv.invalidation / c - 1)}）",
        ]
        if stop is not None:
            lines.append(f"移動停利 **{stop:,.0f}**（{pct(stop / c - 1)}）　{why}")
        return "\n".join(lines)

    def script(self) -> str:
        w = self.live
        c = self.last.close
        if w is None:
            return "沒有資料。"
        if w.state == "engaged":
            return (f"訊號已觸發並在部位中。前高錨點 {w.peak:,.0f}"
                    f"（{w.peak_date}）")
        if w.state == "drawdown":
            trig = w.trigger_close(self.cfg.setup)
            lines = [
                f"追蹤中：{w.peak_date} 高點 {w.peak:,.0f} → {w.trough_date} 谷底 "
                f"{w.trough:,.0f}（回檔 **{w.drop_pct:.1%}**）",
                f"谷底後 {w.bars_since_trough} 日，目前補回 **{w.repair_fraction:.0%}**"
                f"（視窗內最佳 {w.best_in_window:.0%}）",
                f"觸發條件：{self.cfg.setup.max_repair_bars} 日內收盤補回 "
                f"{self.cfg.setup.repair_fraction:.0%}　→　收盤需 ≥ **{trig:,.0f}**"
                f"（{pct(trig / c - 1)}）",
                f"視窗剩 **{w.bars_left}** 個交易日"
                + ("　⚠️ 期限將至" if w.bars_left <= 5 else ""),
            ]
            if w.lower_lows:
                lines.append(f"期間破底 {w.lower_lows} 次（每次都讓計時歸零）")
            return "\n".join(lines)
        if w.state == "expired":
            return f"上一段回檔已過期，等指數重新站上 {w.peak:,.0f} 才重新追蹤。"
        return (f"沒有在追蹤的回檔。目前高點錨點 {w.peak:,.0f}（{w.peak_date}），"
                f"跌破 **{w.peak * (1 - self.cfg.setup.min_drawdown):,.0f}**"
                f"（{pct((1 - self.cfg.setup.min_drawdown) * w.peak / c - 1)}）"
                f"才開始追蹤新的一段。")

    def core(self) -> str:
        c = self.last.close
        if self.ma is None:
            return "資料不足，無法計算均線。"
        above = c / self.ma - 1
        verb = "站上" if self.core_on else "跌破"
        line = (f"MA{CORE_MA} = {self.ma:,.0f}，指數{verb}均線 {above:+.1%}")
        if self.open_trade is not None:
            return line + ("\n→ 目前有策略部位，**核心部位不啟用**"
                           "（核心只在策略空手時填補曝險）")
        state = "**持有中**" if self.core_on else "**空手**"
        return line + f"\n→ 策略空手，核心 {CORE_LEVERAGE:g}x {state}"

    def has_action(self) -> bool:
        """今天是否有需要下單或即將觸發的事。"""
        if self.fired_today or self.opened_today or self.closed_today:
            return True
        t = self.open_trade
        if t is not None:
            c = self.last.close
            stop, _ = self.trail_stop(t)
            if stop is not None and c / stop - 1 < 0.02:
                return True
            return c / t.levels.stop_line - 1 < 0.01
        w = self.live
        if w and w.state == "drawdown":
            trig = w.trigger_close(self.cfg.setup)
            return trig / self.last.close - 1 < 0.01 or w.bars_left <= 3
        return False

    # ---- Discord payload ----
    def payload(self) -> dict:
        title, body, colour = self.action()
        fields = [
            {"name": "① 今天要做什麼", "value": body, "inline": False},
            {"name": "② 部位現況", "value": self.position(), "inline": False},
            {"name": "③ 劇本追蹤", "value": self.script(), "inline": False},
            {"name": f"④ 核心部位（MA{CORE_MA} 濾網）", "value": self.core(),
             "inline": False},
        ]
        foot = (f"訊號以加權指數收盤判定、當日台指期收盤成交　|　"
                f"參數組 tuned　槓桿上限 {self.max_leverage:g}x")
        if self.missing:
            foot += f"　|　⚠️ {len(self.missing)} 個換倉日缺次月報價"
        return {"embeds": [{
            "title": f"{title}　{self.last.d}",
            "description": f"加權指數收盤 **{self.last.close:,.2f}**",
            "color": colour,
            "fields": fields,
            "footer": {"text": foot},
        }]}


def post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "tw-backdraw-daily/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord 回應 {r.status}")


def render_text(p: dict) -> str:
    e = p["embeds"][0]
    out = [e["title"], e["description"], ""]
    for f in e["fields"]:
        out += [f"── {f['name']} ──", f["value"], ""]
    out.append(e["footer"]["text"])
    return "\n".join(out).replace("**", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="每日訊號推送到 Discord")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    ap.add_argument("--preset", default="tuned")
    ap.add_argument("--max-leverage", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true", help="只印出，不送出")
    ap.add_argument("--only-if-action", action="store_true",
                    help="沒有動作也沒有接近觸發時，不送訊息")
    ap.add_argument("--stale-days", type=int, default=5,
                    help="資料落後超過這麼多天就警告")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="回放歷史某一天的訊息（驗證用，會連同 --dry-run 使用）")
    args = ap.parse_args()

    login()
    as_of = (date.fromisoformat(args.as_of) if args.as_of else None)
    d = Daily(PRESETS[args.preset], args.max_leverage, as_of)

    lag = (date.today() - d.last.d).days
    if as_of is None and lag > args.stale_days:
        print(f"⚠️ 資料只到 {d.last.d}（落後 {lag} 天），FinLab 可能尚未更新",
              file=sys.stderr)

    if args.only_if_action and not d.has_action():
        print(f"{d.last.d} 沒有動作也沒有接近觸發，不送出。")
        return 0

    p = d.payload()
    if args.dry_run or not args.webhook:
        if not args.webhook and not args.dry_run:
            print("未設定 DISCORD_WEBHOOK_URL，改為只印出：\n", file=sys.stderr)
        print(render_text(p))
        return 0
    try:
        post(args.webhook, p)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"送出失敗：{exc}", file=sys.stderr)
        return 1
    print(f"已送出 {d.last.d} 的每日訊號。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
