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
