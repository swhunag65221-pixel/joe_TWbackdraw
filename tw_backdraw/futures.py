"""台指期執行版：連續合約、換倉、固定口數的槓桿部位。

與 ETF 版的三個差異
-------------------
1. **換倉**：近月合約到期要換到次月。換倉當日以「近月收盤」平倉、
   「次月同日收盤」建倉，兩者的價差**不計入損益** —— 那是逆價差/正價差，
   不是賺賠。`build_continuous()` 就是在做這件事。

2. **槓桿由停損距離決定**：ETF 的槓桿是固定的（00631L 為 2 倍），期貨可以自己選。
   同樣的風險預算下，停損越近就能放越大：

       槓桿 L = 風險預算 ÷ 停損距離，上限 `max_leverage`

   注意這裡**不套用 `min_stop_distance` 下限** —— 那個下限是為了防止
   固定槓桿的 ETF 算出過大的水位；期貨改由槓桿上限承擔同樣的角色。
   若保留下限，L 會恆等於 8%/5% = 1.6 倍，槓桿上限永遠碰不到。

3. **進場後不調整口數**：權益變動時實際槓桿會自然漂移（賺錢時下降）。
   因此持有期間的權益是**線性**於期貨報酬，不是複利：

       權益(t) / 權益(進場) = 1 + L × (F(t)/F(進場) − 1)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import StrategyConfig
from .levels import Levels

#: 台指期（TX）每點新台幣，小台（MTX）為 50
TX_POINT_VALUE = 200.0


@dataclass(frozen=True)
class FuturesCost:
    """期貨交易成本（以契約金額比例表示，單邊）。"""

    #: 期交稅：契約金額的十萬分之二
    tax_rate: float = 0.00002
    #: 手續費，每口新台幣。換算成比例時需要指數點位與每點價值
    commission_per_lot: float = 50.0
    point_value: float = TX_POINT_VALUE

    def one_way_rate(self, index_level: float) -> float:
        """單邊成本佔契約金額的比例。"""
        notional = index_level * self.point_value
        return self.tax_rate + self.commission_per_lot / notional


def build_continuous(dates: list[date], front_close: list[float],
                     contract: list[str],
                     next_close_on_roll: dict[date, float]) -> list[float]:
    """把近月連續序列接成「已還原換倉價差」的可交易序列。

    Args:
        dates / front_close / contract: 逐日的日期、近月收盤、該日所屬合約月。
        next_close_on_roll: {換倉日: 次月合約在該日的收盤}。換倉日指的是
            **舊合約的最後一天**，也就是 `contract` 即將改變的前一天。

    回傳與輸入等長的序列，起點對齊 `front_close[0]`。

    換倉日隔天的報酬改用「次日近月收盤 ÷ 次月在換倉日的收盤」計算，
    價差因此不落入損益。缺換倉價差資料時退回原始跳動，並在
    `missing_rolls()` 中可查出是哪幾天。
    """
    if not dates:
        return []
    out = [front_close[0]]
    for i in range(1, len(dates)):
        prev_is_roll = contract[i] != contract[i - 1]
        base = front_close[i - 1]
        if prev_is_roll:
            base = next_close_on_roll.get(dates[i - 1], front_close[i - 1])
        out.append(out[-1] * (front_close[i] / base) if base else out[-1])
    return out


def missing_rolls(dates: list[date], contract: list[str],
                  next_close_on_roll: dict[date, float]) -> list[date]:
    """列出缺少次月報價、只能沿用原始跳動的換倉日。"""
    return [dates[i - 1] for i in range(1, len(dates))
            if contract[i] != contract[i - 1] and dates[i - 1] not in next_close_on_roll]


def futures_leverage(entry_index: float, levels: Levels, cfg: StrategyConfig,
                     max_leverage: float = 5.0) -> float:
    """依停損距離決定槓桿倍數，上限 `max_leverage`。

    停損距離的定義與 ETF 版一致（依 `warn_derisk_fraction` 對警戒線與失效線加權），
    但**不套用 `min_stop_distance` 下限**，理由見模組說明。
    """
    to_warn = max(entry_index - levels.warn_line, 0.0) / entry_index
    to_trough = max(entry_index - levels.invalidation, 0.0) / entry_index
    f = cfg.exit.warn_derisk_fraction
    dist = f * to_warn + (1.0 - f) * to_trough
    if dist <= 0:
        return max_leverage
    return min(max_leverage, cfg.sizing.risk_per_trade / dist)


@dataclass(frozen=True)
class FuturesTrade:
    entry_date: date
    exit_date: date | None
    entry_index: float
    entry_futures: float
    exit_futures: float | None
    leverage: float
    stop_distance: float
    futures_return: float    # 期貨本身的報酬
    ret: float               # 權益報酬（已扣進出場成本）


@dataclass(frozen=True)
class FuturesEntry:
    """一筆待執行的期貨部位。索引對應 `dates` 的位置。"""

    entry_i: int
    exit_i: int | None       # None = 持有到資料最後一根
    leverage: float
    entry_index: float       # 進場當日的指數收盤
    stop_distance: float


def vehicle_series(dates: list[date], continuous: list[float],
                   entries: list[FuturesEntry], cost: FuturesCost,
                   index_close: list[float]) -> tuple[list[float], list[FuturesTrade]]:
    """把「固定口數的槓桿期貨部位」攤成一條可餵給回測器的淨值序列。

    空手期間持平；持有期間依 `1 + L × (F/F_entry − 1)` **線性**變動
    （固定口數，不複利、不再平衡），並在進出場各扣一次單邊成本。

    成本按契約金額計算，所以扣在權益上的比例是 `L × 單邊成本率`。
    """
    nav = [1.0] * len(dates)
    detail: list[FuturesTrade] = []
    by_entry = {e.entry_i: e for e in entries}

    equity = 1.0
    active: FuturesEntry | None = None
    eq_at_entry = eq_after_cost = f0 = 0.0

    for i in range(len(dates)):
        if active is None:
            e = by_entry.get(i)
            if e is None:
                nav[i] = equity
                continue
            active = e
            eq_at_entry = equity
            eq_after_cost = equity * (1.0 - e.leverage * cost.one_way_rate(index_close[i]))
            f0 = continuous[i]
            nav[i] = eq_after_cost
            continue

        v = eq_after_cost * (1.0 + active.leverage * (continuous[i] / f0 - 1.0))
        closing = active.exit_i is not None and i == active.exit_i
        if closing:
            v *= (1.0 - active.leverage * cost.one_way_rate(index_close[i]))
        nav[i] = v
        if closing:
            detail.append(FuturesTrade(
                entry_date=dates[active.entry_i], exit_date=dates[i],
                entry_index=active.entry_index, entry_futures=f0,
                exit_futures=continuous[i], leverage=active.leverage,
                stop_distance=active.stop_distance,
                futures_return=continuous[i] / f0 - 1.0,
                ret=v / eq_at_entry - 1.0))
            equity, active = v, None

    if active is not None:      # 持有到最後一根，未平倉
        detail.append(FuturesTrade(
            entry_date=dates[active.entry_i], exit_date=None,
            entry_index=active.entry_index, entry_futures=f0,
            exit_futures=continuous[-1], leverage=active.leverage,
            stop_distance=active.stop_distance,
            futures_return=continuous[-1] / f0 - 1.0,
            ret=nav[-1] / eq_at_entry - 1.0))
    return nav, detail
