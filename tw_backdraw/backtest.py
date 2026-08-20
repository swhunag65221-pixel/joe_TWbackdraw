"""回測與績效統計。"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .bars import Bar
from .config import DEFAULT_CONFIG, StrategyConfig
from .engine import Engine, Result
from .leveraged import synth_leveraged_path


@dataclass(frozen=True)
class Stats:
    n_setups: int
    n_trades: int
    hit_prior_high: int
    hit_rate: float
    win_rate: float
    avg_return: float
    median_return: float
    best: float
    worst: float
    total_return: float
    max_drawdown: float
    median_max_adverse: float

    def render(self) -> str:
        return "\n".join([
            f"訊號次數            {self.n_setups}",
            f"實際交易            {self.n_trades}",
            f"回到前高            {self.hit_prior_high} ({self.hit_rate:.0%})",
            f"獲利比例            {self.win_rate:.0%}",
            f"單筆平均報酬        {self.avg_return:+.1%}",
            f"單筆中位數報酬      {self.median_return:+.1%}",
            f"最佳 / 最差         {self.best:+.1%} / {self.worst:+.1%}",
            f"權益總報酬          {self.total_return:+.1%}",
            f"權益最大回檔        {self.max_drawdown:.1%}",
            f"進場後指數最大逆行  {self.median_max_adverse:.1%}（中位數）",
        ])


def align_etf(bars: list[Bar], etf_bars: list[Bar]) -> tuple[list[Bar], list[float]]:
    """把 ETF 日線對齊到指數日線，只保留兩邊都有交易的日子。

    00631L 2014-10-31 才掛牌，比加權指數短很多；用真實 ETF 價格回測時，
    回測期間會自動縮到重疊區間，而不是拿合成價去補前面那一段。
    """
    etf_by_date = {b.d: b.close for b in etf_bars}
    kept = [(b, etf_by_date[b.d]) for b in bars if b.d in etf_by_date]
    if not kept:
        raise ValueError("指數與 ETF 日線沒有重疊的交易日")
    return [b for b, _ in kept], [p for _, p in kept]


def run_backtest(bars: list[Bar], cfg: StrategyConfig | None = None,
                 etf_prices: list[float] | None = None) -> tuple[Result, Stats]:
    cfg = cfg or DEFAULT_CONFIG
    if etf_prices is None:
        etf_prices = synth_leveraged_path(bars, cfg.cost, cfg.sizing.leverage)
    result = Engine(cfg).run(bars, etf_prices)
    return result, summarize(result)


def summarize(result: Result) -> Stats:
    rets = [t.ret for t in result.trades]
    hits = sum(1 for t in result.trades if t.reached_prior_high)
    adverse = [t.max_adverse_index for t in result.trades] or [0.0]

    peak = -1e18
    max_dd = 0.0
    for _, eq in result.equity_curve:
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1.0)

    n = len(result.trades)
    return Stats(
        n_setups=len(result.setups),
        n_trades=n,
        hit_prior_high=hits,
        hit_rate=hits / n if n else 0.0,
        win_rate=sum(1 for r in rets if r > 0) / n if n else 0.0,
        avg_return=statistics.fmean(rets) if rets else 0.0,
        median_return=statistics.median(rets) if rets else 0.0,
        best=max(rets) if rets else 0.0,
        worst=min(rets) if rets else 0.0,
        total_return=result.final_equity - 1.0,
        max_drawdown=max_dd,
        median_max_adverse=statistics.median(adverse),
    )


@dataclass(frozen=True)
class TradeReview:
    """單筆 ETF 交易的完整檢視：報酬、期間極值，以及當初的進場條件。"""

    trade: "object"          # engine.Trade
    bars_held: int
    weight: float            # 實際建立的部位水位（佔權益比例）
    stop_distance: float     # 訊號日收盤到停損線的距離
    mfe: float               # 期間最大浮動獲利（權益，相對進場）
    mae: float               # 期間最大浮動虧損（權益，相對進場）
    max_drawdown: float      # 期間內從波段高點起算的最大回撤

    @property
    def setup(self):
        return self.trade.setup

    @property
    def levels(self):
        return self.trade.levels

    @property
    def from_peak(self) -> float:
        s = self.setup
        return s.trigger_close / s.peak - 1.0

    @property
    def exit_reason(self) -> str:
        t = self.trade
        if t.exit_reason == "open":
            return "尚未出場（持有中）"
        fills = t.fills
        if len(fills) > 1 and fills[-1].side == "sell":
            return fills[-1].reason
        return t.exit_reason


def review_trades(result: Result) -> list[TradeReview]:
    """把權益曲線切成逐筆部位，算出每筆的 MFE / MAE / 期間最大回撤。

    極值以**整體權益**衡量 —— 沒滿倉的時候現金部位會稀釋波動，
    這正是實際帳戶看到的數字，不是標的本身的漲跌。
    """
    idx = {d: i for i, (d, _) in enumerate(result.equity_curve)}
    out: list[TradeReview] = []
    for t in result.trades:
        if t.entry_date is None:
            continue
        a = idx[t.entry_date]
        b = idx[t.exit_date] if t.exit_date in idx else len(result.equity_curve) - 1
        seg = [eq for _, eq in result.equity_curve[a:b + 1]]
        base = t.equity_at_entry
        if not seg or base <= 0:
            continue
        peak, dd = seg[0], 0.0
        for x in seg:
            peak = max(peak, x)
            dd = min(dd, x / peak - 1.0)
        entry = t.setup.trigger_close
        out.append(TradeReview(
            trade=t, bars_held=b - a, weight=t.filled_weight,
            stop_distance=max(entry - t.levels.stop_line, 0.0) / entry,
            mfe=max(seg) / base - 1.0, mae=min(seg) / base - 1.0,
            max_drawdown=dd))
    return out
