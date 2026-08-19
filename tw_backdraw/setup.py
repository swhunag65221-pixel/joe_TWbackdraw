"""辨識「快速修復」劇本。

流程（全部以收盤價、可即時判定，不使用未來資料）：
    1. 從滾動新高 P 起算，收盤跌破 P×(1-10%) → 進入回檔追蹤
    2. 追蹤期間持續更新谷底 T（創更低就更新，計時歸零）
    3. 自 T 起 N 個交易日內，收盤補回跌幅 ≥ 75% → 觸發訊號
       （N ≤ 15 對應貼文中「89% 會先回到舊高點」的那一組）
    4. 超過 15 日才補回 → 不觸發（那一組成功率只剩 48%，跟丟銅板一樣）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .bars import Bar
from .config import SetupConfig


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


def detect_setups(bars: list[Bar], cfg: SetupConfig) -> list[FastRepairSetup]:
    """掃描整段歷史，回傳所有觸發過的快速修復訊號。"""
    setups: list[FastRepairSetup] = []
    if not bars:
        return setups

    peak, peak_i = bars[0].close, 0
    state = "normal"
    trough, trough_i = bars[0].close, 0
    window_expired = False

    for i, bar in enumerate(bars):
        c = bar.close

        if state == "normal":
            if c > peak:
                peak, peak_i = c, i
            elif c <= peak * (1 - cfg.min_drawdown):
                state, trough, trough_i, window_expired = "drawdown", c, i, False
            continue

        if state == "drawdown":
            if c < trough:
                # 創更低點：谷底與計時一起重設
                trough, trough_i, window_expired = c, i, False
                continue

            elapsed = i - trough_i
            frac = (c - trough) / (peak - trough) if peak > trough else 0.0

            if not window_expired and frac >= cfg.repair_fraction and elapsed <= cfg.max_repair_bars:
                setups.append(
                    FastRepairSetup(
                        peak=peak,
                        peak_date=bars[peak_i].d,
                        trough=trough,
                        trough_date=bars[trough_i].d,
                        trough_index=trough_i,
                        trigger_index=i,
                        trigger_date=bar.d,
                        trigger_close=c,
                        bars_to_repair=elapsed,
                        repair_fraction=frac,
                    )
                )
                state = "engaged"
                continue

            if elapsed > cfg.max_repair_bars:
                # 慢速修復：這一段不是我們的劇本，等它創高後重新開始追蹤
                window_expired = True

            if c > peak:
                state, peak, peak_i = "normal", c, i
            continue

        if state == "engaged":
            # 訊號已發出，等這一輪走完（創高、跌破谷底或過期）再重新掃描
            last = setups[-1]
            if c > peak or c < last.trough or (i - last.trigger_index) > cfg.setup_expiry_bars:
                state = "normal"
                peak, peak_i = (c, i) if c > peak else (peak, peak_i)
            continue

    return setups
