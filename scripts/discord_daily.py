#!/usr/bin/env python3
"""明日作戰表 → Discord。**前一晚執行。**

    export Finlab_API_token=...
    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    python3 scripts/discord_daily.py                 # 送出
    python3 scripts/discord_daily.py --dry-run       # 只印出，不送
    python3 scripts/discord_daily.py --only-if-action  # 沒有可能觸發的點位就不吵

注碼規則（docs/strategy.md §20–§22「最終套件」，docs/final_package.md 有完整決策樹）
------------------------------------------------------------------------------
    進場日收盤 < MA200 → 固定 3x（每口小台需要 = 收盤 × 50 ÷ 3）
    進場日收盤 ≥ MA200 → 半預算風險式：4% ÷ 停損距離，上限 2.5x
                          （每口需要 = (收盤 − 停損線) × 1,250）
    3x 部位權益達 +150%（期貨自進場價 +50%）→ 減碼到 1x，之後不加回（step-down）
    空手 → 核心 0.5x，濾網為 MA200 加 ±2% 遲滯緩衝帶：
                          站上 MA200×1.02 才開、跌破 MA200×0.98 才關，帶內不動作
`--sizing risk --no-step-down` 可切回舊版（8% ÷ 停損距離、上限 5x、不減碼）。
`--core-band 0` 可把核心切回逐日 `收盤 > MA200`（docs/coverage.md §5）。

為什麼改成晚上跑
----------------
訊號以**加權指數 13:30 收盤**判定，成交在**當日台指期 13:45 收盤** ——
中間只有 15 分鐘。收盤後才算、才通知、才下單，時間根本不夠，
而且雲端排程的延遲完全不可控。

但這些觸發點位**今晚就能全部算出來**：停損線、失效線、進場觸發價、
均線交叉價、減碼觸發價，全部由已經收盤的資料決定，明天不會變。
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
                                 entry_regime, futures_leverage,
                                 hybrid_entries, ma_band_filter,
                                 trade_details, vehicle_series)
from tw_backdraw.levels import build_levels                         # noqa: E402
from tw_backdraw.setup import detect_setups, scan_episodes          # noqa: E402

#: 依 docs/coverage.md 的驗收結論：空手時持有 0.5x 核心，濾網為指數 > MA200
CORE_LEVERAGE = 0.5
CORE_MA = 200
#: 核心濾網的遲滯緩衝帶（docs/coverage.md §5）。站上 MA200×1.02 才開、
#: 跌破 MA200×0.98 才關，帶內不動作。用 `--core-band 0` 可切回逐日判定。
CORE_BAND = 0.02
#: §20 混合注碼：均線下固定 3x；均線上風險預算砍半（4%）
BELOW_LEV = 3.0
ABOVE_RISK_SCALE = 0.5
#: §22 step-down：權益 +150% → 降到 1x
STEP_GAIN = 1.5
STEP_LEV = 1.0
MTX_POINT = 50.0        # 小台每點新台幣

GREEN, BLUE, YELLOW, RED, GREY = 0x2ECC71, 0x3498DB, 0xF1C40F, 0xE74C3C, 0x95A5A6


def pct(x: float) -> str:
    return f"{x:+.2%}"


class Daily:
    def __init__(self, cfg: StrategyConfig, max_leverage: float,
                 as_of: date | None = None, note: str = "",
                 sizing: str = "hybrid", step_down: bool = True,
                 core_band: float = CORE_BAND):
        self.cfg = cfg
        self.max_leverage = max_leverage
        self.note = note
        self.sizing = sizing
        self.step_down = step_down
        self.core_band = core_band
        self.bars, self.fs, self.missing = load_tx()
        if as_of is not None:            # 回放歷史某一天，用來驗證訊息內容
            keep = [i for i, b in enumerate(self.bars) if b.d <= as_of]
            if not keep:
                raise SystemExit(f"{as_of} 之前沒有資料")
            self.bars = self.bars[:keep[-1] + 1]
            self.fs = self.fs[:keep[-1] + 1]
        self.dates = [b.d for b in self.bars]
        self.ic = [b.close for b in self.bars]
        self.ma_series = moving_average(self.bars, CORE_MA)
        self.ma = self.ma_series[-1]
        # 核心的開關是**有狀態的**（緩衝帶帶內維持原狀），不能只看今天的收盤
        self.core_series = ma_band_filter(self.ic, self.ma_series,
                                          core_band, core_band)

        # 「均線上」規則的風險預算與槓桿上限；均線下固定 BELOW_LEV
        if sizing == "hybrid":
            self.risk = cfg.sizing.risk_per_trade * ABOVE_RISK_SCALE
            self.cap = max_leverage * ABOVE_RISK_SCALE
        else:
            self.risk = cfg.sizing.risk_per_trade
            self.cap = max_leverage

        res = Engine(cfg).run(self.bars, self.fs)
        ent = entries_from_trades(res.trades, self.bars, cfg, max_leverage,
                                  same_day=True)
        if sizing == "hybrid":
            ent = hybrid_entries(ent, self.bars, CORE_MA, BELOW_LEV, ABOVE_RISK_SCALE)
        self.ent = ent
        self.nav, det = vehicle_series(
            self.dates, self.fs, self.ent, FuturesCost(), self.ic,
            step_gain=STEP_GAIN if step_down else None, step_leverage=STEP_LEV)
        self.details = trade_details(self.dates, self.nav, self.ent, det)
        live: list = []
        scan_episodes(self.bars, cfg.setup, live)
        self.live = live[0] if live else None
        self.setups = detect_setups(self.bars, cfg.setup)

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

    def exit_signal_today(self):
        """今天收盤已經跌破出場線 —— 引擎的賣單要隔一根 K 才成交，
        所以 `closed_today` 在訊號當天還是空的，必須另外判斷。"""
        t = self.open_trade
        if t is None:
            return None
        c = self.last.close
        stop, _ = self.trail_stop(t)
        if stop is not None and c < stop:
            return stop, f"移動停利（自波段最高收盤回檔 {self.cfg.exit.trail_drawdown:.0%}）"
        if c < t.levels.stop_line:
            return t.levels.stop_line, "跌破停損線"
        return None

    @property
    def opened_today(self) -> bool:
        t = self.open_trade
        return bool(t and t.trade.entry_date == self.last.d)

    @property
    def core_on(self) -> bool:
        """核心濾網今天是開還是關。緩衝帶為 0 時等同 `收盤 > MA200`。"""
        return self.core_series[-1]

    # ---- 注碼：均線位置、槓桿、每口需要 ----
    def regime_today(self) -> str:
        """今天收盤適用哪一種注碼（今天觸發的訊號用）。"""
        if self.sizing != "hybrid":
            return "above"
        return entry_regime(self.bars, len(self.bars) - 1, self.ma_series)

    def regime_tomorrow(self, close: float) -> str:
        """明天若收在 `close`，適用哪一種注碼。均線交叉價今晚就能算。"""
        if self.sizing != "hybrid":
            return "above"
        cross = self.ma_cross_tomorrow()
        return "below" if (cross is not None and close < cross) else "above"

    def entry_leverage(self, close: float, lv, regime: str) -> float:
        if regime == "below":
            return BELOW_LEV
        base = futures_leverage(close, lv, self.cfg, self.max_leverage)
        return base * ABOVE_RISK_SCALE if self.sizing == "hybrid" else base

    #: 均線上：每口小台所需權益 = 每點價值 ÷ 風險預算 × (收盤 − 停損線)
    @property
    def per_point(self) -> float:
        return MTX_POINT / self.risk      # 50 ÷ 4% = 1,250（舊版 50 ÷ 8% = 625）

    def cap_below(self, stop_line: float) -> float:
        """低於這個收盤價，槓桿會撞到上限，改用「收盤 × 每點價值 ÷ 上限」。"""
        m = self.cap
        return stop_line * m / (m - self.risk)

    def lot_cost(self, close: float, stop_line: float, regime: str) -> float:
        """在這個收盤價與注碼下，一口小台需要多少權益。"""
        if regime == "below":
            return close * MTX_POINT / BELOW_LEV
        if close < self.cap_below(stop_line):
            return close * MTX_POINT / self.cap
        return self.per_point * (close - stop_line)

    def lot_formula(self, regime: str, stop_line: float | None = None) -> str:
        if regime == "below":
            return f"收盤 × {MTX_POINT:g} ÷ {BELOW_LEV:g}"
        s = f"{stop_line:,.0f}" if stop_line is not None else "停損線"
        return f"(收盤 − {s}) × {self.per_point:.0f}"

    # ---- step-down ----
    def step_trigger(self, t):
        """3x 部位的減碼觸發：期貨自進場 +50%（權益 +150%）。回傳 (期貨倍率, 指數參考價) 或 None。"""
        if not self.step_down or t is None:
            return None
        tr = t.trade
        if tr.leverage <= STEP_LEV or tr.step_date is not None:
            return None
        ratio = 1.0 + STEP_GAIN / tr.leverage        # 3x：1.5 倍
        return ratio, tr.entry_index * ratio

    def stepped_today(self) -> bool:
        t = self.open_trade
        return bool(t and t.trade.step_date == self.last.d)

    def current_leverage(self, t) -> float:
        tr = t.trade
        return tr.final_leverage if tr.final_leverage is not None else tr.leverage

    # ---- 三個區塊 ----
    def tomorrow_plan(self) -> tuple[str, str, int]:
        """明天 13:30 要比對的點位與對應動作。回傳 (標題, 內容, 顏色)。

        這裡列出的每一個價位都由**已收盤**的資料決定，明天不會變動 ——
        所以今晚就能定案，明天只要比對收盤價。
        """
        c = self.last.close
        t = self.open_trade
        cfg = self.cfg

        hit = self.exit_signal_today()
        if hit is not None:
            line, why = hit
            return ("🔴 今天已觸發出場",
                    f"**收盤 {c:,.0f} 已跌破 {line:,.0f}（{why}）**\n"
                    f"→ 今日 13:45 前全數平倉；若已錯過，明天開盤補平。", RED)

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
            st = self.step_trigger(t)
            if st is not None:
                ratio, idx_ref = st
                rows.append(
                    f"🔻 期貨收盤 **≥ 你的進場價 × {ratio:.2f}**（指數參考 ≈ {idx_ref:,.0f}，"
                    f"{pct(idx_ref / c - 1)}）　→　**減碼到 {STEP_LEV:g}x**："
                    f"口數 = 權益 ÷ (收盤 × {MTX_POINT:g})，之後不加回")
            elif t.trade.step_date is not None:
                rows.append(f"　　_已於 {t.trade.step_date} 減碼至 {STEP_LEV:g}x，不再調整口數。_")
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
            lv = build_levels(w.peak, w.trough, cfg.levels)
            reg = self.regime_tomorrow(trig)
            lev = self.entry_leverage(trig, lv, reg)
            rows = [
                f"🟢 收盤 **≥ {trig:,.0f}**（{pct(trig / c - 1)}）"
                f"　→　13:45 前**買進**，"
                f"每 **{self.lot_cost(trig, lv.stop_line, reg):,.0f}** 元 1 口小台"
                f"（{'固定 3x' if reg == 'below' else f'槓桿約 {lev:.2f}x'}）",
                f"　　_進場後停損線 {lv.stop_line:,.0f}、失效線 {lv.invalidation:,.0f}，"
                f"兩條都已固定，不隨進場價變動。_",
            ]
            rows.append(self.regime_note(trig, lv.stop_line, reg))
            rows += [
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

    def regime_note(self, trig: float, stop_line: float, reg: str) -> str:
        """說明明天的注碼取決於收盤與 MA200 交叉價的相對位置。"""
        if self.sizing != "hybrid":
            return "　　_收得越高、離停損越遠，每口要的錢就越多 —— 見下方速查表。_"
        cross = self.ma_cross_tomorrow()
        if cross is None:
            return f"　　_均線資料不足，一律用半預算：每口 = {self.lot_formula('above', stop_line)}。_"
        if reg == "below":
            return (f"　　_觸發價在 MA200 交叉價 {cross:,.0f} 之下 → **固定 3x**"
                    f"（每口 = {self.lot_formula('below')}）；若收盤反而 ≥ {cross:,.0f}，"
                    f"改半預算：每口 = {self.lot_formula('above', stop_line)}。_")
        return (f"　　_觸發價已在 MA200 交叉價 {cross:,.0f} 之上 → **半預算**"
                f"（每口 = {self.lot_formula('above', stop_line)}，上限 {self.cap:g}x）。_")

    def trail_stop(self, t):
        """已創高時的移動停利價位。"""
        s = t.setup
        i0 = self.dates.index(t.trade.entry_date)
        hi = max(self.ic[i0:])
        if hi < s.peak:
            return None, ""
        d = self.cfg.exit.trail_drawdown
        return hi * (1 - d), f"波段最高收盤 {hi:,.0f}，移動停利 {d:.0%}。"

    def sizing_block(self) -> str:
        """收盤後只要做一次減法、一次乘法就能算出口數。"""
        t = self.open_trade
        if t is not None:
            tr, lv = t.trade, t.levels
            reg = "below" if tr.leverage == BELOW_LEV and self.sizing == "hybrid" else "above"
            cur = self.current_leverage(t)
            lines = ["```",
                     f"停損線 {lv.stop_line:,.0f}　（進場時已定，不變）",
                     "",
                     f"進場口數依據：每口 =（{self.lot_formula(reg, lv.stop_line)}）"
                     f"→ ({tr.entry_index:,.0f}) 時 {self.lot_cost(tr.entry_index, lv.stop_line, reg):,.0f} 元/口"
                     f"（＝槓桿 {tr.leverage:.2f}x）"]
            if tr.step_date is not None:
                lines.append(f"已於 {tr.step_date} 減碼至 {cur:g}x：每口 = 收盤 × {MTX_POINT:g}")
            elif self.step_trigger(t) is not None:
                ratio, idx_ref = self.step_trigger(t)
                lines.append(f"減碼觸發：期貨 ≥ 進場價 × {ratio:.2f}（指數參考 {idx_ref:,.0f}）"
                             f"→ 口數 = 權益 ÷ (收盤 × {MTX_POINT:g})")
            lines.append("```")
            return "\n".join(lines)

        w = self.live
        if w is None or w.state != "drawdown" or w.bars_left <= 0:
            return ("目前沒有可進場的劇本，暫時用不到。\n"
                    f"_通則：均線下 每口 = {self.lot_formula('below')}；"
                    f"均線上 每口 = {self.lot_formula('above')}_")
        lv = build_levels(w.peak, w.trough, self.cfg.levels)
        stop = lv.stop_line
        trig = w.trigger_close(self.cfg.setup)
        cross = self.ma_cross_tomorrow()
        lines = ["```", f"停損線 {stop:,.0f}　（今晚已定，明天不變）"]
        if self.sizing == "hybrid" and cross is not None:
            lines += [f"MA200 交叉價 {cross:,.0f}",
                      "",
                      f"收盤 < {cross:,.0f}：每口小台需要 = {self.lot_formula('below')}",
                      f"收盤 ≥ {cross:,.0f}：每口小台需要 = {self.lot_formula('above', stop)}"]
        else:
            lines += ["", f"每口小台需要 = {self.lot_formula('above', stop)}"]
        lines += ["口　　　數   = 你的權益 ÷ 上面那個數，無條件捨去", "```"]

        rows = ["```", f"{'明天收盤':>9}{'注碼':>5}{'每口需要':>11}{'100萬':>7}{'300萬':>7}{'500萬':>7}"]
        for c in (trig, trig * 1.005, trig * 1.01, trig * 1.02, trig * 1.03):
            reg = self.regime_tomorrow(c)
            cost = self.lot_cost(c, stop, reg)
            rows.append(f"{c:>9,.0f}{'3x' if reg == 'below' else '半':>5}{cost:>11,.0f}"
                        + "".join(f"{int(cap_ // cost):>7}" for cap_ in (1e6, 3e6, 5e6)))
        rows.append("```")
        lines += ["速查表（觸發價起算）"] + rows
        if self.sizing == "hybrid":
            lines.append(f"_均線下固定 {BELOW_LEV:g}x；均線上 {self.per_point:.0f} = 小台每點 "
                         f"{MTX_POINT:g} 元 ÷ 風險預算 {self.risk:.0%}（上限 {self.cap:g}x）。_")
        else:
            lines.append(f"_{self.per_point:.0f} = 小台每點 {MTX_POINT:g} 元 ÷ 風險預算 "
                         f"{self.risk:.0%}；等價於「每筆最多虧掉權益的 {self.risk:.0%}」。_")
        return "\n".join(lines)

    def today(self) -> str:
        """今天收盤該做的事（若你今天沒跑腳本，這裡補告訴你）。"""
        fired = self.fired_today
        if fired is not None:
            lv = build_levels(fired.peak, fired.trough, self.cfg.levels)
            reg = self.regime_today()
            lev = self.entry_leverage(self.last.close, lv, reg)
            return (f"🟢 **今天收盤觸發進場訊號** —— 若尚未建立部位，"
                    f"明天開盤補進（{'固定 3x' if reg == 'below' else f'槓桿 {lev:.2f}x'}），"
                    f"但進場價會與訊號日不同，風險略高於回測假設。\n"
                    f"{fired.peak_date} 高點 {fired.peak:,.0f} → {fired.trough_date} "
                    f"谷底 {fired.trough:,.0f}（回檔 {fired.drop_pct:.1%}），"
                    f"{fired.bars_to_repair} 日補回 {fired.repair_fraction:.0%}")
        if self.closed_today:
            t = self.closed_today
            return (f"🔴 **今天收盤出場** —— {t.exit_reason}\n"
                    f"本筆權益報酬 **{t.trade.ret:+.1%}**"
                    f"（持有 {t.bars_held} 個交易日）")
        if self.stepped_today():
            t = self.open_trade
            return (f"🔻 **今天收盤觸發減碼** —— 權益已達 +{STEP_GAIN:.0%}，"
                    f"應已減碼至 {STEP_LEV:g}x（口數 = 權益 ÷ (收盤 × {MTX_POINT:g})）。"
                    f"沒做的話明天開盤補做。")
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
        cur = self.current_leverage(t)
        lev_txt = (f"槓桿 **{tr.leverage:.2f}x**" if tr.step_date is None
                   else f"槓桿 {tr.leverage:.2f}x → **已減碼 {cur:g}x**（{tr.step_date}）")
        lines = [
            f"進場 **{tr.entry_date}** @ 指數 {tr.entry_index:,.0f}"
            f"　{lev_txt}　持有 {t.bars_held} 日",
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
        on, band = self.core_on, self.core_band
        line = (f"MA{CORE_MA} = {self.ma:,.0f}，"
                f"指數{'站上' if above > 0 else '跌破'}均線 {above:+.1%}")
        if band:
            line += (f"\n緩衝帶 ±{band:.0%}："
                     f"開啟 {self.ma * (1 + band):,.0f}／"
                     f"關閉 {self.ma * (1 - band):,.0f}　→　帶內維持原狀，不動作")
        # 開著就盯關閉門檻，關著就盯開啟門檻 —— 另一條這時碰不到
        cross = self.ma_cross_tomorrow((1 - band) if on else (1 + band))
        if cross is not None:
            if not band:
                label, side = "交叉價", "跌破" if on else "站上"
            else:
                label, side = ("關閉門檻", "關閉核心") if on else ("開啟門檻", "開啟核心")
            line += (f"\n明日{label} **{cross:,.0f}**（{pct(cross / c - 1)}）"
                     f"　→　收在這之{'下' if on else '上'}就{side}")
        if self.open_trade is not None:
            return line + ("\n→ 目前有策略部位，**核心部位不啟用**"
                           "（核心只在策略空手時填補曝險）")
        state = "**持有中**" if self.core_on else "**空手**"
        return line + f"\n→ 策略空手，核心 {CORE_LEVERAGE:g}x {state}"

    def ma_cross_tomorrow(self, k: float = 1.0) -> float | None:
        """明天恰好讓「收盤 = 均線 × k」成立的收盤價。

        明日均線 = (前 N-1 根收盤和 S + 明日收盤 c) / N，令 c = k × 均線：

            c = k(S + c)/N　→　c(N − k) = kS　→　c = kS / (N − k)

        今晚就能算出確切的門檻，不必等明天。`k = 1` 是均線交叉價本身
        （注碼用）；`k = 1 ± 緩衝帶` 是核心濾網的開啟／關閉門檻。
        """
        if len(self.ic) < CORE_MA:
            return None
        return k * sum(self.ic[-(CORE_MA - 1):]) / (CORE_MA - k)

    def has_action(self) -> bool:
        """明天是否有實際可能被觸發的點位（用於 --only-if-action）。"""
        if (self.fired_today or self.opened_today or self.closed_today
                or self.exit_signal_today() or self.stepped_today()):
            return True
        t = self.open_trade
        if t is not None:
            c = self.last.close
            stop, _ = self.trail_stop(t)
            if stop is not None and c / stop - 1 < 0.03:
                return True
            st = self.step_trigger(t)
            if st is not None:
                i0 = self.dates.index(t.trade.entry_date)
                if self.fs[-1] / self.fs[i0] >= st[0] * 0.97:
                    return True
            return c / t.levels.stop_line - 1 < 0.03
        w = self.live
        if w and w.state == "drawdown":
            trig = w.trigger_close(self.cfg.setup)
            return trig / self.last.close - 1 < 0.03 or w.bars_left <= 3
        return False

    # ---- Discord payload ----
    # ---- 精簡版：13:30 只需要看這幾行 ----
    def compact(self) -> tuple[str, str, int]:
        c = self.last.close
        t = self.open_trade
        cfg = self.cfg
        head = [f"`{self.last.d}` 收 **{c:,.0f}**"]
        rows: list[str] = []

        fired = self.fired_today
        if fired is not None:
            lv = build_levels(fired.peak, fired.trough, cfg.levels)
            reg = self.regime_today()
            cost = self.lot_cost(c, lv.stop_line, reg)
            # 這則訊息在收盤後才送達，13:45 的下單窗口已經過了 ——
            # 所以是「確認」而非「指示」，寫成命令句會讓人以為還來得及。
            return ("🟢 今天已觸發進場",
                    "\n".join(head + ["",
                        f"✅ 依昨日作戰表，**應已在 13:45 前買進** —— "
                        f"每口 {cost:,.0f}（口數 = 權益 ÷ {cost:,.0f}，捨去；"
                        f"{'固定 3x，均線下' if reg == 'below' else '半預算，均線上'}）",
                        f"停損 **{lv.stop_line:,.0f}**（{lv.stop_line / c - 1:+.1%}）"
                        f"　失效 {lv.invalidation:,.0f}",
                        f"回檔 {fired.drop_pct:.1%}，"
                        f"{fired.bars_to_repair} 日補回 {fired.repair_fraction:.0%}", "",
                        "❗ 沒買到 → **放棄這一筆**，不要隔天追。"
                        "進場價與停損距離都會偏離回測假設，風險更高。"]),
                    GREEN)
        d0 = self.closed_today
        hit = self.exit_signal_today()
        if d0 or hit:
            if d0:
                why, ret, held = d0.exit_reason, d0.trade.ret, d0.bars_held
            else:
                line, why = hit
                why = f"{why} {line:,.0f}"
                ret, held = t.trade.ret, t.bars_held
            return ("🔴 今天已觸發出場",
                    "\n".join(head + ["",
                        f"✅ 依昨日作戰表，**應已在 13:45 前平倉** —— {why}",
                        f"本筆 **{ret:+.1%}**（持有 {held} 日）", "",
                        "❗ 沒賣掉 → **明天開盤立刻平倉**，不要等反彈。"]), RED)

        if t is not None:
            lv = t.levels
            tr = t.trade
            stop, _ = self.trail_stop(t)
            binding = max(lv.stop_line, stop) if stop is not None else lv.stop_line
            if self.stepped_today():
                head.append(f"🔻 **今天已觸發減碼**：權益 +{STEP_GAIN:.0%}，應已減碼至 {STEP_LEV:g}x"
                            f"（口數 = 權益 ÷ (收盤 × {MTX_POINT:g})）")
            rows.append(f"🔴 跌破 **{binding:,.0f}**（{binding / c - 1:+.1%}）→ 13:45 前平倉")
            if stop is None:
                rows.append(f"⬆️ 站上 **{t.setup.peak:,.0f}**"
                            f"（{t.setup.peak / c - 1:+.1%}）→ 啟動移動停利")
            st = self.step_trigger(t)
            if st is not None:
                ratio, idx_ref = st
                rows.append(f"🔻 期貨 ≥ 進場價×{ratio:.2f}（指數≈{idx_ref:,.0f}，"
                            f"{idx_ref / c - 1:+.1%}）→ 減碼到 {STEP_LEV:g}x")
            rows.append("🟡 其餘 → 不動作")
            cur = self.current_leverage(t)
            lev_txt = f"{tr.leverage:.2f}x" if tr.step_date is None else f"{tr.leverage:.2f}x→{cur:g}x"
            reg = "below" if tr.leverage == BELOW_LEV and self.sizing == "hybrid" else "above"
            tail = (f"持有 `{tr.entry_date:%m-%d}` 進場 · {lev_txt} · "
                    f"**{tr.ret:+.1%}** · 進場每口 {self.lot_cost(tr.entry_index, lv.stop_line, reg):,.0f}")
            colour = YELLOW if c / binding - 1 < 0.02 else GREEN
            title = "🟡 續抱" if colour == GREEN else "🟠 貼近出場線"
            return (title, "\n".join(head + [""] + rows + ["", tail]), colour)

        w = self.live
        if w is not None and w.state == "drawdown" and w.bars_left > 0:
            lv = build_levels(w.peak, w.trough, cfg.levels)
            trig = w.trigger_close(cfg.setup)
            reg = self.regime_tomorrow(trig)
            head[0] += f" · 視窗剩 **{w.bars_left - 1}** 日"
            rows = [
                f"🟢 站上 **{trig:,.0f}**（{trig / c - 1:+.1%}）→ 買進，"
                f"每口 {self.lot_cost(trig, lv.stop_line, reg):,.0f}"
                f"（{'固定 3x' if reg == 'below' else '半預算'}）",
                f"🔻 跌破 **{w.trough:,.0f}**（{w.trough / c - 1:+.1%}）→ 破底重來",
                "⚪ 其餘 → 不動作",
            ]
            cross = self.ma_cross_tomorrow()
            if self.sizing == "hybrid" and cross is not None:
                tail = (f"口數 = 權益 ÷ [收盤 < {cross:,.0f}：{self.lot_formula('below')}；"
                        f"否則 {self.lot_formula('above', lv.stop_line)}]，捨去"
                        f"　進場後停損 {lv.stop_line:,.0f}")
            else:
                tail = (f"口數 = 權益 ÷ [{self.lot_formula('above', lv.stop_line)}]，捨去"
                        f"　進場後停損 {lv.stop_line:,.0f}")
            colour = GREEN if trig / c - 1 < 0.01 else BLUE
            return ("⚪ 追蹤中", "\n".join(head + [""] + rows + ["", tail]), colour)

        if w is not None and w.state == "expired":
            return ("⚪ 空手", "\n".join(head + ["", f"上一段已過期，"
                    f"收盤重新站上 **{w.peak:,.0f}**（{w.peak / c - 1:+.1%}）才重新追蹤"]), GREY)

        anchor = w.peak if w else c
        start = anchor * (1 - cfg.setup.min_drawdown)
        return ("⚪ 空手", "\n".join(head + ["", "明天不會有訊號",
                f"🔻 跌破 **{start:,.0f}**（{start / c - 1:+.1%}）→ 開始追蹤新的一段"]), GREY)

    def rule_tag(self) -> str:
        step = f"　減碼 +{STEP_GAIN:.0%}→{STEP_LEV:g}x" if self.step_down else ""
        band = f"　核心緩衝帶 ±{self.core_band:.0%}" if self.core_band else ""
        if self.sizing == "hybrid":
            return (f"注碼 均線下 {BELOW_LEV:g}x／均線上 "
                    f"{self.risk:.0%}÷停損距離≤{self.cap:g}x" + step + band)
        return (f"參數組 tuned　槓桿上限 {self.max_leverage:g}x" + step + band)

    def payload(self, full: bool = False) -> dict:
        note = f"{self.note}\n" if self.note else ""
        if not full:
            title, body, colour = self.compact()
            return {"embeds": [{"title": title, "description": note + body,
                                "color": colour,
                                "footer": {"text": "13:30 比對收盤價，13:45 前下單"
                                                   "　|　--full 看完整版"}}]}

        title, body, colour = self.tomorrow_plan()
        fields = [
            {"name": "① 明天 13:30 比對收盤價", "value": body, "inline": False},
            {"name": "② 收盤後怎麼算口數", "value": self.sizing_block(), "inline": False},
            {"name": "③ 今天發生了什麼", "value": self.today(), "inline": False},
            {"name": "④ 部位現況", "value": self.position(), "inline": False},
            {"name": "⑤ 劇本追蹤", "value": self.script(), "inline": False},
            {"name": f"⑥ 核心部位（MA{CORE_MA} 濾網）", "value": self.core(),
             "inline": False},
        ]
        foot = (f"點位由已收盤資料決定，明天不會變　|　"
                f"下單窗口 13:30–13:45　|　{self.rule_tag()}")
        if self.missing:
            foot += f"　|　⚠️ {len(self.missing)} 個換倉日缺次月報價"
        return {"embeds": [{
            "title": title,
            "description": note + f"依據 **{self.last.d}** 收盤 "
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
    for f in e.get("fields", []):
        out += [f"── {f['name']} ──", f["value"], ""]
    out.append(e["footer"]["text"])
    return "\n".join(out).replace("**", "").replace("`", "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日訊號推送到 Discord")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
    ap.add_argument("--preset", default="tuned")
    ap.add_argument("--max-leverage", type=float, default=5.0)
    ap.add_argument("--sizing", default="hybrid", choices=("hybrid", "risk"),
                    help="hybrid＝§20 混合注碼（預設）；risk＝舊版 8%% ÷ 停損距離")
    ap.add_argument("--no-step-down", action="store_true",
                    help="關閉 §22 的 step-down（權益 +150%% → 降到 1x）")
    ap.add_argument("--core-band", type=float, default=CORE_BAND, metavar="X",
                    help="核心濾網的遲滯緩衝帶：站上 MA200×(1+X) 才開、"
                         "跌破 MA200×(1−X) 才關。預設 %(default)g，"
                         "傳 0 可切回逐日 `收盤 > MA200`")
    ap.add_argument("--dry-run", action="store_true", help="只印出，不送出")
    ap.add_argument("--only-if-action", action="store_true",
                    help="沒有動作也沒有接近觸發時，不送訊息")
    ap.add_argument("--stale-days", type=int, default=5,
                    help="資料落後超過這麼多天就警告")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="依據某一天的收盤回放（驗證用）")
    ap.add_argument("--full", action="store_true",
                    help="完整版（六個區塊）。預設是只有觸發點位的精簡版")
    ap.add_argument("--note", default="",
                    help="在訊息開頭加一行提示，例如標明這是回放而非即時訊號")
    args = ap.parse_args(argv)

    login()
    as_of = (date.fromisoformat(args.as_of) if args.as_of else None)
    note = args.note or ("⚠️ **這是回放，不是即時訊號**" if as_of else "")
    if not 0.0 <= args.core_band < 1.0:
        raise SystemExit("--core-band 必須在 [0, 1) 之間")
    d = Daily(PRESETS[args.preset], args.max_leverage, as_of, note,
              sizing=args.sizing, step_down=not args.no_step_down,
              core_band=args.core_band)

    lag = (date.today() - d.last.d).days
    if as_of is None and lag > args.stale_days:
        print(f"⚠️ 資料只到 {d.last.d}（落後 {lag} 天），FinLab 可能尚未更新",
              file=sys.stderr)

    if args.only_if_action and not d.has_action():
        print(f"{d.last.d} 沒有動作也沒有接近觸發，不送出。")
        return 0

    p = d.payload(args.full)
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
