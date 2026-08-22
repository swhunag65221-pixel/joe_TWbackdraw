#!/usr/bin/env python3
"""明日作戰表 → Discord。**前一晚執行。**

    export Finlab_API_token=...
    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    python3 scripts/discord_daily.py                 # 送出
    python3 scripts/discord_daily.py --dry-run       # 只印出，不送
    python3 scripts/discord_daily.py --only-if-action  # 沒有可能觸發的點位就不吵

為什麼改成晚上跑
----------------
訊號以**加權指數 13:30 收盤**判定，成交在**當日台指期 13:45 收盤** ——
中間只有 15 分鐘。收盤後才算、才通知、才下單，時間根本不夠，
而且雲端排程的延遲完全不可控。

但這些觸發點位**今晚就能全部算出來**：停損線、失效線、進場觸發價、
均線交叉價，全部由已經收盤的資料決定，明天不會變。
唯一會動的是移動停利線（明天創新高才會往上移），而它只會對你有利。

所以流程改成：**今晚拿到點位表 → 明天 13:30 只需比對收盤價 → 13:45 前下單。**
明天不必再跑任何程式，也不必等通知。
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
                 as_of: date | None = None, note: str = ""):
        self.cfg = cfg
        self.max_leverage = max_leverage
        self.note = note
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
    def tomorrow_plan(self) -> tuple[str, str, int]:
        """明天 13:30 要比對的點位與對應動作。回傳 (標題, 內容, 顏色)。

        這裡列出的每一個價位都由**已收盤**的資料決定，明天不會變動 ——
        所以今晚就能定案，明天只要比對收盤價。
        """
        c = self.last.close
        t = self.open_trade
        cfg = self.cfg

        if t is not None:
            lv = t.levels
            stop, why = self.trail_stop(t)
            # 兩條出場線同時存在時，較**高**的那條會先被碰到
            binding = max(lv.stop_line, stop) if stop is not None else lv.stop_line
            mark = lambda x: "　←　**先碰到這條**" if x == binding else ""
            rows = [
                f"🔴 收盤 **< {lv.stop_line:,.0f}**（{pct(lv.stop_line / c - 1)}）"
                f"　→　13:45 前**全數平倉**{mark(lv.stop_line)}",
            ]
            if stop is not None:
                rows.append(
                    f"🔴 收盤 **< {stop:,.0f}**（{pct(stop / c - 1)}）"
                    f"　→　移動停利，**全數平倉**{mark(stop)}")
                rows.append(
                    f"　　_{why}明天若收更高，這條線跟著上移到「新高 ×"
                    f" {1 - cfg.exit.trail_drawdown:.2f}」，只會對你有利。_")
            else:
                need = t.setup.peak
                rows.append(
                    f"⬆️ 收盤 **> {need:,.0f}**（{pct(need / c - 1)}）"
                    f"　→　創前高，**啟動移動停利**"
                    f"（自此以波段最高收盤回檔 {cfg.exit.trail_drawdown:.0%} 出場）")
            rows.append(f"🟡 以上都沒發生　→　**續抱，不動作**")
            colour = YELLOW if c / lv.stop_line - 1 < 0.02 else GREEN
            return ("📋 明日作戰表（持有中）", "\n".join(rows), colour)

        w = self.live
        if w is not None and w.state == "drawdown":
            trig = w.trigger_close(cfg.setup)
            if w.bars_left <= 0:
                return ("📋 明日作戰表（空手）",
                        f"⚪ 修復視窗已用盡（{cfg.setup.max_repair_bars} 日），"
                        f"這一段回檔作廢。\n"
                        f"明天起重新以區間最高點為錨，**不會有進場訊號**。", GREY)
            lev = futures_leverage(
                trig, build_levels(w.peak, w.trough, cfg.levels), cfg,
                self.max_leverage)
            lv = build_levels(w.peak, w.trough, cfg.levels)
            rows = [
                f"🟢 收盤 **≥ {trig:,.0f}**（{pct(trig / c - 1)}）"
                f"　→　13:45 前**買進，槓桿約 {lev:.2f}x**"
                f"（每 {trig * MTX_POINT / lev:,.0f} 元 1 口小台）",
                f"　　_進場後停損線 {lv.stop_line:,.0f}、失效線 {lv.invalidation:,.0f}，"
                f"兩條都已固定，不隨進場價變動。_",
                f"　　_收得越高、離停損越遠，槓桿會自動降低："
                + "；".join(f"收 {x:,.0f} → {futures_leverage(x, lv, cfg, self.max_leverage):.2f}x"
                            for x in (trig, trig * 1.01, trig * 1.02)) + "。_",
                f"🔻 收盤 **< {w.trough:,.0f}**（{pct(w.trough / c - 1)}）"
                f"　→　破底，谷底與計時**全部重來**，觸發價跟著下移",
                f"⚪ 介於兩者之間　→　**不動作**，視窗剩 **{w.bars_left - 1}** 個交易日",
            ]
            colour = GREEN if trig / c - 1 < 0.01 else BLUE
            return ("📋 明日作戰表（空手・追蹤中）", "\n".join(rows), colour)

        if w is not None and w.state == "expired":
            return ("📋 明日作戰表（空手）",
                    f"⚪ 上一段回檔已過期。等收盤重新站上 **{w.peak:,.0f}**"
                    f"（{pct(w.peak / c - 1)}）才會重新開始追蹤。\n明天無動作。", GREY)

        anchor = w.peak if w else c
        start = anchor * (1 - cfg.setup.min_drawdown)
        return ("📋 明日作戰表（空手）",
                f"⚪ 目前沒有在追蹤的回檔，明天**不會有進場訊號**。\n"
                f"🔻 收盤 **< {start:,.0f}**（{pct(start / c - 1)}）"
                f"　→　自高點 {anchor:,.0f} 回檔滿 {cfg.setup.min_drawdown:.0%}，"
                f"開始追蹤新的一段（那天仍不進場）", GREY)

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

    def today(self) -> str:
        """今天收盤該做的事（若你今天沒跑腳本，這裡補告訴你）。"""
        fired = self.fired_today
        if fired is not None:
            lv = build_levels(fired.peak, fired.trough, self.cfg.levels)
            lev = futures_leverage(self.last.close, lv, self.cfg, self.max_leverage)
            return (f"🟢 **今天收盤觸發進場訊號** —— 若尚未建立部位，"
                    f"明天開盤補進（槓桿 {lev:.2f}x），但進場價會與訊號日不同，"
                    f"風險略高於回測假設。\n"
                    f"{fired.peak_date} 高點 {fired.peak:,.0f} → {fired.trough_date} "
                    f"谷底 {fired.trough:,.0f}（回檔 {fired.drop_pct:.1%}），"
                    f"{fired.bars_to_repair} 日補回 {fired.repair_fraction:.0%}")
        if self.closed_today:
            t = self.closed_today
            return (f"🔴 **今天收盤出場** —— {t.exit_reason}\n"
                    f"本筆權益報酬 **{t.trade.ret:+.1%}**"
                    f"（持有 {t.bars_held} 個交易日）")
        t = self.open_trade
        if t is not None:
            lv = t.levels
            c = self.last.close
            zone = {"breakout": "已站上前高", "healthy": "劇本正常",
                    "buffer": "主防線假跌破容忍區", "caution": "已跌破主防線",
                    "warning": "已跌破警戒線", "invalidated": "已跌破谷底"}[lv.zone(c)]
            return f"沒有動作，續抱。分區：{zone}。"
        return "沒有動作，空手。"

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
        cross = self.ma_cross_tomorrow()
        if cross is not None:
            side = "跌破" if self.core_on else "站上"
            line += (f"\n明日交叉價 **{cross:,.0f}**（{pct(cross / c - 1)}）"
                     f"　→　收在這之{'下' if self.core_on else '上'}就{side}")
        if self.open_trade is not None:
            return line + ("\n→ 目前有策略部位，**核心部位不啟用**"
                           "（核心只在策略空手時填補曝險）")
        state = "**持有中**" if self.core_on else "**空手**"
        return line + f"\n→ 策略空手，核心 {CORE_LEVERAGE:g}x {state}"

    def ma_cross_tomorrow(self) -> float | None:
        """明天恰好站上／跌破均線的收盤價。

        明日均線 = (前 N-1 根收盤和 + 明日收盤) / N，
        所以「明日收盤 > 明日均線」等價於「明日收盤 > 前 N-1 根收盤和 / (N-1)」——
        今晚就能算出確切的交叉價，不必等明天。
        """
        if len(self.ic) < CORE_MA:
            return None
        return sum(self.ic[-(CORE_MA - 1):]) / (CORE_MA - 1)

    def has_action(self) -> bool:
        """明天是否有實際可能被觸發的點位（用於 --only-if-action）。"""
        if self.fired_today or self.opened_today or self.closed_today:
            return True
        t = self.open_trade
        if t is not None:
            c = self.last.close
            stop, _ = self.trail_stop(t)
            if stop is not None and c / stop - 1 < 0.03:
                return True
            return c / t.levels.stop_line - 1 < 0.03
        w = self.live
        if w and w.state == "drawdown":
            trig = w.trigger_close(self.cfg.setup)
            return trig / self.last.close - 1 < 0.03 or w.bars_left <= 3
        return False

    # ---- Discord payload ----
    def payload(self) -> dict:
        title, body, colour = self.tomorrow_plan()
        fields = [
            {"name": "① 明天 13:30 比對收盤價", "value": body, "inline": False},
            {"name": "② 今天發生了什麼", "value": self.today(), "inline": False},
            {"name": "③ 部位現況", "value": self.position(), "inline": False},
            {"name": "④ 劇本追蹤", "value": self.script(), "inline": False},
            {"name": f"⑤ 核心部位（MA{CORE_MA} 濾網）", "value": self.core(),
             "inline": False},
        ]
        foot = (f"點位由已收盤資料決定，明天不會變　|　"
                f"下單窗口 13:30–13:45　|　參數組 tuned　"
                f"槓桿上限 {self.max_leverage:g}x")
        if self.missing:
            foot += f"　|　⚠️ {len(self.missing)} 個換倉日缺次月報價"
        return {"embeds": [{
            "title": f"{title}",
            "description": (f"{self.note}\n" if self.note else "")
                           + f"依據 **{self.last.d}** 收盤 "
                             f"**{self.last.close:,.2f}**　→　下一個交易日的作法",
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
                    help="依據某一天的收盤回放（驗證用）")
    ap.add_argument("--note", default="",
                    help="在訊息開頭加一行提示，例如標明這是回放而非即時訊號")
    args = ap.parse_args()

    login()
    as_of = (date.fromisoformat(args.as_of) if args.as_of else None)
    note = args.note or ("⚠️ **這是回放，不是即時訊號**" if as_of else "")
    d = Daily(PRESETS[args.preset], args.max_leverage, as_of, note)

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
