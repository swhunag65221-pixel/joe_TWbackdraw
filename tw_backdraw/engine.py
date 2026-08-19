"""策略執行引擎（狀態機）。

判斷一律用收盤價，成交一律落在下一個交易日，
避免「當日收盤發訊號、當日收盤成交」這種實務上做不到的假設。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .bars import Bar
from .config import DEFAULT_CONFIG, StrategyConfig
from .levels import Levels, build_levels
from .setup import FastRepairSetup, detect_setups


@dataclass
class Fill:
    d: date
    side: str            # buy / sell
    reason: str
    index_price: float
    etf_price: float
    weight: float        # 買：佔訊號日權益比例；賣：佔當時持股比例
    units: float
    cash_flow: float

    def describe(self) -> str:
        verb = "買進" if self.side == "buy" else "賣出"
        return (f"{self.d} {verb} {self.weight:>6.1%} @ ETF {self.etf_price:,.2f} "
                f"(指數 {self.index_price:,.0f})  {self.reason}")


@dataclass
class Trade:
    setup: FastRepairSetup
    levels: Levels
    target_weight: float
    fills: list[Fill] = field(default_factory=list)
    entry_date: date | None = None
    exit_date: date | None = None
    exit_reason: str = "open"
    equity_at_entry: float = 1.0
    equity_at_exit: float = 1.0
    max_adverse_index: float = 0.0   # 進場後指數最大不利波動
    reached_prior_high: bool = False

    @property
    def ret(self) -> float:
        return self.equity_at_exit / self.equity_at_entry - 1.0

    @property
    def filled_weight(self) -> float:
        return sum(f.weight for f in self.fills if f.side == "buy")


@dataclass
class Result:
    trades: list[Trade]
    equity_curve: list[tuple[date, float]]
    setups: list[FastRepairSetup]

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else 1.0


def _blended_stop_distance(entry_index: float, levels: Levels, cfg: StrategyConfig) -> float:
    """兩段式停損的預期虧損距離（指數口徑）。

    先在警戒線減碼一半，剩下的在谷底出清，真正的預期損失介於兩者之間。
    """
    to_warn = max(entry_index - levels.warn_line, 0.0) / entry_index
    to_trough = max(entry_index - levels.invalidation, 0.0) / entry_index
    f = cfg.exit.warn_derisk_fraction
    blended = f * to_warn + (1.0 - f) * to_trough
    return max(blended, cfg.sizing.min_stop_distance)


def position_size(entry_index: float, levels: Levels, cfg: StrategyConfig) -> float:
    """這一筆交易的目標持股水位（佔權益比例）。

    槓桿 ETF 的虧損 ≈ 指數跌幅 × 2，所以指數停損距離越遠、部位必須越小。
    """
    expected_loss = cfg.sizing.leverage * _blended_stop_distance(entry_index, levels, cfg)
    return min(cfg.sizing.max_weight, cfg.sizing.risk_per_trade / expected_loss)


class Engine:
    """單一部位、單一標的（台灣50正2）的回測 / 實盤訊號引擎。"""

    def __init__(self, cfg: StrategyConfig | None = None):
        self.cfg = cfg or DEFAULT_CONFIG

    def run(self, bars: list[Bar], etf_prices: list[float]) -> Result:
        cfg = self.cfg
        if len(bars) != len(etf_prices):
            raise ValueError("指數日線與 ETF 價格長度不一致")

        setups = detect_setups(bars, cfg.setup)
        by_trigger = {s.trigger_index: s for s in setups}

        cash, units = 1.0, 0.0
        equity_curve: list[tuple[date, float]] = []
        trades: list[Trade] = []

        trade: Trade | None = None
        pending: list[tuple[str, float, str]] = []   # (side, weight, reason)
        base_equity = 1.0
        swing_high = 0.0
        peak_since_breakout = 0.0
        unfilled: list[tuple[float, float]] = []
        derisked = reloaded = took_profit = False

        for i, bar in enumerate(bars):
            px = etf_prices[i]

            # ---- 1. 執行前一交易日收盤掛出的委託 ----
            for side, weight, reason in pending:
                if side == "buy":
                    notional = weight * base_equity
                    if notional <= 0:
                        continue
                    fee = notional * cfg.cost.buy_cost
                    u = notional / px
                    cash -= notional + fee
                    units += u
                    assert trade is not None
                    trade.fills.append(
                        Fill(bar.d, "buy", reason, bar.close, px, weight, u, -(notional + fee))
                    )
                    if trade.entry_date is None:
                        trade.entry_date = bar.d
                        trade.equity_at_entry = base_equity
                else:
                    u = units * weight
                    if u <= 0:
                        continue
                    proceeds = u * px * (1 - cfg.cost.sell_cost)
                    cash += proceeds
                    units -= u
                    assert trade is not None
                    trade.fills.append(
                        Fill(bar.d, "sell", reason, bar.close, px, weight, u, proceeds)
                    )
            pending = []
            equity = cash + units * px

            # ---- 2. 收盤後評估，掛出明日委託 ----
            if trade is None:
                setup = by_trigger.get(i)
                if setup is not None:
                    levels = build_levels(setup.peak, setup.trough, cfg.levels)
                    target = position_size(bar.close, levels, cfg)
                    trade = Trade(setup=setup, levels=levels, target_weight=target,
                                  equity_at_entry=equity)
                    base_equity = equity
                    swing_high = bar.close
                    peak_since_breakout = 0.0
                    derisked = reloaded = took_profit = False
                    unfilled = [(thr, w * target) for thr, w in cfg.entry.pullback_ladder]
                    pending.append(("buy", cfg.entry.base_weight * target, "底倉：訊號確認，不等回檔"))
            else:
                lv, c = trade.levels, bar.close
                swing_high = max(swing_high, c)
                bars_since = i - trade.setup.trigger_index
                if trade.fills:
                    trade.max_adverse_index = min(
                        trade.max_adverse_index, c / trade.fills[0].index_price - 1.0
                    )
                zone = lv.zone(c)

                if zone == "invalidated" and cfg.exit.hard_stop_at_trough:
                    unfilled = []
                    pending.append(("sell", 1.0, f"劇本失效：收盤跌破谷底 {lv.invalidation:,.0f}"))
                    trade.exit_reason = "stop_trough"

                elif zone == "warning" and not derisked:
                    unfilled = []
                    derisked = True
                    pending.append(("sell", cfg.exit.warn_derisk_fraction,
                                    f"警戒減碼：收盤跌破 {lv.warn_line:,.0f}（38.2% 回補位）"))

                else:
                    adds_allowed = zone in ("healthy", "buffer", "breakout")

                    if c >= lv.peak:
                        trade.reached_prior_high = True
                        peak_since_breakout = max(peak_since_breakout, c)

                    # (a) 突破前高 → 依設定補齊或取消未成交的分批單
                    breakout_filled = False
                    if trade.reached_prior_high and unfilled:
                        if cfg.entry.breakout_fills_remainder and adds_allowed:
                            for _, w in unfilled:
                                pending.append(("buy", w, f"突破補齊：站上前高 {lv.peak:,.0f}"))
                            breakout_filled = True
                        unfilled = []

                    # (b) 回到前高：分批獲利了結，其餘轉移動停利
                    if trade.reached_prior_high and not took_profit and units > 0 and not breakout_filled:
                        took_profit = True
                        pending.append(("sell", cfg.exit.target_take_fraction,
                                        f"目標達陣：回到前高 {lv.peak:,.0f}"))

                    if took_profit and units > 0 and c <= peak_since_breakout * (1 - cfg.exit.trail_drawdown):
                        pending.append(("sell", 1.0,
                                        f"移動停利：自 {peak_since_breakout:,.0f} 回檔 "
                                        f"{cfg.exit.trail_drawdown:.0%}"))
                        trade.exit_reason = "trail"

                    # (c) 減碼後收復主防線 → 補回一次
                    if derisked and not reloaded and adds_allowed and c >= lv.half_line and units > 0:
                        reloaded = True
                        # 解除減碼旗標：回補之後若再度跌破警戒線，還要能再減一次
                        derisked = False
                        pending.append(("buy", trade.filled_weight * cfg.exit.warn_derisk_fraction,
                                        f"回補：收復主防線 {lv.half_line:,.0f}"))

                    # (d) 回檔加碼梯
                    if unfilled and adds_allowed:
                        pullback = c / swing_high - 1.0
                        still: list[tuple[float, float]] = []
                        for thr, w in unfilled:
                            if pullback <= -thr:
                                pending.append(("buy", w, f"回檔加碼：自波段高點 {pullback:.1%}"))
                            else:
                                still.append((thr, w))
                        unfilled = still

                    # (e) 時間補齊：等不到回檔，不參與才是最大的風險
                    if unfilled and adds_allowed and bars_since >= cfg.entry.fill_timeout_bars:
                        for _, w in unfilled:
                            pending.append(("buy", w, f"時間補齊：{bars_since} 個交易日未見回檔"))
                        unfilled = []

                    # (f) 劇本過期
                    if (not trade.reached_prior_high and units > 0
                            and bars_since >= cfg.setup.setup_expiry_bars):
                        unfilled = []
                        pending.append(("sell", 1.0, f"劇本過期：{bars_since} 個交易日未創高"))
                        trade.exit_reason = "expired"

                # 部位歸零且沒有待買單 → 結案
                if units <= 0 and trade.entry_date is not None and not any(
                        s == "buy" for s, _, _ in pending):
                    if trade.exit_reason == "open":
                        trade.exit_reason = "flat"
                    trade.exit_date = bar.d
                    trade.equity_at_exit = equity
                    trades.append(trade)
                    trade = None

            equity_curve.append((bar.d, cash + units * px))

        if trade is not None:
            trade.exit_date = bars[-1].d
            trade.equity_at_exit = cash + units * etf_prices[-1]
            trades.append(trade)

        return Result(trades=trades, equity_curve=equity_curve, setups=setups)
