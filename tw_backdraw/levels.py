"""由「前高 P」與「谷底 T」推導出的關鍵價位。

台股當前實例：P = 47742、T = 39933、跌幅 R = 7809 點
    主防線 (50%)  = 43837   ← 貼文的 43800
    警戒線 (38.2%) = 42916
    失效線        = 39933
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LevelConfig


@dataclass(frozen=True)
class Levels:
    peak: float
    trough: float
    half_line: float
    half_line_with_buffer: float
    warn_line: float
    invalidation: float

    @property
    def drop(self) -> float:
        return self.peak - self.trough

    @property
    def drop_pct(self) -> float:
        return self.drop / self.peak

    @property
    def stop_line(self) -> float:
        """真正會觸發出場的價位。

        `zone()` 要落到 warning 得同時跌破警戒線**與**主防線的假跌破緩衝區，
        所以實際的停損線是兩者取低。預設參數下警戒線與主防線重合
        （`warn_line_ratio == half_line_ratio`），緩衝區就成了真正的觸發點 ——
        收盤跌破主防線 0.5% 以內會被容忍，超過才出場。
        """
        return min(self.warn_line, self.half_line_with_buffer)

    def repair_fraction(self, price: float) -> float:
        """價格相當於補回跌幅的幾成。"""
        if self.drop <= 0:
            return 0.0
        return (price - self.trough) / self.drop

    def zone(self, price: float) -> str:
        """把價格歸類到操作分區。"""
        if price >= self.peak:
            return "breakout"       # 已創高：轉移動停利
        if price >= self.half_line:
            return "healthy"        # 劇本正常：回檔即加碼
        if price >= self.half_line_with_buffer:
            return "buffer"         # 假跌破容忍區：不加碼、不減碼
        if price >= self.warn_line:
            return "caution"        # 跌破主防線：停止加碼
        if price >= self.invalidation:
            return "warning"        # 跌破 38.2%：減碼
        return "invalidated"        # 跌破谷底：另一個故事，全出


def build_levels(peak: float, trough: float, cfg: LevelConfig) -> Levels:
    if peak <= trough:
        raise ValueError("前高必須高於谷底")
    drop = peak - trough
    half = trough + cfg.half_line_ratio * drop
    return Levels(
        peak=peak,
        trough=trough,
        half_line=half,
        half_line_with_buffer=half * (1 - cfg.half_line_buffer),
        warn_line=trough + cfg.warn_line_ratio * drop,
        invalidation=trough,
    )
