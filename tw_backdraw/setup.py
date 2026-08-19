"""辨識「快速修復」劇本。

流程（全部以收盤價、可即時判定，不使用未來資料）：
    1. 從滾動高點 P 起算，收盤跌破 P×(1-10%) → 進入回檔追蹤
    2. 追蹤期間持續更新谷底 T（創更低就更新，計時歸零）
    3. 自 T 起 N 個交易日內，收盤補回跌幅 ≥ 75% → 觸發訊號
       （N ≤ 15 對應貼文中「89% 會先回到舊高點」的那一組）
    4. 超過 15 日才補回 → 這一段作廢（那一組成功率只剩 48%，跟丟銅板一樣），
       參考高點改錨到谷底之後的波段高，重新開始找下一組 P/T

第 4 步的改錨很關鍵。少了它，參考高點會一直釘在舊高直到指數重新站上為止 ——
台股 2000 年頭部之後花了 17.3 年才收復 10,202，中間包含 2008、2015、2020
在內的所有回檔修復都會被那個舊高遮蔽，偵測器等於瞎掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .bars import Bar
from .config import SetupConfig


@dataclass(frozen=True)
class Episode:
    """一段 ≥10% 的回檔，以及它為什麼（沒）觸發訊號。"""

    peak: float
    peak_date: date
    peak_index: int
    trough: float
    trough_date: date
    trough_index: int
    lower_lows: int              # 追蹤期間破底幾次（每次都讓計時歸零）
    best_in_window: float        # 15 日視窗內補回的最高比例
    best_in_window_bars: int     # 上述最佳值出現在谷底後第幾日
    fired: bool
    end_index: int
    end_date: date

    @property
    def drop_pct(self) -> float:
        return (self.trough - self.peak) / self.peak

    def reason(self, cfg: SetupConfig) -> str:
        if self.fired:
            return f"✅ {self.best_in_window_bars} 日補回 {self.best_in_window:.0%}"
        return (f"❌ {cfg.max_repair_bars} 日內只補回 {self.best_in_window:.0%}"
                f"（第 {self.best_in_window_bars} 日最佳）")


@dataclass(frozen=True)
class FastRepairSetup:
    peak: float
    peak_date: date
    trough: float
    trough_date: date
    trough_index: int
    trigger_index: int
    trigger_date: date
    trigger_close: float
    bars_to_repair: int
    repair_fraction: float

    @property
    def drop_pct(self) -> float:
        return (self.peak - self.trough) / self.peak

    def describe(self) -> str:
        return (
            f"{self.trough_date} 谷底 {self.trough:,.0f}（自 {self.peak_date} 高點 "
            f"{self.peak:,.0f} 回檔 {self.drop_pct:.1%}），"
            f"{self.bars_to_repair} 個交易日補回 {self.repair_fraction:.0%}，"
            f"{self.trigger_date} 觸發訊號 @ {self.trigger_close:,.0f}"
        )


def scan_episodes(bars: list[Bar], cfg: SetupConfig) -> list[Episode]:
    """列出所有 ≥ `min_drawdown` 的回檔段落，含觸發與未觸發的原因。

    `detect_setups()` 就是取這裡面 `fired=True` 的那些，所以兩者不會不一致。
    """
    episodes: list[Episode] = []
    if not bars:
        return episodes

    peak, peak_i = bars[0].close, 0
    trough, trough_i = bars[0].close, 0
    state = "normal"
    lower_lows = 0
    win_best, win_best_n = 0.0, 0

    def flush(i: int, fired: bool) -> None:
        episodes.append(Episode(
            peak=peak, peak_date=bars[peak_i].d, peak_index=peak_i,
            trough=trough, trough_date=bars[trough_i].d, trough_index=trough_i,
            lower_lows=lower_lows, best_in_window=win_best,
            best_in_window_bars=win_best_n, fired=fired,
            end_index=i, end_date=bars[i].d,
        ))

    for i, bar in enumerate(bars):
        c = bar.close

        if state == "normal":
            if c > peak:
                peak, peak_i = c, i
            elif c <= peak * (1 - cfg.min_drawdown):
                state = "drawdown"
                trough, trough_i = c, i
                lower_lows, win_best, win_best_n = 0, 0.0, 0
            continue

        if state == "drawdown":
            if c < trough:
                # 創更低點：谷底、計時與視窗內最佳進度一起重設
                trough, trough_i = c, i
                lower_lows += 1
                win_best, win_best_n = 0.0, 0
                continue

            elapsed = i - trough_i
            frac = (c - trough) / (peak - trough) if peak > trough else 0.0
            if elapsed <= cfg.max_repair_bars and frac > win_best:
                win_best, win_best_n = frac, elapsed

            if frac >= cfg.repair_fraction and elapsed <= cfg.max_repair_bars:
                flush(i, fired=True)
                state = "engaged"
                continue

            if elapsed > cfg.max_repair_bars:
                # 修復視窗到期，這一段作廢
                flush(i, fired=False)
                if cfg.reanchor_on_expiry:
                    seg = bars[trough_i:i + 1]
                    k = max(range(len(seg)), key=lambda j: seg[j].close)
                    peak, peak_i = seg[k].close, trough_i + k
                    state = "normal"
                    continue
                # 不改錨：等指數重新站上舊高才解除
                state = "expired"
                continue

            if c > peak:
                flush(i, fired=False)
                state, peak, peak_i = "normal", c, i
            continue

        if state == "expired":
            if c > peak:
                state, peak, peak_i = "normal", c, i
            continue

        if state == "engaged":
            last = episodes[-1]
            if (c > peak or c < last.trough
                    or (i - last.end_index) > cfg.setup_expiry_bars):
                state = "normal"
                if c > peak:
                    peak, peak_i = c, i
            continue

    return episodes


def detect_setups(bars: list[Bar], cfg: SetupConfig) -> list[FastRepairSetup]:
    """掃描整段歷史，回傳所有觸發過的快速修復訊號。"""
    return [
        FastRepairSetup(
            peak=e.peak, peak_date=e.peak_date,
            trough=e.trough, trough_date=e.trough_date, trough_index=e.trough_index,
            trigger_index=e.end_index, trigger_date=e.end_date,
            trigger_close=bars[e.end_index].close,
            bars_to_repair=e.best_in_window_bars,
            repair_fraction=e.best_in_window,
        )
        for e in scan_episodes(bars, cfg) if e.fired
    ]
