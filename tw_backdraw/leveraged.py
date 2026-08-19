"""台灣50正2（00631L）的價格模型。

實務上訊號來自加權指數，執行卻在槓桿 ETF 上，兩者不是同一條線：

* 00631L 追蹤的是「台灣50指數單日報酬 2 倍」，不是加權指數，
  但兩者日報酬相關性長期在 0.95 以上，訊號層面可互用。
* 2 倍是「單日」複製，路徑相依：盤整盤會有波動耗損，
  單邊上漲則會優於 2 倍。
* 內扣（管理費 + 期貨轉倉/避險）約年化 1%～1.5%，逐日侵蝕淨值。

沒有實際 00631L 日線時，用本模組由指數日報酬合成一條可回測的淨值路徑；
有實際日線就直接餵真實價格，模型只用來做對照。
"""

from __future__ import annotations

from .bars import Bar
from .config import CostConfig


def synth_leveraged_path(
    bars: list[Bar],
    cost: CostConfig,
    leverage: float = 2.0,
    start_price: float = 100.0,
) -> list[float]:
    """由指數日線合成 2 倍槓桿 ETF 的收盤淨值序列。"""
    path = [start_price]
    for prev, cur in zip(bars, bars[1:]):
        r = cur.close / prev.close - 1.0
        nav = path[-1] * (1.0 + leverage * r - cost.daily_carry)
        # 槓桿 ETF 淨值不會歸零/轉負，但單日 -50% 指數已超出任何現實情境；
        # 保底避免回測數值爆掉。
        path.append(max(nav, 1e-6))
    return path


def decay_estimate(index_return: float, realized_vol: float, days: int, cost: CostConfig,
                   leverage: float = 2.0) -> float:
    """粗估持有 N 日後，槓桿 ETF 相對「指數報酬 × 2」的落差。

    近似式: 2x 報酬 ≈ L·r - 0.5·L·(L-1)·σ²·(days/252) - carry·days/252
    用來提醒：這個劇本必須是「快速、單邊」的行情才值得用槓桿工具。
    """
    variance_drag = 0.5 * leverage * (leverage - 1.0) * (realized_vol ** 2) * (days / cost.trading_days)
    carry_drag = cost.daily_carry * days
    return leverage * index_return - variance_drag - carry_drag
