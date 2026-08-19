"""把訊號翻譯成可以直接下單的操作計畫。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT_CONFIG, StrategyConfig
from .engine import ladder_weights, position_size
from .levels import Levels, build_levels


@dataclass(frozen=True)
class Order:
    tag: str
    weight: float          # 佔總權益比例
    trigger: str
    index_level: float | None   # 觸發時的指數概略位置（回檔梯以波段高點推算）


@dataclass(frozen=True)
class TradePlan:
    levels: Levels
    reference_index: float
    target_weight: float
    orders: list[Order]
    capital: float | None = None

    def render(self) -> str:
        lv = self.levels
        out: list[str] = []
        out.append("=" * 68)
        out.append("台灣指數快速修復 → 台灣50正2(00631L) 回檔入場計畫")
        out.append("=" * 68)
        out.append("")
        out.append(f"前波高點 P        {lv.peak:>10,.0f}   ← 主目標（歷史 89% 會回到這裡）")
        out.append(f"波段谷底 T        {lv.trough:>10,.0f}   ← 失效線：收盤跌破 = 全部出場")
        out.append(f"跌幅 R            {lv.drop:>10,.0f} 點 ({lv.drop_pct:.1%})")
        out.append("")
        out.append("關鍵價位")
        out.append(f"  61.8% 回補      {lv.trough + 0.618 * lv.drop:>10,.0f}")
        out.append(f"  主防線 (50%)    {lv.half_line:>10,.0f}   歷史回檔多數止步於此")
        out.append(f"  容忍緩衝 (-0.5%){lv.half_line_with_buffer:>10,.0f}   假跌破區，不加碼也不減碼")
        out.append(f"  警戒線 (38.2%)  {lv.warn_line:>10,.0f}   收盤跌破 → 減碼一半")
        out.append(f"  失效線          {lv.invalidation:>10,.0f}   收盤跌破 → 清倉")
        out.append("")
        out.append(f"現價（參考）      {self.reference_index:>10,.0f}   "
                   f"補回 {lv.repair_fraction(self.reference_index):.0%}，"
                   f"距前高 {self.reference_index / lv.peak - 1:+.1%}，分區：{lv.zone(self.reference_index)}")
        out.append("")
        out.append(f"目標總持股水位    {self.target_weight:.0%} 的權益"
                   + (f"（約 {self.capital * self.target_weight:,.0f} 元）" if self.capital else ""))
        out.append("")
        out.append("進場梯（權重為佔總權益比例）")
        for o in self.orders:
            if o.weight > 0:
                size = f"{o.weight:>6.1%}"
                amount = f"  ≈ {self.capital * o.weight:,.0f} 元" if self.capital else ""
            else:
                size, amount = "  剩餘", ""
            level = f"（指數約 {o.index_level:,.0f}）" if o.index_level else ""
            out.append(f"  {o.tag:<10} {size}{amount:<18} {o.trigger}{level}")
        out.append("")
        out.append("出場")
        out.append(f"  觸及 {lv.peak:,.0f}    → 賣出 1/3 落袋，其餘轉移動停利")
        out.append("  移動停利          自波段最高收盤回檔 8%（指數）→ 出清")
        out.append(f"  收盤 < {lv.warn_line:,.0f}   → 減碼一半，停止加碼")
        out.append(f"  收盤 < {lv.invalidation:,.0f}   → 全部出場，不留倉")
        out.append("")
        out.append("提醒：00631L 為 2 倍槓桿，指數 -1% ≈ ETF -2%（另有波動耗損與內扣）。")
        to_stop = lv.invalidation / self.reference_index - 1
        to_warn = lv.warn_line / self.reference_index - 1
        out.append(f"      現價到警戒線 {to_warn:.1%}（ETF 約 {2 * to_warn:.0%}）、"
                   f"到失效線 {to_stop:.1%}（ETF 約 {2 * to_stop:.0%}）。")
        return "\n".join(out)


def build_plan(peak: float, trough: float, reference_index: float,
               cfg: StrategyConfig | None = None,
               capital: float | None = None) -> TradePlan:
    cfg = cfg or DEFAULT_CONFIG
    lv = build_levels(peak, trough, cfg.levels)
    target = position_size(reference_index, lv, cfg)

    orders = [Order(tag="底倉", weight=cfg.entry.base_weight * target,
                    trigger="訊號確認後次一交易日市價買進，不等回檔", index_level=None)]
    for thr, w in ladder_weights(cfg, target):
        orders.append(
            Order(tag=f"回檔 -{thr:.0%}", weight=w,
                  trigger=f"自訊號後波段最高收盤回檔 {thr:.0%} 且收盤仍在 {lv.half_line_with_buffer:,.0f} 之上",
                  index_level=reference_index * (1 - thr))
        )
    orders.append(
        Order(tag="時間補齊", weight=0.0,
              trigger=f"訊號後 {cfg.entry.fill_timeout_bars} 個交易日仍未觸發回檔梯 → 剩餘部位市價補齊",
              index_level=None)
    )
    return TradePlan(levels=lv, reference_index=reference_index, target_weight=target,
                     orders=orders, capital=capital)
